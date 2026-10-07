#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""image/check-runtime-deps.py — 运行时依赖闭环验收器（T15-2-7）

T15-2-7 的出口是"**chroot 内逐项在位 + ldd 无缺失**"。这句话要能**复跑**，
所以这里把它做成一台机器，而不是一次人眼检查。三件事：

  1. **逐项在位**：MANIFEST 里列出的每一样（二进制 / 库 / python 模块 / QML 模块 /
     字体 / gst 插件 / llama 运行时）在 target 树里都得在，并打印"为什么要它"。
  2. **ldd 无缺失**：不用 ldd（它要跑目标码、还要处理交叉），而是**自己解 DT_NEEDED**：
     遍历 target 里所有 ELF，把每个 NEEDED 拿去 target 自己的 lib 目录里找，
     找不到就算缺。这是 ldd 在交叉场景下的等价物，而且能覆盖**库与插件**
     （ldd 一次只能看一个文件）。
  3. **chroot 冒烟**（`--chroot`，默认开）：真的 `chroot` 进去跑几条命令 ——
     import numpy/cv2/yaml/rknnlite、llama-server --version、gst-inspect mppvideodec、
     fc-list 看中文字体、bash/curl/nmcli/ssh 的版本。WSL 里 binfmt 已注册
     qemu-aarch64 时直接 chroot 就能跑；不能时退化成"把 qemu-aarch64-static 临时拷进去
     跑完再删掉"（绝不留进镜像）。

另外它还是 **R8 的答案**：`PY_MODULES` 把仓库里**真实出现过**的第三方 import 逐个
定性（required / optional / host），`tests/test_image_runtime.py` 用 AST 扫仓库
反过来核对"有没有新 import 没被定性" —— 新增一个库而镜像没跟上，CI 就会红。

用法：
    python3 image/check-runtime-deps.py <SDK 根目录> [--chroot|--no-chroot] [--json]
    python3 image/check-runtime-deps.py <SDK 根目录> --target <target 树>

    ⚠ 不指定 `--target` 时，会**优先选整机构建那棵树**（`buildroot/output/<CFG>/<CFG>/target`，
    也就是 `update.img` 的来源），并把选中的是哪棵打印出来 —— T15-2-10 之前这里写死成
    单包那棵，于是"整机构建后的验证"其实在看另一棵树（详见 image/imagelib.py 的说明）。

退出码：0 = 全过；1 = 缺件/闭包缺/冒烟失败；3 = 上面都过、但 **payload 还没装**
（agent/GUI/native/默认配置，2-8 明确留给后面的那批，见 §5.6 末段）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import imagelib  # noqa: E402  （同目录的工具模块，两个检查器共用）

CFG = imagelib.CFG_DEFAULT

# ---------------------------------------------------------------------------
#  R8：仓库里出现过的第三方 Python 模块 → 定性
#     required : 镜像里必须能 import（缺了主链路就坏）
#     optional : 懒加载/可选后端，缺了要能降级（代码里都有 try/except ImportError）
#     host     : 只在开发机/CI 用，镜像里不该有
#  这张表由 tests/test_image_runtime.py 与仓库真实 import 对照，防止漏项。
# ---------------------------------------------------------------------------
PY_MODULES = {
    "numpy":     ("required", "python-numpy", "视觉/帧管线的点采样"),
    "cv2":       ("required", "opencv4 + LIB_PYTHON", "视觉/帧管线"),
    "yaml":      ("required", "python-pyyaml", "配置读写"),
    "psutil":    ("required", "python-psutil", "rknnlite 的声明依赖"),
    "ruamel.yaml": ("required", "python-ruamel-yaml", "rknnlite 的声明依赖"),
    "rknnlite":  ("required", "prepare-rknnlite.sh（SDK 里的 cp311 wheel）", "SigLIP 走 NPU"),
    "openai":    ("required", "prepare-openai.sh（PyPI 轮子，钉 3.24.0）",
                  "★ 2026-10-07 改判：不是 optional ✗ —— 它挡住的是**主功能**（工具调用 ⇒ "
                  "bilibili_search ⇒ 队列 ⇒ 视频）。镜像里缺它时对话/搜索会静默降级到规则兜底 ✗"),
    "httpx2":    ("required", "prepare-openai.sh（openai 3.x 的 HTTP 客户端）", "openai 的运行时依赖"),
    "pydantic":  ("required", "prepare-openai.sh", "openai 的运行时依赖"),
    "jiter":     ("required", "prepare-openai.sh（aarch64 二进制轮子）", "openai 的运行时依赖"),
    "tokenizers": ("required", "prepare-tokenizers.sh（PyPI 轮子，钉 0.20.3，--no-deps）",
                   "★ 2026-10-07 改判：不是 optional ✗ —— moonlight 连上之后 study/认游戏那一路"
                   "（SigLIP）直接报「缺少 tokenizers 库」⇒ 真帧喂不进 NPU ✗"),
    "cryptography": ("host", "只在 scripts/pair_analyze.py 用", "开发机分析工具"),
    "pytest":    ("host", "镜像里没有", "测试框架"),
    "pytest_asyncio": ("host", "镜像里没有", "测试框架"),
    "agent_native": ("host", "板上/镜像里由 pybind11 扩展提供，镜像侧是 T15-2-7 之后的事", "本地原生扩展"),
    "agent_native_test": ("host", "pybind11 的测试扩展（native/ 构建产物）", "本地原生扩展"),
}

# ---------------------------------------------------------------------------
#  逐项在位清单：(类别, target 相对路径或模块名, 为什么要它)
# ---------------------------------------------------------------------------
MANIFEST = [
    ("bin", "usr/bin/python3", "Agent 的解释器"),
    ("bin", "usr/bin/bash", "llm/scripts/*.sh 是 bash 脚本（ash 跑不了）"),
    ("bin", "usr/bin/curl", "llm/scripts/status.sh 的健康检查"),
    ("bin", "usr/bin/nmcli", "GUI 网络卡片 / wifi 配置"),
    ("bin", "usr/sbin/NetworkManager", "联网（wlan0 rtl8822cs）"),
    ("bin", "usr/sbin/sshd", "dev 镜像要 ssh 进去；release 里留着由 unit 决定开不开"),
    ("bin", "usr/bin/gst-inspect-1.0", "gst 元素自检"),
    ("bin", "usr/bin/fc-list", "字体查询（中文界面）"),
    ("lib", "usr/lib/librockchip_mpp.so", "MPP（硬解/硬编，Moonlight 那条 CPU 80% 的解码要靠它）"),
    ("lib", "usr/lib/librknnrt.so", "NPU 运行时（SigLIP）"),
    ("lib", "usr/lib/libyaml-cpp.so", "GUI 日程区读 config.yaml"),
    ("lib", "usr/lib/libQt5Multimedia.so.5", "GUI 的 QMediaPlayer"),
    ("lib", "usr/lib/libQt5Quick.so.5", "虚拟键盘面板（QML）"),
    ("lib", "usr/lib/libgstreamer-1.0.so.0", "Qt Multimedia 的后端"),
    ("gst", "usr/lib/gstreamer-1.0/libgstrockchipmpp.so", "MPP 的 gst 插件：mppvideodec / mpph264enc"),
    ("plugin", "usr/lib/qt/plugins/platforms/libqeglfs.so", "无 X 的 QPA（eglfs_kms，链到 libmali.so.1）"),
    ("plugin", "usr/lib/qt/plugins/platforminputcontexts/libqtvirtualkeyboardplugin.so", "软键盘"),
    ("qml", "usr/qml/QtQuick.2", "T15-1 1b 四个 QML 模块之一"),
    ("qml", "usr/qml/QtQuick/Window.2", "T15-1 1b 四个 QML 模块之一"),
    ("qml", "usr/qml/QtQuick/Layouts", "T15-1 1b 四个 QML 模块之一"),
    ("qml", "usr/qml/Qt/labs/folderlistmodel", "T15-1 1b 四个 QML 模块之一"),
    ("font", "usr/share/fonts/source-han-sans-cn", "中文界面的字形（Source Han Sans CN）"),
    ("llm", "usr/lib/assistant/llm/bin/llama-server", "edge 模式的本地推理（交叉编译，见 image/build-llama.sh）"),
    ("llm", "usr/lib/assistant/llm/scripts/start.sh", "Agent 用它起 llama-server"),
    ("llm", "usr/lib/assistant/llm/scripts/stop.sh", "Agent 用它停 llama-server"),
    ("llm", "usr/lib/assistant/llm/scripts/status.sh", "人看的健康检查"),
]

#: python 模块在镜像里"在位"的判据（site-packages 下的名字）
PY_IN_IMAGE = {
    "numpy": "numpy",
    "cv2": "cv2",
    "yaml": "yaml",
    "psutil": "psutil",
    "ruamel.yaml": "ruamel",
    "rknnlite": "rknnlite",
    "openai": "openai",
    "httpx2": "httpx2",
    "pydantic": "pydantic",
    "jiter": "jiter",
    "tokenizers": "tokenizers",
}



# site_packages 已收敛到 imagelib（T15-3 / 3-6a）——不再留本地包装


# ---------------------------------------------------------------------------
#  4) payload：agent / GUI / native / 默认配置（unit 文件已经按这些路径写好了）
#  ---------------------------------------------------------------------------
#  与 MANIFEST 的区别：
#    · MANIFEST = "镜像里必须有、而且现在就该有"（缺了就是 bug，构建/配方错了）
#    · PAYLOAD  = "unit 文件已经指向它们，但**还没做**"（docs/image.md §5.6 末段：
#                 "agent/GUI 的 payload（代码与二进制）进 rootfs 也还没做"）
#  为什么必须单独一类：缺 payload 不会让 buildroot 报错，只会让板子起来之后
#  agent.service 反复 `No module named agent`、agent-gui.service 反复
#  `No such file or directory` —— 构建绿的、板子废的。所以刷板前必须能看见。
# ---------------------------------------------------------------------------
PAYLOAD = [
    ("agent", "usr/lib/assistant/agent/main.py",
     "agent.service 的 ExecStart 是 `python3 -m agent.main`（PYTHONPATH=/usr/lib/assistant）"),
    ("agent", "usr/lib/assistant/agent/__init__.py",
     "同上：得是个**包**才 import 得进来"),
    ("gui", "usr/lib/assistant/gui/agent_gui",
     "agent-gui.service 的 ExecStart（Qt5/EGLFS，要用 buildroot 的 Qt 交叉编译）"),
    ("native", "usr/lib/assistant/agent_native.cpython-311-aarch64-linux-gnu.so",
     "agent 的 pybind11 扩展（懒加载，缺了输入/视觉/Moonlight 那几条走不通；"
     "仓库里现有那份是 cp38，镜像 python 是 3.11 用不了）"),
    ("native", "usr/lib/assistant/libmoonlight-common-c.so",
     "上面那个扩展链的库：它的 RPATH 是 $ORIGIN，必须同目录（否则 import 时报找不到）"),
    ("config", "usr/lib/assistant/config/config.example.yaml",
     "assistant-init.service 首启从这里 `cp -n` 出 /data/assistant/config/config.yaml"),
    ("config", "usr/lib/assistant/config/user_profile.example.yaml",
     "同上（user_profile.yaml）"),
    ("doc", "usr/lib/assistant/Readme.md", "agent.service 的 Documentation= 指向它"),
    ("doc", "usr/lib/assistant/docs/gui.md", "agent-gui.service 的 Documentation= 指向它"),
    ("doc", "usr/lib/assistant/docs/image.md", "assistant-init.service 的 Documentation= 指向它"),
    ("bin", "usr/bin/assistant",
     "板端 CLI 入口（包装 `python3 -m agent.cli`，并把 D7 那套环境变量备好；"
     "T15-7 的 cli 唤醒要用它）"),
    ("profile", "etc/profile.d/assistant.sh",
     "交互式 shell 里也能 `python3 -m agent.cli`（单元里的 Environment= 不会传给登录 shell）"),
]


def check_payload(target: Path) -> list:
    rows = []
    for kind, rel, why in PAYLOAD:
        p = target / rel
        rows.append({"kind": kind, "name": rel, "why": why, "ok": p.exists(),
                     "detail": "" if p.exists() else "还没装进 rootfs"})
    return rows


# ---------------------------------------------------------------------------
#  1) 逐项在位
# ---------------------------------------------------------------------------
#: ★★ 2026-10-07（**一次真事故换来的门禁** ✓）：镜像里**不许有桌面/Wayland 合成器**。
#:   我们的形态是"无 X、无桌面、GUI 直接跑在 EGLFS 上"（docs/image.md 决策 D1 / §5.7）。
#:   事故：buildroot **不会删掉**上一次开着时装进 target 的文件 ✗ ⇒ 陈旧的 weston
#:   （30 个文件 + sysinit.target.wants/weston.service ✗）被我这次重建打进 rootfs ✗，
#:   它 WantedBy=sysinit.target **开机极早就抢走 DRM** ⇒ 我们的 GUI 画不上去
#:   （板端实测 `Could not queue DRM page flip on screen DSI1 (Device or resource busy)` ✗），
#:   用户看到的是"界面没起来、偶尔闪一张图" ✗。`.config` 里 WESTON=not set 拦不住它 ✗
#:   （文件是陈旧的，不是这次编出来的）⇒ 只能在这儿用**产物本身**兜住 ✓。
FORBIDDEN_IN_ROOTFS = [
    "usr/bin/weston",
    "usr/bin/weston-launch",
    "usr/libexec/weston-desktop-shell",
    "usr/libexec/weston-keyboard",
    "usr/lib/systemd/system/weston.service",
    "etc/systemd/system/sysinit.target.wants/weston.service",
    "etc/xdg/weston",
]


def check_present(target: Path) -> list:
    #: ⚠ 2026-10-07 修（T15-3 / 3-6a 的漏改）：`site_packages` 那次被"收敛到 imagelib"，
    #:   这里却还按**本地函数**调 ✗ ⇒ `NameError` ⇒ 整个刷板前验收在第 1 步就崩 ✓
    #:   （实测：`preflash-check.sh` 直接 `NameError: name 'site_packages' is not defined` ✓）。
    site = imagelib.site_packages(target)
    rows = []
    for kind, rel, why in MANIFEST:
        p = target / rel
        rows.append({"kind": kind, "name": rel, "why": why, "ok": p.exists(),
                     "detail": "" if p.exists() else "不存在"})
    #: ★ 门禁：**不该有的东西一个都不许有** ✓（见上面 FORBIDDEN_IN_ROOTFS 的事故说明 ✓）
    for rel in FORBIDDEN_IN_ROOTFS:
        if (target / rel).exists():
            rows.append({"kind": "禁止", "name": rel,
                         "why": "镜像里不许有桌面/Wayland 合成器（会抢 DRM ✗）",
                         "ok": False, "detail": "存在 ⇒ 先清掉 target 里的陈旧文件再重建 ✗"})
    for mod, (cls, where, why) in sorted(PY_MODULES.items()):
        if cls != "required":
            continue
        leaf = PY_IN_IMAGE.get(mod, mod.split(".")[0])
        p = site / leaf
        ok = p.exists()
        rows.append({"kind": "py", "name": mod, "why": "%s（%s）" % (why, where), "ok": ok,
                     "detail": "" if ok else "site-packages 里没有 %s" % leaf})
    return rows


# ---------------------------------------------------------------------------
#  2) DT_NEEDED 闭包（ldd 的交叉版）
# ---------------------------------------------------------------------------
ELF_MAGIC = b"\x7fELF"


def _is_elf(p: Path) -> bool:
    try:
        with p.open("rb") as fh:
            return fh.read(4) == ELF_MAGIC
    except OSError:
        return False


def _dyn(p: Path) -> tuple:
    """返回 (NEEDED 列表, RPATH/RUNPATH 列表)。"""
    try:
        out = subprocess.run(["readelf", "-d", str(p)], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, universal_newlines=True,
                             timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return [], []
    needed = re.findall(r"NEEDED\).*\[(.*?)\]", out)
    rpaths = re.findall(r"(?:RPATH|RUNPATH)\).*\[(.*?)\]", out)
    return needed, rpaths


def _expand_rpath(entry: str, elf_dir: str) -> str:
    """展开 RPATH 里的 $ORIGIN / $LIB / $PLATFORM（ld.so 的语义）。"""
    return (entry.replace("$ORIGIN", elf_dir).replace("${ORIGIN}", elf_dir)
                 .replace("$LIB", "lib").replace("${LIB}", "lib")
                 .replace("$PLATFORM", "aarch64").replace("${PLATFORM}", "aarch64"))


def check_closure(target: Path, limit: int = 40) -> tuple:
    """ldd 的交叉版：按 ld.so 的搜索顺序解析每个 ELF 的 NEEDED。

    搜索顺序（**照 ld.so 的真实语义**，不是"随便哪里能找到就算"）：
        1) 它自己的 RPATH / RUNPATH（展开 `$ORIGIN` —— 例如 libpulse 的
           `$ORIGIN/pulseaudio`，那一份就在子目录里）
        2) target/usr/lib 与 target/lib（外加 gst / qt 插件目录）
    ⚠ **不含"可执行文件自己所在目录"**：ld.so 并不搜索它。第一版把它算进去了，
    于是 `llama-server` 明明缺 `libllama-server-impl.so`（没有 $ORIGIN rpath）也被判成"解析得到" ——
    正是 chroot 冒烟把这个问题抓出来的。缺 RPATH 的库就该在这里报出来。
    返回 (缺失列表, 检查过的 ELF 数)。缺失项 = (文件, 缺的库)。
    """
    libdirs = [target / "usr/lib", target / "lib"]
    present = set()
    for d in libdirs:
        if d.is_dir():
            for p in d.rglob("*"):
                if p.is_file() or p.is_symlink():
                    present.add(p.name)
    for sub in ("usr/lib/gstreamer-1.0", "usr/lib/qt/plugins"):
        d = target / sub
        if d.is_dir():
            for p in d.rglob("*.so*"):
                present.add(p.name)

    missing, checked = [], 0
    for d in libdirs:
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*")):
            if p.is_dir() or not _is_elf(p):
                continue
            checked += 1
            needed, rpaths = _dyn(p)
            if not needed:
                continue
            search = []
            for rp in rpaths:
                for one in rp.split(":"):
                    search.append(_expand_rpath(one, str(p.parent.resolve())))
            for need in needed:
                if need.startswith("ld-linux"):
                    continue
                found = need in present
                if not found:
                    for sdir in search:
                        if Path(sdir, need).exists():
                            found = True
                            break
                if not found:
                    missing.append((str(p.relative_to(target)), need))
            if len(missing) >= limit:
                return missing[:limit], checked
    return missing[:limit], checked


# ---------------------------------------------------------------------------
#  3) chroot 冒烟
# ---------------------------------------------------------------------------
QEMU_NAMES = ("qemu-aarch64-static", "qemu-aarch64")


def _qemu() -> str:
    for n in QEMU_NAMES:
        p = shutil.which(n)
        if p:
            return p
    return ""


def chroot_smoke(target: Path, use_chroot: bool, verbose: bool = False) -> list:
    """真跑一遍；返回 [{cmd, ok, out}]。"""
    cmds = [
        ("python3 版本", ["/usr/bin/python3", "--version"]),
        ("import numpy/cv2/yaml/psutil/ruamel", [
            "/usr/bin/python3", "-c",
            "import numpy, cv2, yaml, psutil, ruamel.yaml;"
            "print('numpy', numpy.__version__, 'cv2', cv2.__version__)"]),
        #: ★ 2026-10-07：openai 是**主功能**依赖（工具调用/搜索/入队）⇒ 必须在镜像里真 import 成功 ✓
        #:   （构建机上不猜：这条 chroot 冒烟就是判据 ✓）
        ("import openai（对话/工具调用）", [
            "/usr/bin/python3", "-c",
            "import openai, httpx2, pydantic, jiter;"
            "print('openai', openai.__version__, 'pydantic', pydantic.VERSION)"]),
        #: ★ 2026-10-07（T15-2-10d）：tokenizers = study/认游戏（SigLIP）的必需后端 ✓
        ("import tokenizers（SigLIP 分词）", [
            "/usr/bin/python3", "-c",
            "import tokenizers; print('tokenizers', tokenizers.__version__)"]),
        ("import rknnlite（NPU API）", [
            "/usr/bin/python3", "-c",
            "from rknnlite.api import RKNNLite; print('rknnlite ok', RKNNLite.__name__)"]),
        ("bash", ["/usr/bin/bash", "-c", "echo bash ok"]),
        ("curl", ["/usr/bin/curl", "--version"]),
        ("nmcli", ["/usr/bin/nmcli", "--version"]),
        ("gst 硬解元素", ["/usr/bin/gst-inspect-1.0", "mppvideodec"]),
        ("中文字体", ["/usr/bin/fc-list", ":lang=zh"]),
        ("llama-server", ["/usr/lib/assistant/llm/bin/llama-server", "--version"]),
    ]
    if not use_chroot:
        return [{"cmd": c[0], "ok": None, "out": "（跳过）"} for c in cmds]

    qemu = _qemu()
    inner = None
    plain_ok = False
    no_priv = False
    for probe_argv in (["/usr/bin/python3", "--version"], ["/bin/sh", "-c", "true"]):
        try:
            r = subprocess.run(["chroot", str(target)] + probe_argv,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               universal_newlines=True, timeout=120)
            if r.returncode == 0:
                plain_ok = True
                break
            if "cannot change root directory" in (r.stdout or ""):
                no_priv = True
        except (OSError, subprocess.SubprocessError):
            pass

    if no_priv and not plain_ok:
        # 缺权限 ≠ 缺组件：如实说"跳过"，并给出拿到 root 的办法（不假装通过）
        hint = ("（跳过：chroot 需要 root，当前 uid=%d。用 `sudo` 或 "
                "`wsl -u root` 跑本脚本即可做真冒烟）" % os.geteuid())
        return [{"cmd": c[0], "ok": None, "out": hint} for c in cmds]

    if not plain_ok:
        if not qemu:
            return [{"cmd": "chroot", "ok": False,
                     "out": "没有 qemu-user，且直接 chroot 不可用"}]
        # 把静态 qemu 临时拷进树里，跑完删掉（绝不留进镜像）
        dst = target / "usr/bin" / Path(qemu).name
        shutil.copy2(qemu, dst)
        inner = "/usr/bin/" + Path(qemu).name

    results = []
    try:
        for label, argv in cmds:
            full = (["chroot", str(target)] + ([inner] if inner else []) + argv)
            try:
                r = subprocess.run(full, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   universal_newlines=True, timeout=300)
                out = (r.stdout or "").strip()
                ok = r.returncode == 0
            except (OSError, subprocess.SubprocessError) as exc:
                out, ok = "运行失败: %r" % (exc,), False
            results.append({"cmd": label, "ok": ok, "out": out[:1500]})
    finally:
        if inner:
            try:
                (target / "usr/bin" / Path(qemu).name).unlink()
            except OSError:
                pass
    return results


# ---------------------------------------------------------------------------
def main(argv) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    flags = {a for a in argv[1:] if a.startswith("--")}
    if not args:
        print(__doc__.strip().splitlines()[-1])
        print("用法: python3 image/check-runtime-deps.py <SDK 根目录> "
              "[--target <target 树>] [--chroot|--no-chroot] [--json]")
        return 2
    sdk = Path(args[0]).resolve()

    # --target <目录>：显式指定（也给 --target=<目录> 这种写法留个口子）
    explicit = ""
    for i, a in enumerate(argv[1:]):
        if a == "--target" and i + 2 <= len(argv[1:]):
            explicit = argv[1:][i + 1]
        elif a.startswith("--target="):
            explicit = a.split("=", 1)[1]
    target, why = imagelib.resolve_target(sdk, explicit or None)
    if target is None:
        print("!! %s" % why, file=sys.stderr)
        return 2

    want_chroot = "--no-chroot" not in flags
    rows = check_present(target)
    payload = check_payload(target)
    missing, checked = check_closure(target)
    smoke = chroot_smoke(target, want_chroot)

    payload_bad = [r for r in payload if not r["ok"]]

    if "--json" in flags:
        print(json.dumps({"target": str(target), "target_kind": why,
                          "present": rows, "payload": payload,
                          "closure_missing": missing,
                          "closure_checked": checked, "smoke": smoke},
                         ensure_ascii=False, indent=2))
        return 0 if not payload_bad else 3

    print("== 树：%s" % target)
    print("   （%s）" % why)
    print()
    print("== 逐项在位")
    bad = 0
    for r in rows:
        mark = "[OK] " if r["ok"] else "[缺] "
        if not r["ok"]:
            bad += 1
        print("   %s %-10s %-58s %s" % (mark, r["kind"], r["name"], r["why"]))

    print()
    print("== DT_NEEDED 闭包（ldd 的交叉版）：检查了 %d 个 ELF" % checked)
    if missing:
        print("   !! 有 %d 处依赖在镜像里找不到：" % len(missing))
        for f, n in missing:
            print("      %s -> %s" % (f, n))
    else:
        print("   = 全部解析得到（缺失 0）")

    print()
    print("== chroot 冒烟")
    smoke_bad = 0
    for s in smoke:
        if s["ok"] is None:
            print("   [--] %-38s %s" % (s["cmd"], s["out"]))
            continue
        print("   %s %-38s %s" % ("[OK] " if s["ok"] else "[失败]", s["cmd"],
                                  (s["out"].splitlines() or [""])[0][:110]))
        if not s["ok"]:
            smoke_bad += 1
            if s["out"]:
                for line in s["out"].splitlines()[:6]:
                    print("          %s" % line[:140])

    print()
    print("== payload（我们的 agent / GUI / native / 默认配置；unit 文件已经按这些路径写好了）")
    for r in payload:
        mark = "[OK] " if r["ok"] else "[未做]"
        print("   %s %-8s %-64s %s" % (mark, r["kind"], r["name"], r["why"]))
    if payload_bad:
        print("   ⚠ %d/%d 项还没装进 rootfs —— 这不是构建错误，是**已计划但还没做**的那一步"
              % (len(payload_bad), len(payload)))
        print("      （docs/image.md §5.6 末段写明留给后面）。缺了它，assistant.target 起得来、"
              "但 agent/gui 会一直重启。")

    print()
    verdict = (bad == 0 and not missing and smoke_bad == 0)
    skipped = sum(1 for s in smoke if s["ok"] is None)
    print("== 结论：%s（在位缺 %d、依赖缺 %d、冒烟失败 %d%s、payload 未做 %d/%d）"
          % ("通过" if verdict else "不通过", bad, len(missing), smoke_bad,
             "、冒烟跳过 %d" % skipped if skipped else "",
             len(payload_bad), len(payload)))
    if skipped and bad == 0 and not missing:
        print("   （在位与闭包都过了；冒烟是因权限不足跳过的，拿到 root 再跑一遍就有真证据）")
    if verdict and payload_bad:
        print("   → 系统层可以刷；**但要让 assistant.target 真的把服务拉起来，得先把 payload 装进 rootfs**")
        return 3
    return 0 if verdict else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
