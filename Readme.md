# Agent

开发机上的源码工作区。负责 C++/pybind11 底层、Python Agent Core、PySide6 GUI 的开发、交叉编译与部署。

目标板：RK3568 / Ubuntu 20.04 / aarch64 / glibc 2.31

---

## 目录结构

```
agent/
├── cmake/
│   └── toolchain.cmake          # aarch64 交叉编译工具链 (sysroot / -isystem 多架构路径)
├── native/                      # C++ / pybind11
│   ├── ring_buffer.h            # SPSC 无锁环形缓冲 (模板)
│   ├── image_rb.cpp/.h          # 256×256 RGB888 视频帧环形缓冲 (容量 300)
│   ├── host_input_rb.cpp/.h     # 主机输入事件环形缓冲 (容量 128)
│   ├── preprocess.cpp/.h        # YUV420P → 256×256 RGB888
│   ├── decoder.cpp/.h           # H.265 硬解 (板端 V4L2/FFmpeg)
│   ├── input_sender.cpp/.h      # send_key / send_mouse / send_hotkey
│   ├── moonlight_connection.cpp/.h  # HTTP 握手 / serverinfo / applist / pair
│   ├── moonlight_adapter.cpp/.h # 连接状态机 + 两条接收线程
│   ├── binding_utils.h          # binding 与测试共用的转换工具 (Frame→numpy 等)
│   ├── binding.cpp              # pybind11 模块 agent_native
│   └── third_party/             # 子模块: moonlight-common-c, pybind11, googletest
├── agent/                       # Python Agent Core
│   ├── __init__.py
│   └── config.py                # YAML 配置加载/保存/点号路径读取
│   (main.py / io.py / state.py / scheduler.py / router.py / llm.py /
│    vision.py / ipc.py / tools/ —— 待实现)
├── gui/                         # PySide6 GUI (待实现: main.py / panels.py / zmq_client.py)
├── config/                      # 配置模板 (真实配置不入 git)
│   ├── config.example.yaml      # 全局: llm.mode, sunshine.*, ipc.*
│   ├── user_profile.example.yaml# 用户画像
│   └── schedule.example.yaml    # 日程
├── scripts/                     # 构建 / 部署 / 测试 / 配对工具
│   ├── build.ps1                # aarch64 交叉编译
│   ├── test-host.ps1            # 宿主机单测 (ctest)
│   ├── deploy.ps1               # 打包并推到板端
│   ├── setup-sysroot-deps.ps1   # 给 sysroot 补 python3.8-dev / ffmpeg-dev
│   └── pair_sunshine.py 等      # Sunshine SRSAES 配对 (见 docs/)
├── tests/                       # 宿主机单测
│   ├── CMakeLists.txt
│   ├── test_*.cpp               # C++ 单测 (gtest, 由 ctest 驱动)
│   ├── test_config.py           # Python 单测 (unittest, 直接 python 跑)
│   ├── host/                    # 需要 numpy 的绑定层测试 (按需手动跑)
│   └── board/                   # 板端真机验收脚本
├── docs/                        # 文档
│   └── sunshine-pairing-findings.md
├── build-host/                  # 宿主机构建产物 (不入 git)
├── build-rk3568/                # 交叉编译产物 (不入 git)
├── CMakeLists.txt
├── Readme.md
└── todo.md
```

---

## 快速开始

```bash
# 初始化子模块 (必须 --recursive: moonlight-common-c 还有 enet / nanors 子模块)
git submodule update --init --recursive

# 交叉编译 (aarch64)
scripts/build.ps1

# 宿主机单测 (C++)
scripts/test-host.ps1

# 宿主机单测 (Python)
python tests/test_config.py

# 部署到板端
scripts/deploy.ps1
```

---

## 配置

配置放在 `config/`，**真实 `*.yaml` 不入 git**，仓库里只提交 `*.example.yaml` 模板。

```bash
cp config/config.example.yaml       config/config.yaml
cp config/user_profile.example.yaml config/user_profile.yaml
cp config/schedule.example.yaml     config/schedule.yaml
```

没有建真实配置时会自动退回读模板，所以刚 clone 下来也能直接跑。

```python
from agent import config

cfg = config.load_config("config")     # -> dict (命中缓存时不读盘)
config.get("sunshine.host")            # -> "192.168.137.1" (点号路径)
config.get("llm.mode", "board")        # 缺失时给默认值

config.save_config("config", {...})    # 写 config.yaml 并同步刷新缓存
```

约定：

- 配置名走**白名单**（`config.ALLOWED_CONFIGS`），新增配置要同时加名字和模板。
- 不做 schema 校验（各模块校验自己的字段）、不做热重载、不做环境变量替换。
- `AGENT_CONFIG_DIR` 可覆盖配置目录（板端把配置放别处时用）。

---

## 关键约定

- 板端不手改代码，`agent/`、`.so`、`VERSION` 由 `deploy.ps1` 覆盖。
- `send_key` / `send_mouse` / `send_hotkey` 必须释放 GIL，不阻塞 asyncio。
- ZeroMQ PUB 绑定 `0.0.0.0:5555`，SUB 连板端 `:5556`。
- 固件升级后重拉 sysroot 并重新交叉编译，否则 glibc 不匹配。
- GameStream 的 `/applist`、`/launch`、`/resume` **只在 HTTPS 47984** 上，且需要已配对的客户端证书。

---

## 环境

| 项 | 值 |
| --- | --- |
| 工具链 | GCC 9.2-2019.12，`E:/rk3568/arm/` |
| sysroot | `E:/rk3568/sysroot`（从板端 tar 同步；dev 头文件由 `setup-sysroot-deps.ps1` 补） |
| 板端 | `root@192.168.137.30`，`/home/kickpi/myproject/assitant/` |
| 板端 Python | 3.8.10 |
| 板端 GLIBC | 2.31，编译产物最高要求 2.17 |
