# Agent 双机开发架构文档

## 1. 总体拓扑

```
┌──────────────── Windows PC (开发机) ────────────────┐
│  DSH (pc-dev 工作区)                                 │
│  · C++ / pybind11 源码                                │
│  · 交叉编译工具链 + sysroot                           │
│  · PySide6 GUI                                       │
│                                                       │
│  DSH (rk3568-dev 工作区, SSH)                        │
│  · 透明访问板端文件与终端                             │
└──────────────────────┬───────────────────────────────┘
                       │ SSH / ZeroMQ / Moonlight
┌──────────────────────▼───────────────────────────────┐
│              RK3568 (Ubuntu 20.04 / aarch64)          │
│  agent_native.so  ←  pybind11 绑定                    │
│  Python Agent Core (asyncio)                         │
│  · Image RingBuffer   ← 视频帧                        │
│  · Host Input RB      ← 主机原生键盘                  │
│  · send_key/send_mouse → moonlight-common-c 直调      │
│  · Chat Input Bus (asyncio.Queue)                    │
│  · ZeroMQ PUB 0.0.0.0:5555                           │
└───────────────────────────────────────────────────────┘
```

---

## 2. 工作区划分

| 工作区 | 位置 | 职责 |
|---|---|---|
| `pc-dev` | Windows 本地目录 | 编写 C++ / Python / GUI，交叉编译，跑 host 单测 |
| `rk3568-dev` | RK3568 `/opt/agent` | 部署产物，运行 Agent，板端调试与测试 |

**规则**：`agent/`、`.so`、`VERSION` 由 PC 部署覆盖，板端不手改；`config/`、`logs/`、`models/` 为板端本地状态，部署不覆盖。

---

## 3. 底层 C++ / pybind11 层

### 3.1 读取侧：两个 RingBuffer

```
Sunshine 视频流
  → moonlight-common-c 接收 RTP
  → Rockchip MPP 硬解 (decoder_mpp.cpp; 详见 docs/decoder-mpp.md)
  → ROI 裁剪 + 256×256 + RGB888
  → Image RingBuffer (lock-free, 300 帧)
  → pybind11: image_rb.read_latest()

Sunshine 回传主机键盘
  → moonlight-common-c 接收
  → Host Input RingBuffer (lock-free, 128 事件)
  → pybind11: host_input_rb.read_latest() / read_all()
```

### 3.2 输出侧：直接调用 API，无 RingBuffer

```
Python Agent
  → pybind11: send_key() / send_mouse() / send_hotkey()
  → LiSendKeyboardEvent / LiSendMouseEvent
  → py::call_guard<py::gil_scoped_release> 释放 GIL
  → Sunshine → Windows 主机执行
```

**原因**：输入事件稀疏（每秒几次到几十次），无需缓冲；直调延迟最低，不阻塞 asyncio。

### 3.3 pybind11 接口

```python
import agent_native

# 连接
agent_native.moonlight.start(host, app, w, h, fps)
agent_native.moonlight.stop()
agent_native.moonlight.status()

# 读取侧 RB
frame  = agent_native.image_rb.read_latest(timeout_ms=0)   # numpy (256,256,3)
events = agent_native.host_input_rb.read_all()

# 输出侧直调
agent_native.send_key(modifier="META", key="L", action="press")
agent_native.send_hotkey(["CTRL", "ALT", "S"])
agent_native.send_mouse(x=100, y=200, action="move")
```

---

## 4. Python Agent Core

精简模块，全部拍平到 `agent/` 一层：

| 文件 | 职责 |
|---|---|
| `main.py` | 装配所有组件，启动 asyncio 事件循环 |
| `io.py` | 包装 image_rb / host_input_rb；Chat Input Bus（三源合一） |
| `state.py` | 状态机 SLEEP ⇄ IDLE ⇄ STUDY ⇄ GAME |
| `scheduler.py` | 日程检查、定时触发、快捷键监听 |
| `router.py` | 工具注册、权限控制、执行调度 |
| `llm.py` | edge(NPU) / cloud(API) / disabled(规则引擎) |
| `vision.py` | SigLIP 推理（RKNN-Toolkit-Lite2） |
| `ipc.py` | ZeroMQ PUB 绑 `0.0.0.0:5555`，SUB 连 PC `:5556` |
| `config.py` | YAML 加载与校验 |
| `tools/` | lockscreen / netease / bilibili / wallpaper |

**Chat Input Bus**：终端 stdin、GUI 键盘、Host Input RB 统一进 `asyncio.Queue`，事件格式：

```python
{"source": "terminal" | "gui" | "host_keyboard",
 "text": "...", "timestamp": 1234567890.123}
```

---

## 5. GUI（PySide6，PC 侧运行）

- `main.py`：主窗口
- `panels.py`：壁纸面板、对话/状态面板、控制面板
- `zmq_client.py`：SUB 板端 5555，PUB 本机 5556

Agent Core 与 GUI 通过 ZeroMQ 跨机通信，PUB 端绑定 `0.0.0.0`。

---

## 6. 数据流总览

| 方向 | 通道 | 机制 |
|---|---|---|
| Sunshine → Python（图像） | 视频流 | 接收 → 解码 → 预处理 → **Image RB** → pybind11 |
| Sunshine → Python（主机键盘） | 主机输入回传 | 接收 → **Host Input RB** → pybind11 |
| Python → Sunshine（输入） | 输入通道 | pybind11 **直调** LiSendKeyboardEvent，释放 GIL |
| 终端 → Agent | stdin | asyncio reader → **Chat Input Bus** |
| GUI → Agent | PySide6 signal | Qt signal → **Chat Input Bus** |
| Host Input RB → Agent | pybind11 读取 | read_latest → **Chat Input Bus** |
| Agent → GUI | ZeroMQ PUB/SUB | 状态变更、LLM 输出、壁纸序列 |

---

## 7. 精简文件树

### 7.1 pc-dev

```
D:\projects\agent\
├── .gitignore
├── README.md
├── cmake/
│   └── toolchain.cmake
├── native/
│   ├── CMakeLists.txt
│   ├── ring_buffer.h
│   ├── image_rb.cpp/.h
│   ├── host_input_rb.cpp/.h
│   ├── moonlight_adapter.cpp/.h
│   ├── decoder.cpp/.h          ← 解码器对外接口 + 后端分发 + FFmpeg 软解
│   ├── decoder_mpp.cpp         ← MPP 硬解后端 (RK3568 上真正用的那条路)
│   ├── decoder_backend.h       ← 后端内部接口 (不属于对外 API)
│   ├── input_sender.cpp/.h
│   ├── binding.cpp
│   └── third_party/
│       ├── moonlight-common-c/
│       └── pybind11/
├── agent/
│   ├── main.py
│   ├── io.py
│   ├── state.py
│   ├── scheduler.py
│   ├── router.py
│   ├── llm.py
│   ├── vision.py
│   ├── ipc.py
│   ├── config.py
│   └── tools/
├── gui/
│   ├── main.py
│   ├── panels.py
│   └── zmq_client.py
├── config/
│   ├── config.example.yaml
│   └── schedule.example.yaml
├── scripts/
│   ├── build.ps1
│   ├── deploy.ps1
│   ├── test-host.ps1
│   └── test-board.ps1
└── tests/
    ├── test_native.py
    ├── test_agent.py
    └── test_gui.py
```

### 7.2 rk3568-dev

```
/opt/agent/
├── agent_native.cpython-38-aarch64-linux-gnu.so
├── VERSION
├── agent/
├── config/
├── models/
├── venv/
├── logs/
├── tests/
└── start.sh
```

---

## 8. 开发流程

### 8.1 日常循环

```
1. git checkout -b feat/xxx
2. 在 pc-dev 改代码
3. scripts/build.ps1          # 交叉编译
4. scripts/test-host.ps1      # PC 侧单测
5. git commit + push
6. scripts/deploy.ps1         # 部署到板端
7. 在 rk3568-dev 跑 test-board.ps1 + 看日志
8. 通过 → 合 main
```

### 8.2 提交规范

Conventional Commits，scope 仅 6 个：`native` / `binding` / `agent` / `gui` / `tools` / `ipc`。

### 8.3 测试分层

| 层 | 位置 | 触发 |
|---|---|---|
| L1 host 单测 | PC | 每次 commit |
| L2 交叉编译 | PC | 每次 PR |
| L3 板端冒烟 | RK3568 | 每次 deploy |

端到端（Moonlight 连接、工具链）手动跑，不进 CI。

---

## 9. 部署流程

```
scripts/deploy.ps1
├── build.ps1（交叉编译）
├── 校验 .so 是 ELF aarch64
├── scp .so + agent/ + VERSION 到板端
├── ssh 板端跑 test-board.ps1
└── 失败 → 从 dist/ 取上一版回滚
```

---

## 10. 关键原则与坑

1. **板端不改代码**：改动一律回 PC，走部署流程。
2. **固件升级后重拉 sysroot + 重编译**，否则 glibc 版本不匹配。
3. **pybind11 交叉编译**：`PYBIND11_PYTHON_VERSION=3.8`，`PYTHON_INCLUDE_DIRS` / `PYTHON_LIBRARIES` 指向 sysroot。
4. **ZeroMQ PUB 绑 `0.0.0.0`**，否则 Windows GUI 连不上。
5. **send_\* 必须释放 GIL**，否则阻塞板端 asyncio。
6. **先跑通，再正规化**：Phase 1+2 打通后立刻做 Phase 5 双机联调，再补 GUI 和工具。