#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_config_source_guard.py — 配置真源守卫（归一化 D 系列）

跑法:
    python tests/test_config_source_guard.py

守的是一条**单向**关系:

    config/config.yaml  ──派生──▶  llm/config/llm.env
      (唯一真源, Agent 只读这份)      (喂 llama-server 的派生文件)

为什么要有这个文件
    D 系列之前有三份配置，GUI 的模型测试页一次写三份、Agent 只认一份，于是
    "界面上改了参数，运行时不生效" 而且**没有任何测试会变红**。清理干净之后，
    这个守卫负责让它别再长回来 —— 谁把派生文件重新接回 Agent，或者把 GUI 自己
    那份配置模板捡回来，这里立刻失败。

检查三件事:

  1) `agent/` 的 Python 源码里不许出现 GUI 专用配置名，也不许出现派生文件名
     —— Agent 只认 config/config.yaml。
  2) GUI 专用配置的残留物不许复活: 工作区里不许再出现那份模板，
     `.gitignore` 与 `config/config.example.yaml`（都会送到板端）里不许再为它留规则。
  3) 反空转: 证明扫描真的读到了 agent/ 的源码，且里面真的出现过 config.yaml
     （否则第 1 条永远绿）。

范围说明
    本守卫只管**仓库内容**。"这台机器上还留着旧副本" 有两种正常来源，都不算回归:
    板端 HEAD 落后于仓库、或者文件被本机 `.git/info/exclude` 忽略。所以判据里
    带一次 `git check-ignore`: 被本机忽略的旧副本放过，工作区里**未被忽略**的
    副本才算回归。
"""

import re
import subprocess
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: 扫描范围: Agent 的 Python 源码。
AGENT_DIR = _PROJECT_ROOT / "agent"

#: 不许出现在 agent/ 里的字面量: (正则, 为什么)
FORBIDDEN_IN_AGENT = [
    (r"gui\.yaml",
     "Agent 不许读 GUI 的配置 —— 真源只有 config/config.yaml"),
    (r"llm\.env",
     "llm/config/llm.env 是喂 llama-server 的**派生**文件, 不是应用配置; "
     "Agent 读它就会踩上「改真源不生效」"),
]

#: 真源路径, 反空转检查用。
TRUTH_PATH = "config/config.yaml"

#: D 系列删掉的残留物, 不许复活。
GONE_FILES = ["gui/config/gui.yaml.example"]
GONE_GITIGNORE_PATTERNS = ["gui/config/gui.yaml"]

#: 进库、会送到板端的文件里不许出现的字面量: (正则, 为什么)
FORBIDDEN_IN_SHIPPED_CONFIG = [
    (r"gui\.yaml",
     "GUI 专用配置已经删除 —— 真源只有 config/config.yaml"),
    (r"进行中",
     "归一化 D 系列已完成, 不该再写'进行中'"),
]

#: 真源模板, 它必须体现"GUI 没有自己的配置"。
EXAMPLE_CONFIG = "config/config.example.yaml"


def _locally_ignored(rel_paths):
    """返回其中被**本机** git 忽略的那些（`.gitignore` + `.git/info/exclude`）。

    这是"仓库内容"与"这台机器上有什么"的分界: 板端用 `.git/info/exclude`
    保留它自己的资产, 那些不该让守卫变红。git 不在 / 不是仓库时返回空集合。
    """
    if not rel_paths:
        return set()
    try:
        proc = subprocess.run(
            ["git", "check-ignore", "--stdin"],
            cwd=str(_PROJECT_ROOT),
            input="\n".join(rel_paths),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            universal_newlines=True,
        )
    except (OSError, ValueError):
        return set()
    if proc.returncode not in (0, 1):        # 128 = 不是 git 仓库
        return set()
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


#: 派生文件 llm.env 的**写入者**（T14-2）。
#:
#: 为什么它既读又写还是对的：`llm.env` 是**派生文件**（喂 llama-server），
#: T14 起它的唯一实现搬到了 Python（`docs/adr/0005`：配置真源与派生文件都只由 Agent 写）。
#: 文本级写入必须"读原文 -> 只改目标行 -> 原子写回"，所以它必然既读又写 ——
#: 关键在于它**只把 llm.env 当产物**，绝不当配置来源（真源永远只有 config.yaml）。
#: 这个例外只对这一个路径生效：同一个文件里出现 `gui.yaml` 照样要红，
#: 别处**读/写** llm.env 也照样要红（见 TestAgentCopiesOnlyOneConfig 的机制测试）。
DERIVED_ENV_WRITER = "agent/core/llm_env.py"

#: "这一行真的在**操作文件**"的写法。
#:
#: 规矩（T14-2 起）：`llm.env` 这个名字**允许出现在注释/日志/协议说明里**
#: （它是个有名字的产物，日志里说清"派生没成功"是有用的），但**同一行里只要有
#: 文件读写**，那就只允许 `agent/core/llm_env.py` 干 —— 别的文件一律算回归。
#: ⚠ 与旧版（只给 `agent/cli.py` 开"可以提名字"的口子）相比, 这条把口径推广到了
#:   整个 `agent/`, 因为它管的是"有没有把它当配置读"这件事, 与文件叫什么无关。
FILE_OP_PATTERN = (r"(open\s*\(|read_text\s*\(|readlines\s*\(|write_text\s*\("
                   r"|write_text_atomic\s*\(|\.read\s*\(|\.write\s*\("
                   r"|json\.loads?\s*\(|yaml\.safe_load\s*\()")


def _agent_python_files():
    """agent/ 下要扫描的 .py（跳过 __pycache__），排序稳定。"""
    if not AGENT_DIR.is_dir():
        return []
    return sorted(p for p in AGENT_DIR.rglob("*.py") if "__pycache__" not in p.parts)


def _forbidden_hits(rel: str, line: str):
    """这一行在**这个相对路径**下命中了哪些禁令（抽出来是为了能给判定本身写测试）。

    @return [(pattern, why)] —— 空列表 = 放行
    """
    out = []
    for pattern, why in FORBIDDEN_IN_AGENT:
        if not re.search(pattern, line, re.IGNORECASE):
            continue
        if pattern == r"llm\.env":
            if rel == DERIVED_ENV_WRITER:
                continue
            if not re.search(FILE_OP_PATTERN, line):
                continue          # 只是提名字（注释/日志/协议说明）—— 放行
            why = ("只有 agent/core/llm_env.py 能读写派生文件 llm.env（它是那个产物的"
                   "唯一写入者）；别处连读都不许 —— 真源永远只有 config.yaml")
        out.append((pattern, why))
    return out


class TestAgentCopiesOnlyOneConfig(unittest.TestCase):
    """Agent 只认 config/config.yaml。"""

    def test_no_gui_or_derived_config_literals(self):
        found = []
        for path in _agent_python_files():
            text = path.read_text(encoding="utf-8")
            rel = path.relative_to(_PROJECT_ROOT).as_posix()
            for lineno, line in enumerate(text.splitlines(), 1):
                for pattern, why in _forbidden_hits(rel, line):
                    found.append("%s:%d  命中 /%s/\n      %s\n      原因: %s"
                                 % (rel, lineno, pattern, line.strip(), why))

        if found:
            self.fail("agent/ 里出现了 %d 处不该有的配置字面量 "
                      "(Agent 只读 %s; 如果确实是注释里说明'不读它', 请改写措辞):\n  %s"
                      % (len(found), TRUTH_PATH, "\n  ".join(found)))

    def test_the_derived_env_writer_exemption_has_teeth(self):
        """豁免必须是**按路径 + 按模式**的，不能变成"某个文件说什么都行"。

        四步：① 那个文件里 llm.env 放行（它本来就是写入者）；
        ② **别处读/写** llm.env 照样被抓；③ 只是提名字（日志/注释）放行；
        ④ 同一个文件里出现 `gui.yaml` 照样被抓。
        """
        self.assertEqual(_forbidden_hits(DERIVED_ENV_WRITER,
                                         'ENV_NAME = "llm.env"  # 派生文件'), [])
        self.assertTrue(_forbidden_hits("agent/ipc/__init__.py",
                                        'open("llm/config/llm.env").read()'),
                        "别的文件读 llm.env 必须被抓")
        self.assertTrue(_forbidden_hits("agent/cli.py",
                                        'env = read_text("llm/env")  # llm.env'),
                        "写/读原语混在一行里也要被抓")
        self.assertEqual(_forbidden_hits("agent/ipc/__init__.py",
                                         'log.info("派生 llm.env 没成功")'), [],
                         "只是提名字要放行（日志里说清产物名是有用的）")
        self.assertTrue(_forbidden_hits(DERIVED_ENV_WRITER, 'X = "gui.yaml"'),
                        "同一个文件里别的字面量不受豁免")

    def test_the_exempt_writer_really_exists_and_mentions_it(self):
        """反空转：豁免写的那个路径必须真的存在、真的提到 llm.env。

        否则将来文件改名/搬走，豁免就成了一句空话（守卫还以为自己在守着什么）。
        """
        path = _PROJECT_ROOT / DERIVED_ENV_WRITER
        self.assertTrue(path.is_file(), "豁免里的 %s 不存在" % DERIVED_ENV_WRITER)
        self.assertIn("llm.env", path.read_text(encoding="utf-8"),
                      "%s 里没提 llm.env —— 那这条豁免该删掉" % DERIVED_ENV_WRITER)

    def test_the_file_op_pattern_catches_a_real_read(self):
        """反空转：证明那条"同一行有文件操作就拦"的正则真的抓得住读文件。"""
        for sample in ('with open(env_path) as handle:  # llm.env',
                       'text = env_path.read_text()  # llm.env',
                       'data = json.load(handle)  # llm.env',
                       'Path("llm.env").read_text()'):
            with self.subTest(sample=sample):
                self.assertTrue(re.search(r"llm\.env", sample, re.IGNORECASE))
                self.assertTrue(re.search(FILE_OP_PATTERN, sample),
                                "FILE_OP_PATTERN 没抓住文件操作 —— 例外就成了后门")
        self.assertFalse(re.search(FILE_OP_PATTERN, 'print("llm.env")'),
                         "只是提名字不该被当成文件操作")

    def test_the_scan_actually_covers_something(self):
        """防止路径写错导致"零命中"这种假绿灯。"""
        files = _agent_python_files()
        self.assertGreaterEqual(len(files), 10,
                                "扫到的 agent/ 源码太少: %r" % (files,))

        # 真源路径必须真的出现在 agent/ 里 —— 否则上面那条检查等于什么都没查
        mentions = 0
        for path in files:
            if TRUTH_PATH in path.read_text(encoding="utf-8"):
                mentions += 1
        self.assertGreater(mentions, 0,
                           "agent/ 里没有任何文件提到 %s, 扫描范围可能不对" % TRUTH_PATH)


class TestNoGuiConfigLeftovers(unittest.TestCase):
    """GUI 专用配置的残留物不许复活。"""

    def test_template_is_not_in_the_working_tree(self):
        # 被本机忽略的旧副本放过（板端 / 落后于仓库的 checkout 都属正常）；
        # 工作区里一份**未被忽略**的副本才是回归。
        ignored = _locally_ignored(GONE_FILES)
        for rel in GONE_FILES:
            if (_PROJECT_ROOT / rel).exists() and rel not in ignored:
                self.fail("%s 应该已经被删除（界面参数在 config/config.yaml 的 gui: 段）。"
                          "如果这台机器只是落后于仓库，把它删掉，"
                          "或者写进本机 .git/info/exclude。" % rel)

    def test_gitignore_has_no_rule_for_it(self):
        gitignore = (_PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
        for pattern in GONE_GITIGNORE_PATTERNS:
            self.assertNotIn(pattern, gitignore,
                             ".gitignore 里还留着 %s 的规则；文件都删了, 规则也该删" % pattern)

    def test_example_config_declares_the_single_truth(self):
        """真源模板里必须能看出"GUI 没有自己的配置"。"""
        text = (_PROJECT_ROOT / EXAMPLE_CONFIG).read_text(encoding="utf-8")
        self.assertIn("gui:", text,
                      "%s 里应该有 gui: 段（GUI 的界面参数住在这里）" % EXAMPLE_CONFIG)
        for pattern, why in FORBIDDEN_IN_SHIPPED_CONFIG:
            hits = [ln.strip() for ln in text.splitlines()
                    if re.search(pattern, ln, re.IGNORECASE)]
            if hits:
                self.fail("%s 里还有 %d 行命中 /%s/:\n      %s\n      原因: %s"
                          % (EXAMPLE_CONFIG, len(hits), pattern,
                             "\n      ".join(hits), why))

    def test_scan_is_not_vacuous(self):
        """证明这两个文件真的被读到了（否则上面的断言永远绿）。"""
        gitignore = (_PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("config/*.yaml", gitignore,
                      ".gitignore 读进来的内容不对, 检查路径")
        self.assertTrue((_PROJECT_ROOT / EXAMPLE_CONFIG).is_file(),
                        "%s 不见了" % EXAMPLE_CONFIG)


#: 写文件的**原语**（不是"读"，也不是"提到路径"）。
#: ⚠ 这是 R3 新增的边界：在那之前 agent/ **一个字节都不写配置**，现在多了一条
#:   "一次性日程触发后把它从 config.yaml 里删掉"。写入者每多一处，都该是一次明确的决定。
#: ⚠ T11-8 补的一个**真漏项**：原来只认"整篇重写"那几种原语（`os.replace` / `mkstemp` /
#:   `write_text` / `write_text_atomic`）—— **追加写的（`open(path, "a")` + `write`）一个都扫不到**。
#:   于是 `agent/core/game_anchors.py`（T11-4 往 `config/game_anchors.jsonl` 追加锚点）
#:   明明是第 6 个写入者，这条守卫却看不见它。现在把追加写也算进来。
WRITE_PRIMITIVES = (r"(os\.replace|mkstemp|\.write_text\(|write_text_atomic\("
                    r"|open\([^)]*,\s*[\"']a)")

#: 允许出现写入原语的文件（**只有**这十一个）。
ALLOWED_WRITERS = {
    "agent/config.py",               # write_text_atomic: 全仓唯一的"原子写文本"实现
    "agent/core/schedule_config.py", # 唯一被允许的调用方: 删掉已触发的一次性日程 (R3) +
                                     # 工具 `set_schedule` 的增删 (T12-5) —— 都是**文本级**
                                     # 手术: 只动目标条目的那几行, 其余逐字节不变 + 留 .bak
    # T7-2 新增的第三个写入者: 壁纸**标签数据**（config/wall_data.jsonl）。
    # ⚠ 它写的不是配置真源，是**机器派生数据**（SigLIP 打出来的标签 + 向量）——
    #   真源仍然只有 config.yaml 一份。写入者只有 `assistant tag --apply` 这条路径。
    "agent/vision/wall_data.py",
    # T8-3 新增的第四个写入者: **本地音乐库**（config/music_library.jsonl）。
    # ⚠ 同样是本地数据不是真源: 一行一首歌（id + tags + 播放次数）。写入路径两条:
    #   `assistant music` 导入/打标, 以及运行期"听满 30 秒计一次"。
    "agent/media/music_library.py",
    # T9-2 新增的第五个写入者: **用户画像**（config/user_profile.jsonl）。
    # ⚠ 一次构建一行; 真源仍然只有 config.yaml。写入者只有画像内核这一条路径
    #   （T9-3 由 Agent 在"纯对话攒到 2000 字"时触发; 模型看不到、也调不到它）。
    "agent/core/user_profile.py",
    # T11-4 新增的第六个写入者: **游戏锚点库**（config/game_anchors.jsonl）。
    # ⚠ 这是**追加写**（`open(path, "a")`），一行一锚点: 游戏名 + 768 维 float16 向量 +
    #   截图路径 + 来源 + 时间。它不是配置真源，是识别用的派生数据；
    #   运行期只有一条写入路径 —— 画面与 PC 进程**不一致**时把那一帧登记成锚点（自纠错）。
    #   截图文件（config/game_anchors/<游戏>/*.jpg）也由它写。
    "agent/core/game_anchors.py",
    # T13-2 新增的第七个写入者: **学习内容锚点库**（config/study_anchors.jsonl）。
    # ⚠ 与游戏锚点库同款、但是**另一个文件**（你定的"两个库分开"）: 一行一锚点 =
    #   子标签（code/doc/real/anime/game）+ 768 维 float16 向量 + 截图路径 + 来源 + 时间。
    #   两条写入路径都在这一个模块里: 自学习时**追加**，以及"超每类上限丢最旧 / 清空"
    #   时**整篇原子重写**（`write_text_atomic`）。它不是配置真源，是识别用的派生数据；
    #   截图目录 config/study_anchors/<子标签>/ 也由它写。
    "agent/core/study_anchors.py",
    # T13-2 新增的第八个写入者: **学习监督的运行统计**（config/study_stats.json）。
    # ⚠ 全篇原子重写（不是追加）: 阈值 + 计数 + 分数分布 + 最近明细，**全程有界**
    #   （直方图固定 20 档、明细/备注都是定长环形、计数器与阈值项也封了上限）——
    #   它是"阈值能在运行期自己修正"（你定的）的**唯一依据**，也是诊断材料，不是配置真源。
    "agent/core/study_stats.py",
    # T13-8 新增的第九个写入者: **设置项的文本级写入**（`config/config.yaml` 里的那一行）。
    # ⚠ 这是**第二个能写真源**的 Agent 侧代码（第一个是 `schedule_config.py` 删日程）。
    #   入口只有一条: `assistant set`（默认只看，`--apply` 才写）。承诺与 GUI 的 ConfigStore
    #   一致: 只动目标那一行、注释/顺序/换行逐字节保留、旁边留 `.bak`、**缺段按模板新建**。
    #   能改的键 = `config.example.yaml` 里有的**标量**键（模板是键清单的唯一真源）；
    #   结构级的键（映射/序列）与不在模板里的键一律拒绝。
    "agent/core/settings_config.py",
    # T13-8 新增的第十个写入者: **B 站凭据文件**（config/bilibili_cookie.json）。
    # ⚠ 写的是**凭据**（`SESSDATA` = 账号），不是配置真源: 整份 JSON 原子重写 + `.bak`，
    #   只认三个键（`SEESSDATA` 那种笔误会被拒 —— 实测 B 站会把它当没登录）。
    #   入口: `assistant set cookie --apply`，以及 T14-2 起 GUI 走 IPC 让 Agent 写
    #   （`set_config` 的 `credentials` 字段）。
    "agent/core/settings_credentials.py",
    # T14-2 新增的第十一个写入者: **派生文件 llm/config/llm.env**（喂 llama-server）。
    # ⚠ 它不是配置真源（真源永远只有 config.yaml），是**产物**：按真源的 `llm:` 段
    #   重写那 8 行 `LLM_*=…`（只动目标行 + `.bak` + 原子写）。
    #   为什么搬到这里: T14 起"配置真源 + 派生文件"都只由 Agent 写（docs/adr/0005），
    #   原来的唯一实现是 C++ 的 `gui/src/core/config_sync.cpp`（T14-3 退役）。
    #   它是**唯一**允许碰 llm.env 的 Python 模块（见 DERIVED_ENV_WRITER）。
    "agent/core/llm_env.py",
}


class TestWhoWritesTheConfig(unittest.TestCase):
    """写 config.yaml 的地方必须是**数得出来**的（R3）。

    R3 之前 `docs/config-sources.md` 写的是"Agent 只读它"；现在 Agent 会在
    "一次性日程触发后"删掉那一条 —— 这条守卫把"写入者只有这一处"钉成机械可查的事实，
    免得哪天有人再加一处偷偷写配置。
    """

    def test_only_the_sanctioned_files_contain_write_primitives(self):
        offenders = []
        for path in _agent_python_files():
            rel = path.relative_to(_PROJECT_ROOT).as_posix()
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if not re.search(WRITE_PRIMITIVES, line):
                    continue
                if rel not in ALLOWED_WRITERS:
                    offenders.append("%s:%d\n      %s" % (rel, lineno, line.strip()))
        if offenders:
            self.fail("agent/ 里出现了新的写入者（写配置只允许 %s）:\n  %s\n"
                      "  要新增写入者, 请先想清楚边界并把它加进 ALLOWED_WRITERS 的说明里。"
                      % (", ".join(sorted(ALLOWED_WRITERS)), "\n  ".join(offenders)))

    def test_the_writer_scan_is_not_vacuous(self):
        """反空转：白名单里那十一个文件**真的**命中了写入原语，否则这条守卫什么都没查。

        ⚠ T11-8：这条断言正是"补上追加写"的理由 —— 只把 `game_anchors.py` 加进白名单
        而正则不认 `open(path, "a")`，这里就会红（那份白名单是假的）。
        ⚠ T13-2：`study_stats.py` 只做**整篇重写**（`write_text_atomic`），
        `study_anchors.py` 两条都有（追加 + 重写）—— 两个都必须在正则里有命中。
        """
        hits = {}
        for path in _agent_python_files():
            rel = path.relative_to(_PROJECT_ROOT).as_posix()
            if rel not in ALLOWED_WRITERS:
                continue
            text = path.read_text(encoding="utf-8")
            hits[rel] = len(re.findall(WRITE_PRIMITIVES, text))
        self.assertEqual(sorted(hits), sorted(ALLOWED_WRITERS),
                         "白名单里的文件没扫到（路径写错了？）")
        for rel, count in hits.items():
            self.assertGreater(count, 0,
                               "%s 里一个写入原语都没有 —— 正则或路径不对, 守卫是假的" % rel)


if __name__ == "__main__":
    unittest.main(verbosity=2)
