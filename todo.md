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
□ **【GUI 待删除】** 主机输入（host input）相关内容：moonlight-common-c 没有「主机 → 客户端」
  输入接收 API，本方向已从 Phase 6 删除（C2/C3）。GUI 里凡涉及主机输入的 UI 入口 /
   命令 / 文案 / 信号，需一并移除或明确标注「不提供」（对应 Phase 6 的 C4）
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

Phase 6 — 双机联调（方案已评审通过；决策记录见本节末尾）

-- A 基础设施（先做 A0：它是所有同步动作的前提）--
■ A0 板端 git 状态核实：弄清 llm/ sig/ net/ runtimes/ 是「已跟踪 / 被忽略 / 未跟踪」，
     再决定同步方式是 git pull 还是 scp（否则后续同步可能覆盖板端工作）
     （已核实；板端 origin 已改用 SSH + core.sshCommand=ssh -4）
■ A1 scripts/deploy.ps1 重写：build → scp(.so + libmoonlight-common-c.so + config) → 板端 health_check.sh
     （现版本坏：部署到 agent 子目录会拷成 agent/agent、不传 libmoonlight、无 health check、ANSI 乱码）
     （完成 c707243）
■ A2 scripts/health_check.sh（新，板端侧）：import agent_native / --check-config / 关键文件指纹
     （完成 c707243）
■ A3 scripts/run-board-tests.ps1：SSH 到板端跑 Python 测试（板端已装 pytest + pytest-asyncio）
     （完成 d858fbe；顺带修掉它首次跑出来的 3 个板端测试失败）
■ A4 scripts/sync-gui.ps1：gui/ 同步到板端 + 板端本地 cmake 重编（GUI 是板端本地构建）
     （完成，本提交；含三方冲突护栏，-Test 可跑板端 ctest 16/16）

-- B 打通连接（最硬的堵点：握手层是纯 HTTP，配对再好也连不上）--
■ B1 握手改走 HTTPS 47984 + 客户端证书 —— **实现放在 Python**（已评审：不在 C++ 里引 OpenSSL）
     agent/net/sunshine_client.py 负责 /serverinfo /applist /launch /resume；
     native 新增 moonlight.start_with_session() 只接字段，一次 HTTP 都不发；
     launch 查询串追加 LiGetLaunchUrlQueryParameters()（由绑定导出，不抄一份进 Python）。
     主机上已有别的客户端时 /launch 回 400，自动退到 /resume 加入同一会话（共存）。
     （完成 15fb6a3 + 6251474 + c32c8b3；实测对照与分层见 docs/sunshine-pairing-findings.md §5.1）
     （原描述"moonlight_connection 改 HTTPS"是当时的写法，实际按评审结论改在 Python 侧；
       那条明文 HTTP 路径保留给无 TLS 的 GFE 主机，是否删除等 B3 板端跑通后再定）
■ B2 板端配对落地：部署 creds/ + 校验授权名单 + 重启 Sunshine + verify-authorized.sh 对照
     creds/ 已部署到板端 (/home/kickpi/myproject/assitant/creds/)，client.pem/key 的 sha256
     与 PC 上一致；板端 live config.yaml **本来就配了** sunshine.cert/key，所以无需改配置
     （https_port 缺省由代码按 47984 处理）。
     授权名单（D:\tool\sunshine\config\sunshine_state.json）：agent-native 在列且 enabled，
     cert 长度 1208 = 我们的 client.pem；另有 Anorak(1013)×2、ALN-AL00(1021)、Anorak_TV(1021)
     —— 按决策 6 不清理。
     板端验证：verify-authorized.sh（curl，用 CLIENT_CERT/CLIENT_KEY 覆盖路径）200/200，
     /launch 400 "An app is already running"（主机上确有另一个 Moonlight 客户端）；
     我们的 SunshineClient 从板端拿到 PairStatus=1 与 sessionUrl0=rtsp://192.168.137.1:48010（走 /resume）。
     ⚠ **重启 Sunshine 没做成**：它是 Windows 服务 SunshineService（Automatic/Running），
       本会话无管理员权限，Restart-Service 报 "Cannot open SunshineService service"。
       实测这次重启并非必需（名单早已生效：PC 与板端都拿到 PairStatus=1），所以没有为此
       卡住；需要重启时请用管理员权限执行 `Restart-Service SunshineService`。
     安全副作用（顺手补掉）：板端 .gitignore 停在 gui 分支的老版本，没有 *pem/*key 规则 ——
       私钥只差一个 `git add -A` 就会入库。已在板端 .gitignore 补上 creds/ + *.pem + *.key，
       main 的 .gitignore 也补了 creds/（PC 的 creds 目录还有 client.der/clientcert.hex，
       那两类不在 *.pem/*.key 覆盖内）。
■ B3 Moonlight 连接验证：板端 moonlight.start → Sunshine 主机
     两段式实测（2026-09-21，与 agent/main.py::_start_native 同一调用顺序）：
     握手 /resume 0.15s → start_with_session **True** 0.07s；
     日志 `decoder ready: rkmpp [硬件] codec=H.265 1280x720`；
     主机侧 `New streaming session started [active sessions: 2]` + 单独一条 `hevc_nvenc`
     —— 与另一台 Moonlight 客户端**各自独立会话/编码器**并存，对方那路未被改动。
     另一重要更正：/resume 不是"加入对方那一路流"，而是"给我一路自己的会话"，
     已改 docs/sunshine-pairing-findings.md §5.1。
■ B4 moonlight.status() 返回 connected
     同一探针：status() → state=streaming / connected=True / error=''；
     stop() 后干净回到 state=idle（主机侧对方仍是 SUNSHINE_SERVER_BUSY）。
■ B5 Image RB 实流验证：板端读到真实解码帧
     8s 采样（每 0.5s）：frames_pushed 5→148，image_rb.size() 同步增长，overruns=0，
     image_frames_dropped=0；帧为 (256,256,3) uint8、单帧约 1000 种颜色；
     存成 PNG 后**肉眼确认是主机桌面**（主机桌面 1440x2160 竖屏，按我们的 720p 请求
     pillarbox —— 中间约 37% 宽的内容带与 720×1440/2160÷1280 吻合）。
     解码器：`rkmpp [硬件] codec=H.265 1280x720`（Sunshine 为我们单独开了 hevc_nvenc）。
     帧率约 18fps 而非请求的 60：桌面近乎静止（相隔 4.5s 两帧仅 6.9% 像素不同），
     编码侧按"内容有变化才发"工作 —— 非解码问题；"持续 60fps"归 Phase 8 性能基线。
     docs/decoder-mpp.md §8.4 已由"还没验到"改写为实测结果，并新增 §8.5 列出仍未验的部分
     （10bit/4:4:4/AV1 的拒绝路径、H.264 实流、`timestamp_ns` 仍写 0 导致 read_by_timestamp 无意义）。

-- C 输入方向 --
■ C1 send_key 端到端验证：板端调用 → Windows 主机动作
     客观验证（不依赖看图）: 读主机键盘锁定状态
       发键前 CapsLock=False → 板端 InputSender.send_hotkey(["capslock"]) → True
       再发一次 → False（恢复原状、无残留）；发送本身 6~10ms。
     视觉验证: 板端发 WIN (send_hotkey(["meta"])) → 全分辨率主机截图里出现 Win11 开始菜单
       （搜索框「搜索应用、设置和文档」/ 已固定 Edge·Word·Excel…/ 推荐的项目），
       再发一次后确认关闭，主机保持干净（没留下菜单开着）。
     ⚠ 第一次用"我们自己的 256x256 流"判读是**失败**的：三帧对比是单向变化（A→B 0.90、
       B→C 0.12、A→C 0.89），不符合"开了又关"的形态。原因见 C1b —— 开始菜单开在**主屏**
       DISPLAY1，而我们的流是副屏 DISPLAY15，菜单压根没进画面。教训：验证 UI 动作要看
       对应那块屏，或用可查询的宿主状态。
     ■ 锁屏（决策 5 那条"最后单独做"，前面几轮我们口头叫它 C2）：**无法通过注入输入完成**
       —— Windows 平台限制，不是实现缺陷。证据链：
         ① 板端 WIN+L（小写 l）没锁 —— 但那一下还夹着一个真 bug（见下方 resolve_key）；
         ② 板端 WIN+L（正确的 VK 0x4C）**仍然没锁**；
         ③ 板端 WIN+D **成功**（桌面显示 384977→2060448 B，再按一次字节数**完全还原**）
            => Win 组合键整体是通的，单单"锁屏"这个动作不行；
         ④ **主机本机**合成 Win+L（keybd_event，完全不经 Sunshine）**也没锁**
            => Windows 把"合成输入"的锁屏组合过滤掉了（与 Ctrl+Alt+Del 同类安全动作）。
       影响：Phase 7 的 `tools/lockscreen.py` 若按 `send_hotkey(["META","L"])` 实现**不可能生效**；
       需要另一条路（板端 → 主机目前没有反向命令通道，可考虑宿主机侧放个小助手）。
       ⚠ **待你定**：这个功能是砍掉，还是加一个宿主机侧机制 —— 属 Phase 7 设计。
■ 【C1/C2 期间修掉】resolve_key 对小写单字符直接 ord() -> 发错键
     Windows 的字母 VK 是大写 ASCII；`ord('a')`=0x61 其实是 VK_NUMPAD1，
     于是 `send_key("ctrl","c")` 会变成 Ctrl+数字键盘3（"复制"根本不是复制）。
     修法：单字符先 `.upper()` 再 `ord()`；数字与标点本来没这个问题（'7'=0x37、'/'=0x2F）。
     同时改掉 tests/test_io.py 里**固化错误行为**的断言（原来断言 `resolve_key("a") == 97`），
     并补一条"小写字母到 native 必须是大写 VK"的端到端断言。test_io 61 项全绿。
■ C1b 【C1 期间发现】ROI → 主机鼠标坐标在 pillarbox 下失真
     Sunshine 抓的是副屏 DISPLAY15（物理 1440x2160 竖屏，sunshine.conf output_name 指定），
     而我们的流按 1280x720 请求 → 画面是"竖屏内容 + 左右黑边"，内容带只占约 37% 宽
     （与 720x1440/2160/1280 吻合，B5 实测）。
     但 preprocess 把**整帧**（含黑边）缩到 256x256，send_mouse 又把这个 256x256 原样
     作为参考平面交给 LiSendMousePositionEvent（宿主按 0..1 归一化映射到显示器）。
     后果：中心点仍然对得上，但偏离中心的点击在水平方向会偏 —— **看见的像素与点到的
     位置不是同一个坐标系**。键盘不受影响（C1 因此全绿）。
     建议：请求与显示器同比例的流（例如 720x1080），或让 Agent 按黑边补偿后再算 ROI。
□ C2 ~~Host Input RB 实流验证：主机原生键盘事件被板端读到~~ **【已删除】**
     原因：moonlight-common-c 没有任何「主机 → 客户端」输入接收 API（只有 LiSendKeyboardEvent
     等发送方向），原设计假设不成立，见 docs/sunshine-pairing-findings.md 同期调研
□ C3 ~~Host Input Bus 三源合并验证（终端 / GUI / Host RB）~~ **【随之取消】**
     第三源不存在；若要改成「终端 + GUI 两源」需另行讨论
□ C4 GUI 侧删除主机输入相关内容（Phase 5 的 input_capture 之外，凡涉及「主机输入」的
     UI 入口 / 命令 / 文案一并移除）—— 改动在 GUI，需与 GUI 侧同步

-- D 状态与命令 --
■ D1 IPC 命令格式统一为 GUI 实际实现：{"action": str, "payload": object}，一行一条
     改动：protocol.py 新增 ACTION_FIELD/PAYLOAD_FIELD + encode_command/decode_command
     （两种信封共用的前半段抽成 _parse_object，免得两处各写一遍后漂移）；
     local_server 的入方向改用 decode_command；local_client 改发 GUI **逐字节同款**信封；
     测试新增 15 条命令信封用例（字节级断言 / 字段顺序 / 无 timestamp / 拒收老格式 / 各种坏形状）。
     ⚠ 修之前的实况：server 只认 {"topic","data","timestamp"}，而 GUI 发 {"action","payload"}
       —— **GUI 的每一条命令都会被当坏行丢掉**（这正是 D5/D6 的拦路石）。
     刻意的取舍：**只认一种格式**。topic 形态的命令明确拒收，并有用例钉住
     （test_topic_shaped_command_is_dropped）—— 双格式兼容就等于"文档说 A、代码也收 B"。
     验证三方：PC 整套 exit 0（protocol 66 OK）；WSL 14 文件 OK（pytest 23 passed）；
     板端 protocol 66 / local_server 77 / main 47 / pytest 23 全 OK。
     （第一次板端验证**无效**：deploy 走 HEAD 而 D1 当时未提交，板端跑的是旧代码 ——
       靠"test_ipc_protocol 只跑 51 条 vs PC 66 条"看出来，单独送文件重跑才是上面这份。）
     文档仍写 topic 形态 —— 属 D3，本次没有跳步去改。
■ D2 修 switch_mode 的键：payload 里是 value（不是 mode）
     处理器原来读 `payload["mode"]`，而 GUI 与 docs 给的都是 `{"value": ...}`
     —— 也就是 D1 把信封修对之后，switch_mode 仍然一个都认不出来。
     改动：`agent/ipc/__init__.py` 改读 `value`，且缺 `value` 时给一条**点名字段**的警告
     （不静默读不到就算了）；顺带把几条测试里的 `{"mode": ...}` 命令 payload 改成 `value`。
     新增 `test_switch_mode_reads_the_value_key`：用日志把两条路分开 ——
     给 `value` 会走到「收到 switch_mode」，给老键 `mode` 只会得到「缺少 value 字段」。
     （踩过一次坑：日志里打的是**内部**模式名，`mode_from_wire("STUDY") -> "study"`，
       第一版断言写了大写所以红了。）
     三方验证：PC 整套 exit 0；WSL 14 文件 OK（pytest 23 passed）；
     板端 test_ipc_local_server 78 OK / pytest 23 passed。
     注意：status **推送**里的 `mode` 是反方向的另一个字段（本来就该叫 mode），没有动它。
■ D3 三份文档同步（命令信封改成 GUI 的实际实现）
     · docs/ipc-protocol.md：
         §2 重写为**两个方向的信封**（推送 {topic,data,timestamp} / 命令 {action,payload}，
            命令方向没有 timestamp），并写明"不做双信封兼容"；
         §4 命令表改成 action/payload，`switch_mode` 那行点明**键是 value 不是 mode**
            （mode 是 §3 里 status 推送的字段，方向不同）；
         §5 补了命令方向的**逐字节示例**（52 字节，中文原样 UTF-8）；
         §6 错误表按方向拆开（缺 action / 缺 payload / **信封发错方向** 各一行）；
         §7 Python 与 C++ 两侧的实现要点都按新信封改写；
         §8 常量表补 ACTION_FIELD / PAYLOAD_FIELD；
         §10 顺手改掉一处过时说明：GUI 那行原写「**尚未实现**」，其实 gui/ 里早就是真代码了。
     · docs/gui-agent-integration.md：
         §1 传输表新增一行「信封：两个方向不一样」，避免读者从中间看起时混用；
         §3 那句「与 protocol §4 一致」原来**是错的**（protocol §4 当时写的是 topic 形态），
            现在两边真的对齐了 —— 改成可核对的说明，并留下 D1/D2 的历史提醒（勿再退回：
            Agent 侧曾收 topic 信封 + 读 payload["mode"]，导致 GUI 的每条命令被整条丢掉）。
     · gui/src/services/local_client.cpp：sendCommand 上方的注释原文写着
        "这**不是** docs §4 的信封 … 若将来要对齐 Agent 的 decode_full()，两边需要统一成
        topic/data/timestamp" —— 方向已经反了（是 Agent 对齐 GUI）。改成说明协议本身就是
        两个方向两种信封，并点明 D1 之后 Agent 由 decode_command() 收这个信封。
     另外加了一条测试 test_documented_byte_example_is_exact：把文档 §5 里那 52 字节
     钉在代码上，免得以后改了格式、文档里的 hex 变成骗人的。
     验证：test_ipc_protocol 67 OK；PC 整套 exit 0；板端 GUI 重新编译 **BUILD_OK**、
     `ctest -R local_client` **1/1 passed (9.21s)**（这个 C++ 用例正是拿真 server 核对
     action/payload 字段的那个）。
■ D4 build_ipc(bus, config, runtime=None) 接线出方向推送（status / llm）
     原来的缺口：出方向**根本不存在** —— `handle_event()` 算出来的回复被主循环直接丢掉，
     状态变化也没人告诉 GUI（build_ipc 只有入方向）。
     改动：
       agent/ipc/__init__.py: build_ipc 收可选 runtime；给了就 `_wire_outbound()`：
         · `state.on_change` -> push `status{mode(大写), connected}`
         · `runtime.on_reply` -> push `llm{text}`
         状态回调是**同步**的、可能来自别的线程，而 push() 是协程 —— 所以建好时抓住
         loop，用 `run_coroutine_threadsafe` 投回去；server 没跑就静默跳过
         （`push()` 返回 0 本来就不是错误）。**不给 runtime 时行为与 D4 之前完全一样。**
       agent/main.py: 新增 `on_reply` 钩子（默认 None，无 IPC 时行为不变），
         `handle_event()` 在有非空回复时调它（支持 async 钩子；钩子自己出错只记 warning，
         不影响"这条回复已经算成功"）；`_call_ipc_factory()` 只在 factory **签名接受**
         runtime 时才传 —— 用签名探测而不是 try/except TypeError，后者会把 factory
         内部真正的 TypeError 也当成签名不符、再调一次（重复副作用）。
     测试 +12：IPC 层 6 条（两个方向都接上了 / 不给 runtime 就只接入方向 / NullServer
     接线不炸 / 状态变化真的推到连着的 GUI / 回复真的推到连着的 GUI / stop 之后再推被忽略）、
     runtime 层 6 条（钩子拿到回复 / async 钩子被 await / 钩子抛错不影响回复 /
     接线前是 None / start 后有人接 / LLM 失败不推空 llm）。
     ⚠ 其中一条**我自己写错了**：断言 `rt.start()` 之后 `on_reply is None` —— 但 D4 的接线
       正是在 IPC 启动时发生的（Windows 上 NullServer 也接）。已改成两条如实的：
       接线前是 None、start 之后 callable。
     三方验证：PC（test_main 53 OK / test_ipc_local_server 84 OK，整套 exit 0）；
     WSL（84 **0 skipped** —— 那几条"真的推到连着的 GUI"是走真 socket 的）；
     板端（84 OK / 53 OK）。
     本次**没做**：`switch_mode` 还没驱动状态机（处理器仍是"状态机还没接进来，已忽略"），
     所以状态推送目前只能由直接 transition 触发。命令 -> 状态机那一步放 D5/D6。
□ D5 Agent 状态推送到 GUI 验证
□ D6 GUI 命令到 Agent 验证（switch_mode 往返 / chat_input 往返）
□ D7 未接线的命令（next_wallpaper / next_bilibili）回推一条 llm 说明「功能未接入」
     （下游属 Phase 7；已确认可接受，避免 GUI 上点了没反应）

Phase 6 决策记录（已评审）
1. IPC 命令格式**以 GUI 实际实现为准**：{"action","payload"}
2. Host Input 方向**删除**（moonlight-common-c 无此能力），三源合并随之取消
3. build_ipc 签名扩为 (bus, config, runtime=None)，保持向后兼容
4. 板端**安装** pytest + pytest-asyncio（不再只跑 unittest）
5. send_key 端到端先用**不锁屏**动作验证，锁屏最后单独做
6. 不清理 Sunshine 侧历史遗留（重复证书条目 / min_log_level=debug）
7. next_wallpaper / next_bilibili 本期只做「收到 + 回推说明」
8. **Agent 只读 config/config.yaml**：不读 gui/config/gui.yaml，也不读 llm/config/llm.env

项目归一化处理（新增，与 Phase 6 并行）
□ 唯一真源收敛：命令信封以实际实现为准后，docs/ipc-protocol.md 必须同步改，避免"文档说 A、代码做 B"
□ 配置入口收敛：只有 config/config.yaml 是 Agent 的运行时配置；
     gui/config/gui.yaml 与 llm/config/llm.env 不再被 Agent 读取 —— 需明确「谁写、谁读」，
     否则 GUI 模型测试页写三份、Agent 只认一份，改了不生效
□ 清理并存实现：agent/ipc/server.py（收 {action,payload}）与 local_server.py（按 protocol 收 topic）
     两份 server 现都躺在仓库里且格式不同 —— 二选一，另一份删除或明确标注废弃
□ 板端 ⇄ PC 同步机制固化：明确 git pull 与 deploy.ps1 各自负责什么，杜绝"板端落后好几个提交却没人发现"
□ 文档去重：gui-agent-integration.md §3 与 ipc-protocol.md §4 内容重叠且互相矛盾，合并到一处

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