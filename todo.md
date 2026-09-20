<!-- Phase 1 — native 层（C++ 核心）
□ image_rb.h/.cpp：固定容量 300 帧，支持 read_latest、read_by_timestamp
□ host_input_rb.h/.cpp：固定容量 128 事件，read_latest、read_all
□ moonlight_adapter.h/.cpp：LiStartConnection、视频接收线程、主机输入接收线程
□ decoder.h/.cpp + decoder_mpp.cpp：H.264/H.265 硬解（Rockchip MPP），输出 YUV
□ preprocess.cpp：ROI 裁剪 + 缩放 256×256 + RGB888
□ input_sender.h/.cpp：封装 LiSendKeyboardEvent、LiSendMouseEvent
□ host 单测：test_ring_buffer、test_image_rb、test_host_input_rb 全绿
□ 交叉编译通过：scripts/build-native.ps1 产出 libagent_native.a -->
<!-- Phase 2 — pybind11 绑定
□ binding/module.cpp：定义 agent_native 模块
□ bind_moonlight.cpp：moonlight.start/stop/status
□ bind_image_rb.cpp：image_rb.read_latest/read_by_timestamp/size/capacity，返回 numpy
□ bind_host_input_rb.cpp：host_input_rb.read_latest/read_all/size/capacity
□ bind_input_sender.cpp：send_key/send_mouse/send_hotkey，全部加 py::call_guard<py::gil_scoped_release>
□ 交叉编译通过，产出 agent_native.cpython-38-aarch64-linux-gnu.so
□ file 命令确认 ELF aarch64
□ host 编译一份 .pyd/.so 用于 Python 单测（mock 掉 moonlight 连接） -->
<!-- Phase 3 — Python Agent Core
□ io/image_reader.py：包装 image_rb，asyncio 友好（run_in_executor）
□ io/host_input_reader.py：轮询 host_input_rb，投递到 Chat Input Bus
□ io/chat_bus.py：asyncio.Queue + 统一事件格式（source/text/timestamp）
□ io/input_sender.py：包装 send_key/send_mouse，提供 async 接口
□ core/state_machine.py：SLEEP ⇄ IDLE ⇄ STUDY ⇄ GAME
□ core/scheduler.py：日程检查、定时触发、快捷键监听
□ core/tool_router.py：工具注册、权限控制、执行调度
□ llm/provider.py：edge/cloud/disabled 三模式切换
□ llm/rule_engine.py：disabled 时的兜底规则
□ vision/siglip_encoder.py：RKNN-Toolkit-Lite2 推理封装（先 mock）
□ vision/roi.py：ROI 配置解析
□ config/loader.py + config/schema.py：YAML 加载 + 校验
□ main.py：装配所有组件，启动 asyncio 事件循环
□ 单元测试：state_machine、chat_bus、tool_router、config_loader 全绿 -->
<!-- Phase 4 — IPC 层
□ ipc/protocol.py：消息格式定义（topic 常量、编解码）
□ ipc/local_server.py：Unix socket server，asyncio.start_unix_server
□ ipc/local_client.py：Agent 侧测试客户端
□ docs/ipc-protocol.md：topic 与 command 规范
□ Python 侧往返测试：server push → client 收到
□ C++ 侧 local_client.h/.cpp：QLocalSocket 封装
□ C++ 侧自动重连（断线后 1s 重试）
□ C++ 侧换行分隔 JSON 解析
□ C++ 侧信号：statusReceived / llmReceived / wallpaperReceived / musicReceived
□ C++ 侧方法：sendCommand(action, payload)
□ 板端跑通：Agent push status → C++ 收到
□ 板端跑通：C++ 发 switch_mode → Python 收到
□ 单测：ipc 往返（PC 上跑） -->

<!-- Phase 5 — GUI（板端 C++ Qt5）
□ gui/CMakeLists.txt：Qt5 + AUTOMOC
□ gui/src/main.cpp：入口
□ gui/src/main_window.h/.cpp：主窗口骨架
□ gui/src/services/local_client.h/.cpp：Unix socket 客户端（Phase 4 完成）
□ gui/src/services/input_capture.h/.cpp：Qt 键盘事件 → Command
□ gui/src/widgets/wallpaper_panel.h/.cpp：壁纸显示 + "换一张"按钮
□ gui/src/widgets/chat_panel.h/.cpp：LLM 输出（只读 QTextEdit）+ 输入框（QLineEdit）
□ gui/src/widgets/control_panel.h/.cpp：模式切换（QButtonGroup）+ LLM 模式（QComboBox）
□ gui/src/widgets/bilibili_player.h/.cpp：播放列表（QListWidget），不内嵌播放器
□ 主窗口装配：壁纸区 + 对话区 + 控制区 + Bilibili 区
□ 连接 LocalClient 信号到各 panel
□ 各 panel 信号连到 LocalClient::sendCommand
□ 窗口尺寸 1920×1080
□ 板端本地编译通过
□ 窗口正常显示
□ Agent 切状态，GUI 更新
□ GUI 点"换一张"，Agent 收到命令
□ GUI 单测：local_client、input_capture（QT_QPA_PLATFORM=offscreen） -->

Phase 6 — 双机联调
□ scripts/deploy.ps1：交叉编译 → scp → 板端 health_check.sh
□ scripts/run-board-tests.ps1：通过 SSH 在板端跑 pytest
□ scripts/sync-gui.ps1：PC 源码同步到板端（如果 GUI 在 PC 侧开发）
□ Sunshine 配对流程（解决 PairStatus=0）
□ Moonlight 连接验证：板端 moonlight.start → Sunshine 主机
□ moonlight.status() 返回 connected
□ Image RB 实流验证：板端读到真实解码帧
□ Host Input RB 实流验证：主机原生键盘事件被板端读到
□ send_key 端到端验证：板端调用 → Windows 主机锁屏
□ Host Input Bus 三源合并验证：终端 / GUI / Host RB
□ Agent 状态推送到 GUI 验证
□ GUI 命令到 Agent 验证

Phase 7 — 工具层
□ tools/base.py：工具基类（name、schema、execute、权限、allowed_states）
□ tools/init.py：工具注册入口
□ core/tool_router.py：注册、权限、调度（Phase 3 已建骨架，此处完善）
□ tools/lockscreen.py：send_hotkey(["META","L"])，仅 STUDY 未完成时
□ tools/netease_music.py：cloud-music-mcp 集成，动态歌单、批量加歌、URL Scheme
□ tools/bilibili.py：搜索、order=play 排序、顺序播放、反馈切换
□ tools/wallpaper.py：按 LLM 编排序列切换
□ 每个工具的单元测试（mock 外部依赖）
□ tests/test_tools.py：工具注册、状态权限、参数校验
□ LLM 能调用工具完成任务
□ 工具在正确状态下才允许执行
□ 权限控制生效（如锁屏仅 STUDY 未完成时）

Phase 8 — 固化与优化
□ .github/workflows/host-ci.yml：lint + host 单测 + 交叉编译检查
□ .github/workflows/release.yml：tag 触发，产出 .so + agent/ 归档
□ docs/ipc-protocol.md：Unix socket 协议（Phase 4 同步写）
□ docs/deploy.md：部署、回滚、版本对齐
□ docs/adr/0001-ipc-unix-socket.md：为什么用 Unix socket 而非 ZMQ
□ docs/adr/0002-gui-on-board.md：为什么 GUI 在板端而非 PC
□ docs/adr/0003-ringbuffer-readonly.md：RingBuffer 只在读取侧
□ docs/adr/0004-pybind11-gil-release.md：pybind11 释放 GIL
□ scripts/bench.sh：NPU 推理延迟、解码延迟、端到端延迟
□ docs/bench.md：性能基线记录
□ systemd/agent.service：Agent 开机自启
□ systemd/agent-gui.service：GUI 开机自启（After=agent.service）
□ 崩溃日志落盘 logs/crash/
□ Restart=on-failure 重启策略
□ scripts/monitor.sh：CPU/RAM/NPU 占用快照
□ 检查 Agent + GUI 同跑时是否打架