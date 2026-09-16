<!-- Phase 1 — native 层（C++ 核心）
□ image_rb.h/.cpp：固定容量 300 帧，支持 read_latest、read_by_timestamp
□ host_input_rb.h/.cpp：固定容量 128 事件，read_latest、read_all
□ moonlight_adapter.h/.cpp：LiStartConnection、视频接收线程、主机输入接收线程
□ decoder_ffmpeg_rk.cpp：H.264/H.265 硬解，输出 YUV
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
Phase 3 — Python Agent Core
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
□ ipc/zmq_pub.py：绑定 0.0.0.0:5555
□ ipc/zmq_sub.py：连接 PC 192.168.1.100:5556
□ config/loader.py + config/schema.py：YAML 加载 + 校验
□ main.py：装配所有组件，启动 asyncio 事件循环
□ 单元测试：state_machine、chat_bus、tool_router、config_loader 全绿
Phase 4 — GUI（PC 侧）
□ gui/app/main.py：PySide6 主窗口
□ widgets/wallpaper_panel.py：壁纸展示 + 手动换一张
□ widgets/chat_panel.py：LLM 输出 + 对话输入框 + 键盘捕获
□ widgets/control_panel.py：模式切换、快捷键配置、LLM 模式选择
□ widgets/bilibili_player.py：GAME 模式播放器 + 播放列表
□ services/zmq_client.py：SUB 板端 5555，PUB 本机 5556
□ services/input_capture.py：Qt 键盘事件 → Chat Input Bus
□ GUI 单测：zmq_client、input_capture
Phase 5 — 双机联调
□ scripts/deploy.ps1：交叉编译 → scp → 板端 health_check.sh
□ scripts/run-board-tests.ps1：通过 SSH 在板端跑 pytest
□ ZeroMQ 双向通路验证：板端 PUB → PC SUB，PC PUB → 板端 SUB
□ Moonlight 连接验证：板端 moonlight.start → Sunshine 主机
□ send_key 端到端验证：板端调用 → Windows 主机锁屏
□ Image RB 实流验证：板端读到真实解码帧
□ Host Input RB 实流验证：主机原生键盘事件被板端读到
Phase 6 — 工具层
□ tools/base.py：工具基类（name、schema、execute、权限）
□ tools/lockscreen.py：send_hotkey(["META","L"])，仅 STUDY 未完成时
□ tools/netease_music.py：cloud-music-mcp 集成，动态歌单、批量加歌、URL Scheme
□ tools/bilibili.py：搜索、order=play 排序、顺序播放、反馈切换
□ tools/wallpaper.py：按 LLM 编排序列切换
□ 每个工具的单元测试（mock 外部依赖）
Phase 7 — 固化与优化
□ GitHub Actions host-ci.yml：lint + host 单测 + 交叉编译检查
□ release.yml：tag 触发，产出 .so + agent/ 归档
□ ADR 补齐：每个关键决策一份
□ docs/deploy.md：部署、回滚、版本对齐
□ 性能基线：NPU 推理延迟、解码延迟、端到端延迟写入 bench.sh 输出
□ 板端 systemd/agent.service 开机自启
□ 崩溃日志落盘 + 重启策略