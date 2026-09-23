<!-- ⚠ 下面 Phase 1 / 2 / 3 三个块是**历史计划**, 不是当前状态。其中部分交付物
     已删除或改名 —— 例如 host_input_rb.*(含"主机输入接收线程")以及 binding 的
     host_input_rb 子模块, 已按 Phase 6 决策 2 删除, 详见文末"决策 2 的落地"。
     当前状态以仓库代码与 docs/ 为准。 -->
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
     （原描述"moonlight_connection 改 HTTPS"是当时的写法，实际按评审结论改在 Python 侧。
       Phase 6 收尾时**已把那条明文 HTTP 路径删掉**（native/moonlight_connection.{h,cpp}
       + tests/test_moonlight_connection.cpp，共 20 个用例）：实测它在这台主机上只能拿到
       PairStatus=0 与 /applist //launch 的 404，留着只会让人以为还有一条能用的路。
       现在 native 只有 start_with_session() 一个连接入口。详见下方"C 收尾"条目）
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
       ⚠ **你的决定（2026-09-21）：这个动作从"锁屏"改为"回到主页"**。
         落地方式待明确（见下方"C 收尾"条目）："主页"指的是板端 GUI 的主页面，
         还是宿主机的桌面/浏览器主页？两者实现完全不同，我不猜。
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
     ■ **已决定：不做**（2026-09-21 你的判定"不需要鼠标坐标"）。结论留在这里：
       如果哪天真要用 vision 驱动点击，必须先解决这件事（改流比例 或 按黑边补偿），
       否则点偏是必然的。代码里的 send_mouse 路径未动。
□ C2 ~~Host Input RB 实流验证：主机原生键盘事件被板端读到~~ **【已删除】**
     原因：moonlight-common-c 没有任何「主机 → 客户端」输入接收 API（只有 LiSendKeyboardEvent
     等发送方向），原设计假设不成立，见 docs/sunshine-pairing-findings.md 同期调研
□ C3 ~~Host Input Bus 三源合并验证（终端 / GUI / Host RB）~~ **【随之取消】**
     第三源不存在；若要改成「终端 + GUI 两源」需另行讨论
□ C4 GUI 侧删除主机输入相关内容（Phase 5 的 input_capture 之外，凡涉及「主机输入」的
     UI 入口 / 命令 / 文案一并移除）—— 改动在 GUI，需与 GUI 侧同步
     ■ **你的决定（2026-09-21）：本期不做。**

-- C / B 收尾（2026-09-21 你的决定，逐条落地）--
■ 删除 native 的明文 HTTP 握手层（原 B1 遗留的"方案 C：等 B3 跑通再定"）
     B3 已在板端跑通 → 删掉：native/moonlight_connection.{h,cpp} + tests/test_moonlight_connection.cpp
     （20 个用例）。同步改：moonlight_adapter.{h,cpp}（只剩 prepare_start + start_with_session +
     connect_limelight，去掉 HTTP 分支与 unique_id）、binding.cpp（去掉 moonlight.start 绑定与头注释）、
     native/CMakeLists.txt（去掉源文件与 ws2_32 链接）、tests/CMakeLists.txt（去掉该测试目标）、
     tests/mocks/mock_agent_native.py（替身改为 start_with_session）、文档四处
     （sunshine-pairing-findings §5.1、architecture、decoder-mpp、Readme 目录树）与
     agent/{main.py,net/sunshine_client.py} 的注释。
     验证：host ctest **193/193 passed**（213 − 20，正好是被删的那 20 条）；
           交叉编译 exit 0（AArch64 / GLIBC 2.17）；PC 整套 exit 0。
■ 修掉 authorize_client.py 的两个毛病
     · 默认 state.json 按平台选：Windows `D:\tool\sunshine\config\sunshine_state.json` /
       WSL `/mnt/d/tool/sunshine/config/sunshine_state.json`，另支持 `SUNSHINE_STATE` 覆盖；
       缺文件时给出可操作提示（原来 Windows 上直接 FileNotFoundError）。
     · cp936 控制台打印含 `⚠` 的文档会 UnicodeEncodeError → stdout/stderr 只放宽 `errors`
       不动 encoding（中文照常，`⚠` 变 `?`）。
     实测：PC 走 D:\、WSL 走 /mnt/d/，读到的都是同一份 5 条设备；无参数打印用法不再崩。
     （顺手修掉自己引入的 SyntaxWarning：docstring 里的 Windows 路径要转义反斜杠。）
     · 同类但**未动**：verify-authorized.sh / pair_*.py 仍是 WSL 写法 —— 它们在本项目里
       是"排查期工具"，板端用 env 覆盖即可（B2 就是这么跑的），不属本次要求。
■ 锁屏 → **改为"回到桌面"**（2026-09-21 你的决定："从锁屏改为回到主页" → 宿主机回到桌面）
     机制已就位：`agent/io/input_sender.py::InputSender.show_desktop()` = `send_hotkey(["meta","d"])`
     （WIN+D）。测试 +2（发出的码是 [0x5B, 0x44]，且**不含**锁屏键 0x4C）。
     板端实测：主机桌面确实显示出来（截图 1718395 → 2060448 B，与 C1 那次 WIN+D 的特征尺寸一致），
     再按一次窗口回来了。**但**两次里有一次没落地、还原也不是字节级精确 —— 详见 Phase 7 那条的 ⚠。
     Phase 7 的对应条目已从 lockscreen.py 改成 back_to_desktop.py。

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
■ D5 Agent 状态推送到 GUI 验证（含 switch_mode 真正驱动状态机）
     先补上缺的那一步：switch_mode 之前只记一条"状态机还没接进来"就丢掉。
     现在 `_make_command_handler(bus, runtime, push)` 拿到 runtime 后：
       · 合法转换 -> `state.transition(mode, "ipc: switch_mode")`；推送交给 D4 的 on_change
       · 非法转换 -> 状态机返回 False：**拒绝 + 把当前真实状态推回给 GUI**
         （docs §4 承诺过这件事，之前是空话）—— 否则界面会停在它自己乐观切过去的状态上
       · 没给 runtime -> 说清是"没有状态机"，不静默
     顺带抽出 `_make_dispatcher()` 与 `_status_data()`：让"状态变化推送"与"非法转换纠正推送"
     共用同一份载荷构造，免得两处慢慢漂移。
     板端活体验证（真 Runtime + 真主循环 + LocalClient 当 GUI）：
       1) switch_mode(STUDY)      -> status{mode: STUDY}，板端 state=study
       2) switch_mode(GAME)(非法) -> 日志"被状态机拒绝 (当前 study)，把真实状态推回给 GUI"
                                    -> status{mode: STUDY}，状态未变
       3) chat_input("现在几点")   -> llm{text: "现在是 2026-09-21 18:57:59。"}
       这条把整链走通了：命令 -> socket -> bus -> **主循环** -> handle_event -> 规则引擎
       -> on_reply 钩子 -> push llm。
     （第一次探针第 3 步超时，是因为我**没起主循环** —— bus 只有 `Runtime.serve()` 会消费；
       补上 serve() 立刻通过。是探针不全，不是产品问题。）
     测试 +4（合法转换真的切状态 / 非法转换推真实状态 / 无 runtime 不炸 / 真 socket 往返）；
     三方：PC 整套 exit 0；WSL 14 文件 OK（TestBuildIpc 18 条全跑，含真 socket 往返）；
     板端 test_ipc_local_server 88 OK + 上面那次活体往返。
■ D6 GUI 命令到 Agent 验证（switch_mode 往返 / chat_input 往返）
     板端有真显示（Xorg :0 + XFCE + /dev/dri，Qt 的 libqxcb 在），GUI 二进制也在，
     所以用的是**真 GUI**（不是替身）。GUI 自带验收开关，正好干这事：
       · chat_input 往返：`--chat-demo "现在几点"` **走真实输入框 + 发送按钮**，
         再 `--screenshot` 存下窗口图。GUI 日志与截图都拿到了：
             [ipc] 发送: {"action":"chat_input","payload":{"text":"现在几点"}}
             [recv] llm {"text":"现在是 2026-09-21 19:04:38。"}
         截图里能看见：左上角绿点「已连接」、右侧「切换模式」面板、
         用户气泡「现在几点」与助手气泡「现在是 2026-09-21 19:04:38。」——
         推送确实渲染到了控件上。
       · switch_mode 往返：`--stdio` 模式（`@<action> <json>` 语法）连真 Agent：
             [ipc] 发送: {"action":"switch_mode","payload":{"value":"STUDY"}}
             [recv] status {"connected":false,"mode":"STUDY"}
             [ipc] 发送: {"action":"switch_mode","payload":{"value":"GAME"}}   (非法)
             [recv] status {"connected":false,"mode":"STUDY"}   <- 被拒后把真实状态推回来
         板端 Agent 的 state 从 idle 变成 study ✓（`stats: events=1 replies=1`）。
     这两条同时把 **D1 的信封**在真 C++ 侧验了：GUI 真发的是 {"action","payload"}，
     与 Agent 的 decode_command 逐字节一致（之前只是"按构造应该一致"）。
     ⚠ 一处**没做成**：想用 XTEST 合成真实点击去按「学习」按钮（板端没有 xdotool，
     但服务端启用了 XTEST 且有 libXtst.so.6）。第一版 ctypes 没声明 argtypes，64 位
     Display* 被截断 → 点击静默无效；补上 argtypes 后，XTestFakeMotionEvent 之类的
     请求会**挂住**（疑似被别的客户端 grab 了 X server —— 板端跑着 onboard 屏幕键盘）。
     没有硬凑：GUI 的按钮点击与 stdio 命令走的是**同一个** `LocalClient::sendCommand`
     （gui/src/main_window.cpp 的模式按钮），且按钮级点击另有 GUI 自己的 QTest 单测
     覆盖（板端 ctest 16/16）。若要把"真按钮被点"也做成可复跑的验收，最干净的是给 GUI
     加一个 `--switch-mode-demo <MODE>` 开关（与其既有 --*-demo 系列一致）—— 待你定。
■ D7 未接线的命令（next_wallpaper / next_bilibili）回推一条 llm 说明「功能未接入」
     原来这两条只记一条 warning 就丢掉 —— GUI 上点了**毫无反应**，像坏了。
     现在 `UNWIRED_COMMAND_NOTES`（agent/ipc/__init__.py）给出面向用户的一句话，
     经 `push` 走 llm 通道回给 GUI：
       next_wallpaper -> "换壁纸的功能还没接入（Phase 7），这次点击先没有生效。"
       next_bilibili  -> "B 站「下一集」还没接入（Phase 7），这次点击先没有生效。"
     不认识的 action **不**回话（只记 warning）—— 那是版本不一致的正常现象，别往聊天里塞。
     板端活体验证：LocalClient 发这两条，各收到一条 llm（原文见上）。
     文档：ipc-protocol.md §4 与 gui-agent-integration.md §3 都写明"会回一条说明"，
     免得 GUI 侧以为这是 bug。
     测试 +4（两条各回一句说明 / 陌生 action 不塞 llm / 没有 push 也不炸 / 真 socket 收到）；
     三方：PC 整套 exit 0；WSL 14 文件 OK（92 条全跑，0 skipped）；板端 92 OK + 活体。

Phase 6 决策记录（已评审）
1. IPC 命令格式**以 GUI 实际实现为准**：{"action","payload"}
2. Host Input 方向**删除**（moonlight-common-c 无此能力），三源合并随之取消
3. build_ipc 签名扩为 (bus, config, runtime=None)，保持向后兼容
4. 板端**安装** pytest + pytest-asyncio（不再只跑 unittest）
5. send_key 端到端先用**不锁屏**动作验证，锁屏最后单独做
6. 不清理 Sunshine 侧历史遗留（重复证书条目 / min_log_level=debug）
7. next_wallpaper / next_bilibili 本期只做「收到 + 回推说明」
8. **Agent 只读 config/config.yaml**：不读 gui/config/gui.yaml，也不读 llm/config/llm.env
9. 【待定】快捷键监听（`scheduler.parse_hotkey_text` / `HotkeyBinding` / `listen_hotkey`）现在
   **没有生产者** —— 它唯一认的输入是 `source == "host_keyboard"`，而那条路已随决策 2 删除。
   Phase 6 收尾只清了 host-input 管道、**没有动这个功能**：要么将来把快捷键接到板子自己的
   键盘/GUI，要么一并删除。见 parse_hotkey_text 的 @note。

决策 2 的落地（Phase 6 收尾，已验收）：
   Host Input 侧代码**全部删除**，不只是"不清理"：
     · native/host_input_rb.h / .cpp（含 InputEvent、HostInputRingBuffer、kHostInputCapacity）
     · binding.cpp 的 host_input_rb 子模块、status() 的 host_input_* 字段、
       HOST_INPUT_CAPACITY 常量；binding_utils.h 的 event_to_dict()
     · MoonlightAdapter::host_input_rb() 与 Impl::input_rb
     · agent/io/host_input_reader.py 及其在 agent/io/__init__.py、agent/main.py 的装配
     · config/config.example.yaml 的 scheduler.host_input_interval_ms
     · tests/test_host_input_rb.cpp、tests/mocks/mock_agent_native.py 的替身子模块、
       tests/test_io.py 的 TestHostInputReader / TestEventToText 用例
     · docs/architecture.md、Readme.md 里声称"主机键盘 → Host Input RB → pybind11"的图与表
   板端 test_binding_api.py 反过来断言 host_input_rb **不存在**，防止它被带回来。

项目归一化处理（新增，与 Phase 6 并行）
☑ 唯一真源收敛：**已完成**（B1 `a364269` + B2 `5333bff`）。ipc-protocol.md 加了 §0 划清真源边界
     （只管线上格式）；字段定义表只留协议文档一份，gui-agent-integration.md 降级为"GUI 行为 + 指针"；
     Readme 删掉"实现未做"并补上命令信封。另加 tests/test_docs.py 文档守卫（相对链接 + 过时说法黑名单），
     已注册进三端共享的套件清单 —— 以后漂移会直接让测试变红。
     还发现并修掉：Readme 的 ZeroMQ 约定、architecture.md 整篇旧设计（ZeroMQ + PC 侧 GUI）。
☑ 配置入口收敛：**已完成**（归一化 D 系列 D1–D5）
     · D1 `56d1ff9` config.yaml 成为唯一真源：原 GUI 专用配置的 17 个键整体并进它的 `gui:` 段，
       同时修正 `llm:` 段漂移（edge = llama.cpp GGUF + 本机 llama-server）并补齐 llama-server 参数
     · D2 `da1ae85` GUI 改读写 config.yaml：11 + 16 个键改到 `gui.*`，`--gui-config` → `--config`
     · D3 `b6d5276` llm/config/llm.env 降级为**派生**文件：ConfigSyncer 只做
       config.yaml → llm.env 的 8 个 LLM_* 键单向映射，不再读写 GUI 专用配置、不再自己存一份
     · D4 删掉 gui/config/（模板连同 .gitignore 规则）；新增 docs/config-sources.md 讲清
       「谁写 / 谁读 / 谁派生」；新增 tests/test_config_source_guard.py（agent/ 里出现
       gui.yaml 或 llm.env 字面量即失败）；docs 的过时说法黑名单加两条
     · D5 `52e9459` 板端 live 配置落地：把原 GUI 专用配置里的界面参数并成 `gui:` 段、
       补齐 llm 的 7 个 llama-server 参数与 sunshine.https_port、ipc 段从 ZeroMQ 时代的
       pub_bind/sub_connect/recv_timeout_ms 改成 socket_path/queue_size、scheduler.hotkeys →
       commands、删掉无引用的 host_input_interval_ms；板端 gui/config/gui.yaml 已删除。
       落盘前逐键核对并在板端实测：目标配置 `--check-config` 退出 0、`gui_config_sync`
       派生零差异（llm.env 逐字节不变）、模型页保存零差异（D3 的"新键追加到段尾"就此关闭）。
     判据: 归一化后全仓只剩历史注释提到 GUI 专用配置；板端模型页改 mode → 只有 config.yaml 变、
     llm.env 被重新派生（且派生结果与板端在用的那份逐字节相同）；Agent `--check-config`
     对含 `gui:` 段的配置仍退出 0
☑ 清理并存实现：agent/ipc/server.py **已删除**（归一化 C1 `8608438`）——它是第二份 server 实现，自带第二份 decode_command()，只被 gui/tests/e2e_ipc.py 当联调对端用
     现在生产侧只有 agent/ipc/local_server.py 一份（收命令信封），e2e_ipc.py 改用现成的 gui/tests/local_server.py 当真对端。
     判据: agent/ipc/ 只剩 4 个文件; 全仓 `def decode_command` 只有 1 处; 板端 ctest e2e_ipc 全绿。
     顺带（C2 `4dcd42e`）把 gui/tools 两个只差一点的假 Agent 合成 fake_agent.py。
☑ 板端 ⇄ PC 同步机制固化：**已完成**（F1/F2/F3）
     · F1 `4f23a19` 之前的提交推到 GitHub，板端 fetch 后能看到 main
     · F2 `f78d273` deploy.ps1 生成 logs/deployed-manifest（逐文件 sha256）+ deployed-rev；
       health_check.sh 加"落后判定"（说得清落后几个、是哪几个）与"多余文件"，并加了
       `--list-extra` 供 `deploy.ps1 -Prune` 复用（规则只有一处）；-PruneDryRun 可先看
     · F3 板端从 `gui` 分支切到 `main`：备份/恢复板端本地路径 →
       `git checkout -f -B main origin/main` → 从旧提交恢复 llm/ sig/ net/
       config/config.yaml docs/gui-qt5-*.md tests/test_llm_integration.py todo →
       写 .git/info/exclude。**板端 git status 首次完全干净**（59 项脏 → 0），
       并顺带补齐了 main 有而板端一直没有的 native/ 源码、根 CMakeLists.txt、.gitmodules
     待做：F4 把"git pull 负责什么、deploy.ps1 负责什么、板端哪些不入库"写成 docs/deploy.md
     · F4 **已完成**：新增 `docs/deploy.md`（三条同步路径各管什么 / 清单与落后判定 /
       板端不入库清单 / 常见操作 / 踩过的坑），`docs/architecture.md` §2 §8.2 §9.2 同步，
       Readme 的 docs 目录树补成实际 8 个文件。文档守卫（相对链接 + 过时说法）通过。
☑ 板端卫生 + 四端复核（G1，归一化收尾）
     · 推送与同步：F3/F4/D1–D5 共 8 个提交推到 GitHub（`f78d273..52e9459`），板端
       `git fetch` + `git checkout -f -B main origin/main` 同步到 `52e9459`。
       **板端 git status 0 条**，与部署清单 98 个文件全部匹配；板端本地资产
       （config/config.yaml、llm/ sig/ net/ runtimes/ temp/ creds/、docs/gui-qt5-*.md、
       tests/test_llm_integration.py、tests/board/mpp_decode_smoke、todo）逐项确认在位，
       config.yaml 与 llm.env 的指纹一字未变。
     · 字节码卫生：清掉 65 个**不属于本机**的 .pyc（cpython-312/313/314，mtime 全是
       2026-09-16 —— 早期整树 scp 带过来的；板端全盘只有 python3.8，这些缓存永远不可能
       被加载）。117 → 52 个 .pyc，剩下 50 个 cpython-38 + 2 个 cpython-38-pytest。
       机制上也不会再回来：deploy.ps1 用 `git archive`，只送已跟踪的文件。
     · 判据复核（仓库侧）：agent/ 里读 llm.env 0 处、提 GUI 专用配置 0 处；
       全仓 `def decode_command` 1 处；agent/ipc/ 4 个文件；gui/config/ 目录不存在；
       .gitignore 里旧规则 0 条；提到 GUI 专用配置的只剩 2 处历史注释 +
       2 个守卫测试 + todo.md（历史记录，不在扫描范围）。
     · 四端：PC 宿主机 ctest 172/172；PC python 套件 exit 0；WSL python 套件 15 文件
       exit 0；板端 python 套件 16 文件 ALL OK；板端 GUI ctest 16/16。
     · 顺手记一个坑：**别在 WSL 里对 /mnt/e 的这份 checkout 跑 `git status`** ——
       WSL 的 git 默认 `core.autocrlf=false`，同一份工作树会假报约 100 个文件被改；
       `git -c core.autocrlf=true status` 就是干净的（PC 侧 autocrlf=true，权威视角）。
☑ 文档去重：**已完成**（B1 `a364269`）。gui-agent-integration.md §2/§3 的两张字段表降级为指针
     （字段定义只在 ipc-protocol.md），该文档只管"GUI 在哪儿发、收到后界面怎么变"；
     §6 里指向 temp/（clone 后不存在）的假 Agent 换成了仓库内真实脚本。

GUI 日程区（S 系列：右区域切成"对话区 + 日程区"，已全部验收）
☑ S1 板端依赖 + 探针：`apt install libyaml-cpp-dev`（0.6.2-4ubuntu1，2 个包、0 删除；
     回滚 `apt remove`）。探针问清四件事：`find_package(yaml-cpp)` 可用、**0.6.x 的 imported
     target 是不带命名空间的 `yaml-cpp`**（0.7+ 才是 `yaml-cpp::yaml-cpp`）；行内 flow 序列 /
     中文 UTF-8 / 未加引号的 `2026-09-20`（普通标量字符串）都能读；**坏转换与语法错一律抛
     `YAML::Exception`**（所以解析要先查 IsDefined/IsSequence/IsScalar 再转换）。
     顺手删掉 D3 遗留的 dead function（`test_config_sync.cpp` 的 newValueOf，一直在产生
     `-Wunused-function`）—— 也因此发现我 D3/D4 说的"零警告"是在**增量**日志上数的，不成立；
     从那以后一律用 `--clean-first` 全量重建来证明零警告。
☑ S2 核心解析 `gui/src/core/schedule_model.{h,cpp}`：yaml-cpp **只读**解析 config.yaml 的
     `scheduler.recurring/oneoff`（逐键回落顶层，与 `_load_events` 一致），镜像 Python 语义，
     展开成今天/明天两段。头文件只暴露 Qt 类型，yaml-cpp PRIVATE 链接。41 个单测。
     踩到并写进注释的 0.6.2 三个坑：① 默认构造的 `YAML::Node()` 是 `IsDefined()==true` 但
     `IsNull()==true`（判空必须两个都看）；② **从 const `YAML::Node` 取出的节点没有 memory
     holder**，对它调 `IsNull()` 会抛 `invalid node`（凡是要索引的句柄一律非 const）；
     ③ PyYAML 会把某些 plain 标量解析成 int/float/bool/null（含 YAML 1.1 六十进制 `9:30`），
     所以 title/start/end 要按"在 Python 眼里是不是 str"判断。
☑ S3 跨实现一致性守卫：`tests/data/schedule_parity/`（8 份手写夹具 + 8 份**由板端 python3.8
     生成**的期望）+ `tests/test_schedule_parity.py`（Python 语义变了就红，带反空转与 --write）
     + `test_schedule_model.cpp::parityWithPythonFixtures`（C++ 镜像变了也红，失败时打印两边
     行的全文差异）。**反证**：把 `occursOn` 改坏 → C++ 侧 FAIL 并打印多出来的行；改夹具不重新
     生成 → Python 侧 FAIL 并打印字段差异；还原后都变绿。跨版本稳定：3.8/3.12/3.14 都通过。
☑ S4 显示层 `SchedulePanel`（纯显示，不读文件）：两段标题总在、空段显示"无"、`HH:MM[-HH:MM]`
     + 标题、今天已过的时间变暗、`max_rows` 两段合计截断 + "还有 N 项"、逐条坏日程与整份失败
     各一行琥珀提示；副标题"今天 · N 项 · 下一条 HH:MM"（今天都过完了落回明天第一条）。16 个单测。
     自身修掉两处：控件与文本同名 `subtitle_`（编译期重复声明）、三个访问器只声明未定义。
☑ S5 装配 + 图标 + 几何守卫：右区域加第三块并 `setStretchFactor` 3:2（**只是伸缩因子**，
     窗口矮时最小高度会占超过 2/5）；`reloadSchedule()` 在启动/设置页保存后/每 60 秒各刷一次；
     自绘 `schedule.svg` 成第 23 个图标；**新增 `test_icons` 守卫**（kNames 必须与源码 `*.svg`
     清单逐个对上、每个名字都要能取到非空 QIcon —— 之前 `iconNames()` 全仓没人用，icons.cpp
     那句"测试会核对数量"是假的）；`test_main_page` 加几何断言。CMake 坑：
     `target_compile_definitions(test_icons ...)` 必须放在创建该 target 的 foreach 之后。
☑ S6 文档与模板：`config.example.yaml` 加 `gui.schedule.max_rows: 6`；`docs/gui.md`（依赖 /
     三段布局图 / 新键 / 现状表 / 图标 23）；`docs/config-sources.md` 新增"GUI 也读 scheduler 段"
     （Agent 触发 vs GUI 展示、两条互为表里的守卫、**保证边界只到夹具覆盖的写法**、重新生成期望
     要在板端跑），并记下 `config/schedule.example.yaml` 全仓没人读这处已知漂移；
     `architecture.md` 同步。
☑ S7 板端 live 配置：加 `gui.schedule.max_rows: 6` + 4 条**样例**日程（晨间计划 08:30 每天 /
     午休 13:00-13:30 每天 / 周会 10:00 周一三五 / 项目评审 2026-09-22 14:00 一次性）。
     先给目标文件、授权后落盘（指纹 `931d83c4…` → `789b14b7…`，`llm.env` 未动）。
     ⚠ 样例**不是只给界面看的**：`Scheduler._fire()` 对没有 action 的事件仍会推
     「日程提醒：<标题>」，会出现在对话区并进 LLM。
☑ S8 取证：新增 `agent_gui --dump-schedule`（走完整装配路径后打印 SchedulePanel 真实渲染的
     SUBTITLE/ROW/HIDDEN/NOTE），让"GUI 显示 vs Agent 展开"成为机器逐行比对 —— 实测 **6 行 +
     副标题 + 截断计数完全一致**。真机 scrot 三张（有日程 / 空状态 / 坏条目"配置里有 1 条读不出来"）。
     **更正**：真机**应用逻辑屏幕是 1280×800**（面板 DRM 模式 800x1280，xrandr 旋转 left），
     不是我在 S6 写的"800×1280 竖屏" —— `fb0` 的 `virtual_size` 不能拿来推布局。
☑ S9 四端回归：PC 宿主机 ctest 172/172；PC python exit 0；WSL python exit 0；
     板端 python 17 文件 ALL OK + 板端 GUI ctest 19/19；deploy 清单 115 文件全匹配
     （`config.yaml`/`llm.env` 指纹未变，板端 git status 归零）。
☑ S10 onboard 行为改为"点输入框才弹"（用户要求）：新增纯逻辑
     `OnboardCtl::shouldShow(onboardAuto, inputSource, inputFocused)` + `ChatPanel::inputFocusChanged`
     （只盯输入框的 FocusIn/FocusOut）。`applyInputType()` 不再弹键盘（只在切到 terminal 时收起），
     **弹的唯一调用点**变成"输入框获得焦点"。取证：真机两张 —— 启动态 `Visible=false` 且日志无弹出；
     `--focus-input-demo` 后 `Visible=true` + `[ui] 软键盘弹出（输入框获得焦点）`；live 配置指纹未变。
     单测：onboard 7 项（含 shouldShow 四种组合）+ main_page 10 项。取证参数 `--focus-input-demo` 也登记进 docs/gui.md。

CLI（C 系列：板端控制 CLI `assistant`，C1–C4 已验收；C5 排在 P 系列之后）
☑ C1 骨架 `agent/cli.py`（六条只读/控制命令 status/chat/mode/watch/schedule/doctor；人类可读输出，
     不做 `--json`；`--config` 语义与 `agent/main.py` 一致：给的是**路径**，其父目录必须含
     config.yaml/config.example.yaml）。踩到的 argparse 坑：全局选项要在子命令**前后**都能用，就得让
     主解析器与每个子解析器共享 `parents`，而子命令那份必须 `default=argparse.SUPPRESS` ——
     否则子解析器自己的默认值会盖掉写在命令**前面**的真实值。
☑ C2 socket 路径优先级（`--socket` > 配置 `ipc.socket_path` > 协议默认）；边界与 GUI 一致：CLI 只**读**
     `scheduler:` 段。D4 守卫 `tests/test_config_source_guard.py` 相应扩展：`cli.py` 允许**提** `llm.env`
     这个名字，但同一条语句里出现读调用就红（防"豁免变成后门"）。
☑ C3 `schedule`（用**真的** `Scheduler`/`occurs_on()`/`trigger_at()` 语义展开今天/明天）+ `doctor` 六项。
☑ C4 真机验收抓到两个问题：① `schedule_rows()` 把**明天**的行标成「已过」（`now > start` 对明天恒成立）
     -> `past` 现在要求 `day == now.date()`，加回归 `test_tomorrow_is_never_marked_past`；
     ② `watch` 挂着永远用不上的 `--timeout` -> 摘掉，并加"给 watch 传 --timeout 必须被拒（exit 2）"的用例。
     ⚠ 那个用例我第一版写成 `hasattr(args,"timeout")`，是**测试写错**：主解析器那份默认值本来就在
     namespace 里，"有没有这个选项"要看的是"传它会不会被拒"。
☑ C5 文档与启动器：新增 `docs/cli.md`（使用手册：怎么跑、公共选项与优先级、退出码、六条命令用
     **真机原始输出**当例子、"已触发 / 已过（未触发）/ 已过"三种标记对照表、边界、排障）；`Readme.md`
     三处（目录树加 `agent/cli.py`、docs 清单加 `cli.md`、`## 运行` 之后新增 `## 板端控制 CLI` 一节）；
     新增仓库文件 `scripts/assistant`（POSIX sh，**提交时带可执行位 100755** —— 不带的话软链目标不可执行）。
     板端按**软链**安装：`ln -sf <repo>/scripts/assistant /usr/local/bin/assistant`（`git pull`/deploy
     之后启动器自动最新，不会与仓库漂移）。启动器只做三件事：找仓库根 → 补 **PYTHONPATH** → `exec
     python3 -m agent.cli`；**不改 cwd**（改了会让 `--config ./x.yaml` 指向别处，实测反证：从 /tmp/c5cfg
     用 `--config ./config.yaml` 解析到的是 /tmp 那份「相对路径测试」，不是 live 配置的「晨间计划」）。
     板上取证：软链 `ls -l` 正常、首行 `od -c` 确认是 `# ! / b i n / s h \n`（没有 CR）、cwd=/root 下
     **六条命令全部跑通**（mode/status/chat/watch/schedule/doctor，退出码 0；watch 用外部 timeout 兜底 ——
     给 watch 传 `--timeout` 是被拒的）；live config 指纹 `789b14b7…` 全程未变。实测出并写进文档的一个细节：
     `doctor` 只要有一项警告，退出码就是 1（所以能直接当脚本健康检查用）。

日程触发事实协议（P 系列：让 CLI 看到"Agent 到底触发过哪条日程"，P1–P5 已验收）
☑ P1 `Scheduler` 记触发事实（`agent/core/scheduler.py`，**只加不改**：+87/−1，唯一删掉的那一行是
     `from typing import ...` 里加了 `Deque`）：有界 `_history`（`DEFAULT_HISTORY_LIMIT=50`）、
     `recent_fired(limit)`（返回副本）、`on_fire` 回调（**抛异常只打一行日志**，不影响触发与去重）、
     `stats["fired_history"]`。事实形状 `title/date/scheduled_at/fired_at/actions`（`fired_at` 就是原来的
     `now`，对客户端来说"now"没意义）。板端裸输出四组：空 / 触发后 1 条 / 回调炸了仍然算触发且去重照旧 /
     limit=3 时丢最旧。16 个新用例。
☑ P2 协议（真源文档与代码同任务落地）：`TOPIC_SCHEDULE="schedule"`、`COMMAND_QUERY_SCHEDULE="query_schedule"`
     进 TOPICS/COMMANDS/`__all__`；`docs/ipc-protocol.md` 加 §0 真源归属行、§3 topic 行 + "事实对象"字段表
     + 两条示例、§4 命令行 + "应答=随后那条推送、**没有请求 id**"、§8 两行常量。`logs/p2_check.py` 做
     **机械核对**：§8 每一行常量 vs `protocol.py`（20 对）、§3 事实字段表 vs Scheduler 真产出、
     §3 示例 JSON 里的键必须在表里 —— PC 与板端都 PASS。⚠ 核对脚本第一版自己解析错了引号与表格续行
     （跑出一片假 FAIL），是**工具**的问题不是文档；已加"解析到几行"的防空转断言。
☑ P3 接线与降级（`agent/ipc/__init__.py`）：`_scheduler_of()`（拿 `recent_fired` 当能力探测，不
     isinstance，测试替身与将来实现都接得上）、`_wire_outbound()` 接 `on_fire -> schedule{kind:"fired"}`、
     命令分支 `query_schedule -> schedule{kind:"state"}`（`limit` 报**真实上限**）、没有调度器时回一条
     `llm` 说明而不是静默。板端线上原始行三组（fired / state / 无调度器时的 llm 说明）与文档 §3 逐字对齐。
     `agent/main.py` 的 `_STEPS` 里 `_start_scheduler` 在 `_start_ipc` 之前（依赖已核）。
     顺带把 `Scheduler.history_limit` 做成公开属性（不然 ipc 层要摸 `_history.maxlen`）——**超出任务列表
     一行，已单独报备**。9 个新用例（含两条真 socket）。
☑ P4 CLI（`agent/cli.py`）：`ask_fired()` 发 `query_schedule` 等 `schedule{kind:"state"}`；三态标记
     「已触发 HH:MM:SS / 已过（未触发）/ 什么都不标」——**只有问到了才敢说"未触发"**（`asked=False` 时
     一个字都不许提）；两条页脚（问到 / 问不到 + 原因），`--no-ask` 明确"是你要我不问的"；`watch` 把
     `schedule` 推送打成一行 key=value（不倒嵌套 dict；不认识的 kind 原样打，不装懂）。
     与行对齐用 `trigger_at`（与 Agent 同一个 `ScheduleEvent.trigger_at()`），所以提前量跨天
     （00:05 提前 10 分钟 → 前一天 23:55）也能对上；配置改过对不上就是"这个时刻没触发过"，如实。
     19 个新用例。⚠ 我写测试时两次犯同一个手误（字符串里套双引号），`ast.parse` 当场报红才拦住。
☑ P5 契约 + 端到端 + 四端回归：C++ 侧补一条**钉住 `schedule` 字面名字**的契约测试
     （`test_view_state.cpp::scheduleTopicIsStillIgnoredByTheGui`：GUI **故意不认**这个 topic，靠协议 §3
     的"未知 topic 忽略"保持向前兼容 —— 把"不认"钉成契约而不是巧合；通用"未知 topic 忽略"用例本来就有，
     没重复造）。板端端到端（`logs/p5verify.sh`）：**临时配置**（`start = 现在+2 分钟`、`interval_min=0.2`）
     + `--config` 指它 + socket 也在 `/tmp`，真 Agent 起在 13:26:45；`watch --topics schedule` 在
     **13:28:09** 收到 `kind=fired title=触发事实验收 … fired_at=2026-09-22T13:28:09`（用时 81.7 秒），
     随后 `assistant schedule --today` 打出 `13:28  触发事实验收  ← 已触发 13:28:09`（同配置里 23:59 那条
     不标）。**live config 指纹 `789b14b7…` 全程未变**（只用 `--config` 指临时文件，没碰它）。
     四端：PC 原生 ctest 172/172；PC python 套件 `python tests OK`；板端 python 套件 18 文件 OK；
     板 GUI ctest 19/19（含新契约用例）。CLI 单测：PC 71 项（skip 17，全是 AF_UNIX）/板端 71 项（0 跳过）。
     ⚠ 已知边界：触发记录**只在内存**（重启即清零，这是"事实"的定义）；GUI 界面暂不显示"已触发"
     （协议已经铺好，要不要显示是另一个任务）。

遗留清理（L 系列）
☑ L1 删掉没人读的日程模板 `config/schedule.example.yaml`：它是历史遗留，**全仓 0 处代码读它**
     （`Scheduler._load_events()` 只从 `config.yaml` 的 scheduler 段/顶层找日程），里面的
     `timezone` / `defaults.remind_before_min` / `location` 也从来没有读取者。
     做法是**先迁移、再删除**：把它唯一有价值的东西——**事件语法**——以**注释**形式并入
     `config/config.example.yaml` 的 `scheduler:` 段（**只写真会被读的键**：title / days / start /
     end / date / remind_before_min / action），并明确写出"`timezone`/`defaults`/`location` 写了也不生效"，
     同时给上 `recurring: []` / `oneoff: []` 两个空键（与 `commands: []` 同一风格）。
     ⚠ 用注释而不是真条目：`_fire()` 对没有 action 的事件**也会推**「日程提醒：<标题>」，
     真条目会让每个新 clone 凭空多出提醒（S7 实测过这条）。
     连带改：`Readme.md`（目录树 / 配置一节的两条 `cp` + 一句"日程写在 scheduler 段"）、
     `docs/architecture.md` 目录树、`docs/config-sources.md`（§5 的"已知漂移，暂不处理"→"**已删除（L1）**"）、
     `agent/core/scheduler.py` 里那句"与 schedule.example.yaml 一致"的注释。
     迁移后 PC 与板端都验过读得动：Python 侧 `assistant schedule` 报"共 0 条"（空列表，符合预期）、
     GUI 侧 `agent_gui --dump-schedule --config <模板>` 报"日程: 0 行 / SUBTITLE 今天没有日程"。
     ⚠ 有意偏离：`remove_fired_oneoff` 开关的注释**没有**跟着一起写进模板 —— 现在写进去就是
     "文档说有个键、代码不读"，正是 L1 要清掉的那类漂移；它跟 R3 的代码一起加。
☑ L2 修文件名拼写 `docs/architecure.md` → `docs/architecture.md`：`git mv`（保留历史）+ **全仓零命中**
     —— 活引用 19 处（Readme 4 / ipc-protocol 1 / deploy 4 / cli 2 / `native/*.h` 6 个头注释 /
     state_machine.py 1 / scheduler.py 1 / test_docs.py 的 must 清单 1）+ todo.md 里 6 处历史提法，
     合计 **25 处 / 15 个文件**（22 行增 22 行删 —— 只动了那一行里的名字）。
     ⚠ `tests/test_docs.py` 把旧名字写死在"必须扫到"的清单里, 不改就红 —— 这正好是
     "改名必须全仓找引用"的守卫。
     ⚠ 手法：25 处替换没有手工点，用 `[IO.File]::ReadAllText` + `UTF8Encoding($false)` 逐文件重写
     （逐个确认过都没有 BOM；用 `-creplace` 而不是 `-replace`，免得误改大小写）。
     todo.md 的历史行**按你的选择一起改名**（没加"当时叫…"的注记 —— 加了就又不零命中了），
     所以现在 `grep architecure` 是 **0 命中**。
     ⚠ 我第一版只 grep 了**文件名**就以为清干净了, 结果漏掉按**名字**引用它的地方 —— 是测试把它
     顶出来的；那条教训记在 L1。

日程显示重构 + 触发后移除（R 系列）
☑ R1 CLI 改成"后 N 小时"窗口（`agent/cli.py`）：默认 **24 小时**、`--hours N` 可调（`type=positive_hours`，
     非正数/非数字直接是参数错 exit 2）；**删掉 `--today/--tomorrow`**；`--limit` 变成**整个窗口**的
     行数上限（不再是每天一份配额）。新增 `window_range / window_days / render_window / day_heading /
     window_end_text / row_mark`；`schedule_rows` 与 `render_schedule` 保持原语义（"整天视图"与既有
     测试不动，只把三态标记抽成 `row_mark` 共用）。
     两个关键口径：① 可见性按**用户看到的 `start`** 判、不用 `trigger_at` —— 否则 `start=14:00` +
     `remind_before_min=10` 的条目在 13:55 看会因为提醒时刻（13:50）已过而消失；
     ② **30 分钟尾巴**：窗口只往前看，但"刚刚过去"的那一小段留着，否则 P 系列的
     「已触发 / 已过（未触发）」在列表里完全看不见。尾巴里**配置已经被删**的一次性条目（R3 之后
     的常态）靠**触发事实**补成行（`from_fact`），不会因为"配置里没有"就丢。
     列表头打印窗口（`窗口：18:00 → 明天 18:30（24 小时；另有最近 30 分钟里刚过去的）`）；
     段标题今天/明天/后天带日期，更远的直接给日期（`2026-09-25（周五）`）。
     证据：确定性演示（注入 now=2026-09-22 18:26）四组；板端真 live 配置 18:30 跑 `--no-ask` 默认 24h
     → **今天那三条全部不在窗口里，只剩明天的 08:30 / 10:00 / 13:00**；`--hours 1` → 窗口内没有日程；
     `--hours 72` → 明天 / 后天 / 2026-09-25 三段；真 Agent 的 asked 路径 → 页脚换成"来自运行中的 Agent"。
     测试：PC 与板端 `tests/test_cli.py` **89 项**（原 71，+18；PC skip 17 全是 AF_UNIX）。
     ⚠ 我第一版的 5 个新用例**期望写错了**（把"每天 08:30"当成"今天不显示"，其实明天的 08:30 正好落在
     24 小时窗口内）—— 是测试错、不是代码错；改用 `oneoff`（指定日期）做边界用例，并把"每天"那条改成
     断言真实语义（只显示下一次）。
     ⚠ `docs/cli.md` 里 `--today/--tomorrow` 与"今天/明天"的说法**暂时过时**，R4 统一改。
☑ R2 GUI 用同一套窗口（`gui/src/core/schedule_model.{h,cpp}`、`ui/schedule_panel.{h,cpp}`、
     `main_window.cpp`、`main.cpp`）：窗口做成**独立一层** `applyWindow()/loadWindowed()`，
     **`parse()/loadFromConfig()` 一动不动** —— 那是与 `scheduler.py` 逐条对齐、被 8 份夹具与
     C++ parity 用例盯着的一层，显示规则不该混进去。`main_window` 改调 `loadWindowed`
     （`[now, now+24h)`，**GUI 没有尾巴** —— 它拿不到 Agent 的触发事实）。
     副标题改成 `接下来 24 小时 · 到 明天 18:42 · 3 项 · 下一条 明天 08:30`（窗口终点在副标题里，
     第二段标题仍写"明天" —— 按你的选择：截断只用副标题说明）；`--dump-schedule` 加
     `SECTION\t今天 · 0 项` 行，让"今天 0 项 / 明天 3 项"这种结构也进得了证据。
     两侧口径统一到**分钟粒度**：`applyWindow` 把 now 截到分钟，`agent/cli.py` 的 `window_range`
     也截到分钟（否则"当前这一分钟"的那条两边会不一致）。
     ⚠ `applyWindow` 的窗口**上限收敛到 24 小时**：展开层只有今天/明天两段，要更宽得同时改展开层
     与夹具 —— 与其副标题写着 72 小时却只显示两天，不如收敛（有单测钉住）。
     ⚠ **附带后果**：窗口只往前看 → 经过窗口的行不会有 `past`，界面上**看不到"已过变暗"**了。
     变暗的渲染与单测都**保留着**（将来给 GUI 加尾巴那天不用重写），并有单测把"窗口内永远不 past"
     钉成事实。要不要给 GUI 也加同一条 30 分钟尾巴（不依赖事实，只是变暗淡出），**等你说**。
     证据：板端 `--dump-schedule` 两组 —— live 配置 → `今天 · 0 项 / 明天 · 3 项`（今天那三条全被挡）；
     临时配置（今天 1 小时前 + 今天 1 小时后 + 明天同一时刻）→ 只留"待会儿的"一行（过去那条被丢、
     超出窗口终点的那条也被丢）。测试：板 GUI ctest **19/19**（连跑两遍），`test_schedule_model`
     **50 项**（原 44，+6 窗口用例），`test_schedule_panel` 全绿；板 `tests/test_cli.py` **90 项**。
     ⚠ 同一次全量 ctest 里 `test_bench_runner` 失败过一次，单跑与随后两次全量都通过 —— 那是
     **并发负载下的抖动**（它与日程无关），不是 R2 引起的。
     ⚠ 我在窗口用例里**连续三次**把"没写 days 的 recurring 当成只在那一天"（其实那是**每天**）：
     R1 两次、R2 一次。改法是把窗口用例里的日程全写成 `oneoff`（指定日期），语义才唯一。
☑ R3 触发后把那条一次性日程从 **config.yaml 里删掉**（默认关）：新增
     `agent/core/schedule_config.py` —— **文本级**外科删除（找 `oneoff:` 序列里那一条的行区间，
     只删属于它的行，其余**逐字节**不变；注释与顺序都保住）+ 写前**重新读盘**核对
     （title + date + start 三者都要对上，对不上就不删）+ 原子写 + 原文件旁留 `.bak`。
     匹配靠**注入的 predicate**：文件里抽出来的裸标量交给 Scheduler 判断（它懂 `parse_clock` /
     `date.fromisoformat`），本模块只懂文本 —— 顺带避免 `scheduler ↔ schedule_config` 循环依赖。
     `Scheduler` 新增 `config_path` 参数与 `remove_fired_oneoff` 开关（**默认 false**，非布尔值
     记 warning 并按 false 处理）；触发后调用，**失败只记 WARNING**（写不了配置绝不能影响触发）。
     只对 **oneoff** 生效，recurring 一条都不动。`agent/config.py` 的原子写抽成
     `write_text_atomic()`（**全仓唯一实现**，`save_config` 也改走它）。
     明确不做并在测试里钉住的：flow 风格（`oneoff: [{...}]`）给理由、不改文件；紧贴**下一条**的
     注释与空行**留着**（只删属于那条的行）；序列空了就把 `oneoff:` 写成 `[]`。
     新增 `tests/test_schedule_config.py`（28 项：文本形状 16 + 文件级 4 + Scheduler 集成 8）并登记进
     两个 test-python 脚本（PC 19 项 / 板 19 文件都跑到了）。新守卫
     `tests/test_config_source_guard.py::TestWhoWritesTheConfig`：`agent/` 里出现写入原语的**只能是**
     `agent/config.py`（唯一实现）与 `agent/core/schedule_config.py`（唯一调用方），带反空转。
     板端端到端（临时配置 + 开关 true）：18:48:02 触发 → 配置里那一条**真的没了**
     （另一条与紧邻的注释原样保留）、`config.yaml.bak` 里两条都在、Agent 日志有"已从 … 删掉…"，
     随后 `assistant schedule` 里那条**靠触发事实**还在（`← 已触发 18:48:02`）—— R1 的尾巴 + R3 的删除
     正好接上。live config 指纹 `789b14b7…` 全程未变。
     ⚠ 测试自身的两个错（都是"没考虑运行环境"，且第一个在 PC 上**假绿**、板端才暴露）：
     ① 以 root 跑时目录权限拦不住写入 → 那两条用例在 root 下**显式跳过**（不把断言放宽成"抛不抛都行"）；
     ② `load_config` 按 `AGENT_CONFIG_DIR` 找文件且有缓存 → 不设它就会去读板端 live config
     （PC 上因为退回 example 模板、恰好 0 条而蒙对）。现在显式指到临时目录 + 清缓存。
☑ R4 文档统一改口径 + 新守卫：`docs/cli.md` 的 `schedule` 一节整段重写（窗口与 `--hours`、30 分钟尾巴、
     三种标记"基本只出现在尾巴上"、一次性日程被删后列表里就没有它、`--hours 30` 的新例子）；
     `Readme.md`（CLI 示例 + "只显示接下来 N 小时"）；`docs/config-sources.md` 改**边界** —— §1 的一句话与
     示意图、§2 的"谁写"表都写上"Agent 只在一件事上写"，新增 **§3.1** 把八条规则（只删 oneoff / 文本级 /
     原子写 / `.bak` / 写前核对 / 失败只 WARNING / flow 风格拒绝 / 注释归属）与**两条如实写下的限制**
     （竞争窗口、崩溃窗口）落成表格，§5 的"展示"行改成"展开 + 窗口筛选"；`docs/gui.md`（窗口写在副标题、
     GUI 无尾巴、"已过变暗"不再出现、刷新时机改成"窗口往前滑"）；`docs/architecture.md` 新增 **§6.2 窗口**
     一节 + 文件表与 §6.1 的措辞；`docs/gui-agent-integration.md` 那句"Agent 只读"。
     顺带改掉三处**代码注释**里同样过时的"已过变暗跟着时间走"（`gui/src/main_window.{h,cpp}`）。
     `tests/test_docs.py` 的 STALE_CLAIMS 加一条 `--today|--tomorrow`（R1 已删这两个开关），并**做了反证**：
     往 `docs/` 放一个写着 `--today` 的临时文档 → 守卫 FAILED；删掉 → 恢复 OK（证明新黑名单真的会咬）。
     证据：PC `python tests OK`；板 `test_docs` 4 OK + `python tests OK (19 files)`。
☑ R5 四端全量回归 + 板端组合端到端 + 收尾：
     四端（都在**已归零到 `2349b61` 的提交树**上跑）：PC python 套件 **exit 0**（`python tests OK`）；
     PC 原生 ctest **172/172**；板 python 套件 **19 文件 OK**；板 GUI ctest **19/19**。
     板端组合端到端（`logs/r5verify.sh`：临时配置 + 开关 true + 一条 2 分钟后触发的 oneoff）：
       [触发前] CLI 列 2 条；GUI `今天 · 1 项 / 明天 · 1 项`，两行都在
       19:05:04 触发（`watch` 收到 `kind=fired`）
       [触发后] 配置里那条**没了**（`oneoff: []`），Agent 日志有"已从 … 删掉…"；
                **CLI**：配置共 1 条，但尾巴把那一条**靠事实**画出来 → `19:05  一次性的  ← 已触发 19:05:04`；
                **GUI**：读得动被改过的文件，而且那一条**不在**列表里（`今天 · 0 项 / 明天 · 1 项`）
                —— 因为 GUI 没有尾巴（它拿不到触发事实）。这就是 R1+R2+R3 三条拼在一起的验收形态。
       live config 指纹 `3662d089…` 全程未变。
     板端归零：HEAD → `2349b61`，`git status` **0 行**；`config/config.yaml` 仍是开关版 `3662d089…`
     （它被 `.gitignore` 忽略，reset 碰不到）、`llm.env` 未变、`scripts/assistant` 仍 100755、软链仍通。
     ⚠ 归零脚本最后那句还是旧的 `--today`，板端回了 `unrecognized arguments: --today` —— 顺带**证明**
     R1 删掉那两个开关在板上真的生效（那是我的临时脚本过期，已改）。
     ⚠ 临时配置没有 `gui:` 段时 GUI 会打一行 `[ui] 保存输入源失败: … 找不到键 gui.input_source` ——
     既有行为（真实配置从 config.example.yaml 来、带 `gui:` 段），不是 R3 引起的；但它同时说明
     **GUI 也在写这个文件**，正好印证 §3.1 记的"第二个写入者"那条限制。

R 系列到此结束：**日程显示重构**（R1 CLI 窗口 / R2 GUI 同一套）+ **触发后从配置里移除**（R3）
+ **文档统一口径**（R4）+ **四端回归与端到端**（R5）。板上 `remove_fired_oneoff` 已按你的决定打开。

收官后追加两项（用户要求，"直接做"）：
☑ A GUI 也加 30 分钟尾巴（与 CLI 同口径）：`kTailMinutes = 30`，`applyWindow` 的起点从
     `[now, …)` 改成 `[now-30min, …)` —— 尾巴里的行**沿用展开层算出的 `past`**，所以界面上就是
     **变暗**（S4 那套渲染原样复用，没写新代码）。`--dump-schedule` 顺手把已过的行标成
     `ROW\t[已过] …`（否则"尾巴"在证据里看不见）。
     ⚠ 如实记一条代价：窗口起点跨午夜时，前一天的尾巴行 GUI **看不到**（展开层只有今天/明天两段）；
     CLI 能看到（它按需展开任意天）。这条写进了 `docs/gui.md`。
     测试：`test_schedule_model` 把原来那条"窗口内永远不 past"改成
     `tailRowsAreKeptAndMarkedPast` + `tailBoundaryIsInclusiveAtThirtyMinutes`（正好 30 分钟算在内）。
☑ B `assistant cleanup`（**显式**清理已经过去的一次性日程）：默认 **dry-run**（只列，不动文件），
     `--apply` 才真删 —— 走的是 Agent 那套文本级删除（`agent/core/schedule_config.py`），
     不是另写一份：原有的 `remove_fired_oneoff()` 因此改名成 **`remove_oneoff_from_file()`**
     （现在有两个调用方：Agent 触发后自动删、CLI 显式清理）。
     "匹配这条 oneoff"抽成模块级 `Scheduler.oneoff_matcher(event)`，Agent 与 CLI **共用一份**
     归一化规则（`parse_clock` / `date.fromisoformat`），避免两层互相 import。
     规矩：只清 `date < 今天`；**今天的不动**（`late_grace_min` 可能还认它，输出里会提示一句）；
     recurring 一条不动；**给 `--config` 指模板会直接拒绝**（不写 example）。
     这是 CLI"默认只读"的**唯一显式例外**，docs/cli.md 与 config-sources.md §3.1 都写明了。
     测试：`tests/test_cli.py` 新增 `TestCleanupCommand` 9 项（dry-run 不动文件 / 只删过期那条 /
     今天与将来与 recurring 都不动 / 连删两次无害 / 空清单 / 模板拒写 / 配置读不到 / 拒收 `--timeout` /
     `stale_oneoffs` 纯函数）。
     证据（板端临时配置）：GUI `--dump-schedule` → `ROW [已过] 19:05  十分钟前的` +
     `ROW 21:15  两小时后的`；cleanup dry-run 文件不动、无 `.bak`；`--apply` 只删三天前那条、
     `.bak` 里 3 条都在；再跑一次说"没有需要清理"；live config 上 dry-run 只读（今天那条不算过期）。
     四端：PC python `python tests OK`（test_cli 99 项）、板 python 19 文件 OK、板 GUI ctest 19/19。
     ⚠ 我这次在测试上连踩两个"环境隔离"的坑（都是测试自己的错）：① `load_plane_config` 用的是
     `os.environ.setdefault(AGENT_CONFIG_DIR, …)` —— **一个进程只认第一次**，前面的用例会把临时目录
     泄漏给后面的（于是有的用例读到了别人的配置、有的读到已删目录）；加了共用辅助
     `_run_cli_with_config()` 显式覆盖 + 清缓存 + 还原。② 两个用例的临时目录里已经有 `config.yaml`，
     加载器当然先找到它，测不到"模板"那条分支 —— 各改用**独立空目录**。

Phase 7 — 工具层（**T1–T5 全部完成**：骨架 / edge 真后端 / 壁纸工具 / 权限表 / 端到端验收）
☑ tools/__init__.py：工具注册入口（`TOOL_MODULES` + `build_tools(router)`）
      **没有** `tools/base.py`：工具的形状就是 `core/tool_router.py` 里的 `Tool` dataclass
      （name/description/schema/handler/allowed_states），再包一层基类只是多一层没人用的壳。
      每个工具模块自己导出 `NAME/DESCRIPTION/SCHEMA/ALLOWED_STATES/build(services)`。
☑ core/tool_router.py：注册、权限、调度（Phase 3 的骨架已完备，此处只补 `services`）
      `ToolRouter(services={...})` —— 工具要用的依赖（input_sender / image_reader / bus / config）
      **挂在 router 上**，这样 `build_tools(router)` 的签名不变、测试不用改；本模块不解释依赖内容，
      工具自己按名字取，缺了就是缺了（`build()` 返回 None 并记 warning）。
☑ tools/back_to_desktop.py（原 lockscreen.py）：调 InputSender.show_desktop()（= WIN+D），仅 STUDY
     为什么不是锁屏：Windows 过滤**合成输入**的 Win+L（实测连 host 本机 keybd_event 合成也锁不上，
     与 Ctrl+Alt+Del 同属安全动作）→ 按你的决定改成"回到桌面"，而 WIN+D 在真机上是通的。
     机制已经在 `agent/io/input_sender.py::InputSender.show_desktop()` 里（Phase 6 收尾时就位）。
     ⚠ 它是**开关**且没有回执：实测连按两次里有一次没落地（第 2 次丢了，第 3 次才还原），
       而且那次还原的截图字节数与基线不同（1352621 vs 1718395，右侧的 VS Code 没回来）。
       **这两句必须让模型看见**，所以写进了 `DESCRIPTION`（不是只写在注释里）：
       "开关动作 + 没有回执 + 一次调用只发一次、不要连着调"。tests 有一条专门钉这个措辞。
□ tools/netease_music.py：cloud-music-mcp 集成，动态歌单、批量加歌、URL Scheme
      ↓ 往后放（你定的）：见本节末尾"下一个任务列表（草案）"
□ tools/bilibili.py：搜索、order=play 排序、顺序播放、反馈切换
      ↓ 同上
□ tools/wallpaper.py：按 LLM 编排序列切换（**T3 已完成**：目录游标 + IPC 命令 + LLM 工具 +
     板端截图验收；"按内容挑图"要等**标签化**，见下面那条）
☑ 每个工具的单元测试（mock 外部依赖）—— back_to_desktop 已覆盖
☑ tests/test_tools.py：工具注册、状态权限、参数校验（17 项）
☑ 工具在正确状态下才允许执行 / 权限控制生效（如回到桌面仅 STUDY）
      → **T4 完成**：整张表钉在 `tests/test_tool_permissions.py`（含"禁止的组合真的被拒、
        handler 一次没跑"），并且给模型的候选清单也按状态过滤
☑ LLM 能调用工具完成任务 ← **T2 已完成**：edge 接上真模型，且与 cloud 共用同一个工具循环
      ⚠ 如实记一条限制：Qwen3-0.6B 很小，工具调用的**可靠性有限** —— 板端探针里它调对了
      `get_board_time`，但别指望它像大模型那样稳定挑工具、填参数。
☑ 工具层端到端验收（T5：真板 + 真模型 + 真工具）→ **完成，见下面 T5 记录**

T1 记录（已完成，等验收）：工具层骨架 + 第一个真工具
☑ 落地的文件：`agent/tools/__init__.py`、`agent/tools/back_to_desktop.py`、
     `agent/core/tool_router.py`（+`services`）、`agent/main.py`（装配 services + 文案）、
     `tests/test_tools.py`（新，17 项）、`tests/test_main.py`（改了一条旧用例，见下）。
☑ `agent/main.py::_register_tools()` 的旧文案"agent/tools/ 还没有实现"已过期（现在有真工具了），
     改成"导不进 agent/tools (%r)"，并把 import 的异常原因带上 —— 原来那句会让人以为工具层还是空的。
     ⚠ 原来那条 `test_missing_tools_module_is_not_a_failure` 断言的是 `len(rt.tools) == 0`，
     **它成立只是因为当时 `agent/tools/` 不存在**；现在这个前提没了，所以把它改成**真的**打断
     `importlib.import_module("agent.tools")`（mock 只对 `agent.tools*` 抛 ImportError，其余放行），
     另加一条 `test_real_tools_module_registers_its_tools`（非空 + 认得 back_to_desktop）。
     教训：靠"某个东西还不存在"成立的测试，等它存在的那天会以**看起来无关**的方式红。
☑ 缺依赖是可测分支：`build()` 缺 `input_sender`（或 sender 没有 `show_desktop()`）→ 返回 None +
     一条 warning，工具层少一个工具但**照常起来**；`agent/tools/__init__.py` 对导入失败/工厂抛异常
     也只记 error 并跳过（与 main.py"单组件失败不影响其他组件"同一口径）。
☑ 权限 fail-closed 有测试钉住：IDLE 下调 `back_to_desktop` 被拒且 `sender.calls == 0`（handler 一次没跑）。
☑ 文档：`docs/architecture.md` §4.1 新增"工具层"（约定表 + 依赖来源 + fail-closed + 只有 cloud 走工具循环）。

T2 记录（已完成，等验收）：edge 接真模型 + 工具循环共享
☑ 代码：`agent/llm/provider.py` 重构成"一个底座 + 两个后端"：
     `_OpenAICompatibleBackend`（client 惰性创建 + `create(messages, tools)`）／
     `EdgeBackend`（真后端：连本机 llama-server）／`CloudBackend`（行为不变）。
     `LLMProvider._chat_with_tools(backend)` 与 `_plain_chat(backend)` **两边共用** ——
     edge 与 cloud 的差别只剩"连哪儿 + 每次请求带什么参数"。
☑ **删掉了 EdgeBackend 的 mock**（`reply=` / `responder=` / `is_ready()` 恒 False）。
     替身改成"注入假 openai client"（与 cloud 完全同一套），于是"edge 也走工具循环"是
     **真事**而不是 provider 里的一个特例分支。`tests/test_llm.py` 相应重写 5 条、新增 12 条
     （切后端 / edge 真循环 / 轮数上限 / 配置派生 / 降级 / 空正文 / `is_ready` 离线语义）。
☑ 读配置：`EdgeBackend.from_config()` 读 `llm.port` → `http://127.0.0.1:<port>/v1`、
     `model_name` → model、`local_api_key` → key（**不是**云端的 `api_key`），
     另外 `max_tokens` / `temperature` / `timeout_s` / `model_path`（只进日志）。
     读不到就各自退回默认值（9000 / qwen3-0.6b），**不抛异常**。
     ⚠ 这两个键（`max_tokens` / `temperature`）以前 **agent/ 里没人读**，模板里也这么写着 —— 现在读了，
     `config.example.yaml` 与 `docs/config-sources.md` §2.1 都改了（"哪些键有第二个读者"落成一张表）。
☑ Qwen3 的思考坑 + `/no_think`：edge 默认在 system 提示末尾加 `/no_think`。
     板端实测（RK3568 + Qwen3-0.6B-Q4_K_M + llama-server build 10677，温度 0，同一句话）：
     | | 耗时 | reasoning_content | 正文 |
     | 不关思考 | 27.0 s | 281 字 | 8 字 |
     | 关思考（默认） | **2.3 s** | 0 字 | 14 字 |
     （早先的探针里还见过更糟的：思考把预算烧完 → `finish_reason=length` + 正文空字符串。）
☑ 降级（你批的方案里那条"llama-server down → 退 RuleEngine + warning"）：
     · `chat()` **照抛**（调用方要知道模型挂了）
     · `chat_with_tools()` 退回规则兜底：`ok=True`、结果里多一个 `degraded` 写原因、
       `agent/main.py` 记一条 `LLM 降级为规则兜底: <原因>` 并计入 `llm_errors`
     · **正文前面加一句"（板端模型没有响应，这条是规则兜底）"** —— 因为 **GUI 只显示正文**，
       只写日志的话，界面上那条兜底回复看起来与模型答的一模一样（这条是板端探针跑完才补的）
     · 模型"没给正文"也算降级，理由里写清是 `finish_reason` 还是思考吃掉了预算
     · `cloud` **不降级**（保持原样：ok=False + 原因），这条边界有专门的测试钉着
☑ 板端实测（真 llama-server，不碰主机、不碰配置）：
     · `provider.chat("你好，你是谁？")` → `我是RK3568开发板上的桌面助手。`（2.6 s）
     · `chat_with_tools("现在板子上几点？用工具查一下。")` → 模型自己调 `get_board_time`，
       工具返回 `2026-09-22 20:05:31`，模型据此作答（`tool_calls` 里看得到）
     · 端口指向没人听的 9099：`chat()` 抛 `APIConnectionError`；
       `chat_with_tools()` 降级，`degraded="APIConnectionError: Connection error."`
☑ live config 切成 edge（你之前定的"验完把 live 切成 edge"）：
     `llm.mode: disabled` → `edge`，指纹 `3662d089…` → `598a9d70…`；派生文件 `llm.env` **没动**
     （仍 `2d8bcac5…`，`assistant doctor` 那条"派生 llm.env 与 config.yaml 一致"依旧 OK）。
     顺手把 live config 里 3 处**已经过期**的注释改掉（"EdgeBackend 仍是 mock"、
     两处"当前 agent/ 里还没有人读这个键"、"这一组只用于派生、Agent 自己不读"）。
☑ 端到端（Agent 真跑在 live config 上）：启动日志同时出现
     `LLMProvider 就绪 (mode=edge)` 与
     `llm: edge 后端 = llama-server http://127.0.0.1:9000/v1 model=qwen3-0.6b max_tokens=512 temperature=0.7 no_think=True`；
     `assistant chat` 三轮都是真答复；把 llama-server 停掉再问 → 回复带降级前缀 + 日志 warning；
     再起回 llama-server → 又是真答复（恢复得了）。
☑ 文档：新增 `docs/llm.md`（三模式 / edge 怎么接 / 工具循环 / `/no_think` 实测表 /
     降级表 / 怎么验 / 边界）；`docs/architecture.md` 加 §4.2 并修 §4.1 那句"只有 cloud 走工具循环"；
     `config.example.yaml` 的 llm 段重写；`docs/config-sources.md` 加 §2.1；`Readme.md` 的 LLM 一节与目录树。
☑ 测试：PC `test_llm.py` 90 项、板端同一份 90 项；PC 全套 `python tests OK`；板端 20 个文件 OK。
     ⚠ 教训（板端运维）：`pkill -f 'agent[/]main[.]py'` 会连**自己所在的 ssh shell** 一起杀掉
     （那条命令行里也含这个模式），于是"kill 完顺手启动"的写法会静默什么都不做 ——
     把 kill 与 start 拆成两条命令。
     ⚠ 同一个坑的**另一种表现**（T5 又踩一次）：`pgrep -af 'python3 agent/main' && echo 在跑`
     也会命中自己那个 shell —— 于是"Agent 已停"的画面里报出"Agent: 在跑"（**假阳性**,
     比假阴性更坏: 你会以为它还活着）。可靠的写法是 `ps -eo pid,args | grep -e agent/main | grep -v grep`。
     ⚠ 板端现在的状态：仓库里 T2 的 4 个文件（`agent/llm/provider.py`、`agent/llm/__init__.py`、
     `agent/main.py`、`tests/test_llm.py`）**临时超前于 HEAD**（等 push 后 reset）；
     live config 已是 `edge`；`llama-server` 在跑；Agent 没在跑。

下一个任务列表（**草案，等你审核**）：音乐 / 视频两个工具
□ tools/netease_music.py：cloud-music-mcp 集成 —— 动态歌单、批量加歌、URL Scheme
□ tools/bilibili.py：搜索、order=play 排序、顺序播放、反馈切换
（这两项是已批准的任务列表里**你主动往后放**的：先把工具层的骨架、edge 真后端、壁纸工具
 跑通，再上这两个"要联网 + 要外部服务"的工具。）

T3 记录（已完成，等验收）：壁纸工具 + 接上 next_wallpaper
☑ 先看清事实：**GUI 那边 T8 早就做完了**（`LocalClient::wallpaperReceived` →
     `ViewState` → `MainWindow::setWallpaperFromPath()`，等比缩放 + 居中裁切
     `core::centeredCropRect` + 200ms 交叉淡入 + 读不到给兜底底色与橙字提示，主区右下角
     本来就有「下一张」按钮，`--next-wallpaper-demo` 也在）。缺的**只有 Agent 这边**:
     没人推 `wallpaper`，`next_wallpaper` 还挂在"还没接入"那张表上。
☑ `agent/core/wallpaper.py`（新）：`WallpaperDeck` = 目录 + 游标。**不碰 IPC、不解码图片**。
     · 认 .jpg/.jpeg/.png/.webp/.bmp（不含 .gif）, 按**文件名**排序（编号 01_/02_ 就是给排序用的）
     · **每次调用重新列目录** —— 你可以随时往里丢新图, 不用重启 Agent
     · 游标记的是**当前那张的路径**而不是下标: 新图插在前面也不会让"下一张"跳回去;
       当前那张被删了就从第一张重新开始
     · step=0 = 重推当前这张, 负数是往前翻, 越界**回绕**; 目录不存在/没图片 → `WallpaperError`,
       消息是**给人看的**（会显示到界面上）
☑ `agent/tools/wallpaper.py`（新）：LLM 那个工具（`next_wallpaper`, 允许 IDLE/STUDY）。
     它**只把入参转给** `Runtime.next_wallpaper()` —— 不自己挑图、不自己推 IPC。
     ⚠ 依赖的是**运行时入口**而不是壁纸目录: 目录不存在属于运行期问题, 启动时目录还没建好
     不该让这个工具消失（`build()` 只检查 services）。
     `DESCRIPTION` 写清三件事: 只改显示不动文件 / 是翻页不能指定某张 / 推完没有回执。
☑ 两条入口**共用一份实现**（这是 T3 的结构决定）：
     GUI 的 `next_wallpaper` 命令（`agent/ipc/__init__.py::_handle_next_wallpaper`）与
     LLM 的工具，都调 `agent/main.py::Runtime.next_wallpaper(step)`；真正的语义只在
     `core/wallpaper.py`。成功时**不往对话区写话**（点一次按钮多一条气泡太吵, 界面反馈就是
     壁纸变了）; 失败才推一条 `llm` 说明（目录不存在/没有图片/模块没接进来）。
     `UNWIRED_COMMAND_NOTES` 里 `next_wallpaper` 那条**撤掉**了, 只留 `next_bilibili`。
☑ push 只让 IPC 层认识: `Runtime` 新增 `on_wallpaper(path, index)` 钩子（与 `on_reply`
     同款"有就接"）, `build_ipc` 把它接到 `wallpaper{path,index}` —— Runtime/工具都不认识
     线格式字段。`_make_dispatcher` 本来就是**同步且线程安全**的（`run_coroutine_threadsafe`）,
     所以工具 handler 可以是同步的（ToolRouter 会把同步 handler 丢线程池）。
☑ 配置: 新增 `wallpaper.dir`（默认 `/home/kickpi/wallpapers`）—— `config.example.yaml` 写了
     一节说明; **板端 live config 也补上了这一段**（值就是默认值, 行为不变, 只是写明白）。
     指纹 `598a9d70…` → `34482331…`; `llm.env` 仍没动。
☑ GUI 侧一处小改（T3 收尾）: 壁纸一到, 主区那两行开发占位文字（"T9：游戏模式 = 真视频…"）
     **收起来**（`MainPage::setMainHint("")` = 藏起来; 之前它会压在壁纸上）。
☑ 素材: `scripts/make-wallpaper-samples.py`（新, 只用标准库 zlib+struct, 不依赖 Pillow/ImageMagick）
     造 8 张**纯色**样张, 覆盖 16:10 / 10:16 / 16:9 / 9:16 / 21:9 / 1:2 / 1:1, 板端现场生成到
     `/home/kickpi/wallpapers`（仓库外面, 所以不入库）。
     ⚠ 每张都加了一圈 3px 白边 + 左上角小方块: **纯色块看不出裁切与变形**, 边框才能看出
       "有没有被拉变形 / 有没有居中裁掉两边"。第 8 张是**正中一个白圆** —— 专治"有没有拉伸":
       等比填满时还是正圆, 被拉伸就成明显椭圆。
☑ 板端验收（真 Agent + 真 GUI + 真面板截图）:
     · 启动日志: `ToolRouter 就绪 (2 个工具)` + `壁纸目录 = /home/kickpi/wallpapers (8 张)`
     · 用**真 IPC 客户端**发 `next_wallpaper`: 依次收到
       `wallpaper{"01_landscape_1280x800.png", 0}` … `{"08_circle_1280x1600.png", 7}`
       （7 次连点正好走完一圈, 顺序与文件名一致）
     · `scrot` 截图逐张核对: 主区颜色随图变（蓝 → 深灰 → 深绿）, **占位文字消失**;
       1280x1600 的竖图在 1280x800 屏上**圆还是正圆**（等比填满 + 居中裁切, 不是拉伸）
     · **失败路径**: 把壁纸目录改名再点「下一张」→ 界面聊天区出现
       「换壁纸没成功：壁纸目录不存在: /home/kickpi/wallpapers（先建目录, 或者改配置里的
       wallpaper.dir）」, 原壁纸留在屏上（不崩、不白屏）; 目录改回来后恢复正常
     · **LLM 入口**: `assistant chat "调用 next_wallpaper 工具，参数 step=1"` →
       `工具 next_wallpaper({'step': 1}) -> ok` + `wallpaper: …01_landscape… (index=0/8)`,
       模型答复里引用了**真实路径**
     · ⚠ 如实记一条: 第一次用"帮我换一张壁纸"这种**含糊**的问法时, 0.6B **只说要换、没真调工具**
       （日志里没有工具调用）。明确让它调工具就调对了 —— 这是小模型的可靠性问题, 不是接线问题
       （接线由单测与上面那条钉住）。
☑ 测试: `tests/test_wallpaper.py`（新, 38 项: 列目录/游标/回绕/路径游标/工具转调/状态权限/
     参数校验/IPC 命令成功与失败/运行时入口/启动装配）。更新 `tests/test_ipc_local_server.py`
     里两处把 `next_wallpaper` 当"未接线命令"的用例（改用 `next_bilibili`）。
☑ 文档: `docs/ipc-protocol.md`（§3 加"变化时推、连上不补"的说明; §4 把 `next_wallpaper`
     从"还没接"改成"有下游了"）; `docs/gui-agent-integration.md`（命令表 + 那两段"还没接入"）;
     `docs/gui.md` 主页能力; `docs/config-sources.md`（真源图里加 `wallpaper:` 一节）;
     `docs/architecture.md`（§4.1 加"工具不是唯一调用方"、文件树加 `core/wallpaper.py`）;
     `Readme.md`（目录树 + `test_tools.py`/`test_wallpaper.py`/新脚本）。
     新守卫: `tests/test_docs.py` 的 STALE_CLAIMS 加一条
     `(换壁纸…还没接 | next_wallpaper / next_bilibili 的下游在 Phase 7)` —— 防这两句回来
     （单独验过: 旧句命中、新句与新写的 `next_bilibili` 说明都放过）。
☑ 两个只在**板端**才会红的坑（都记下来）:
     ① `tests/test_ipc_local_server.py` 里"未接线命令走真 socket"那条用的是 `next_wallpaper`
        —— 它一接线就红, 但**PC 上 AF_UNIX 不可用会被 skip**, 所以只有板端套件能抓住。
     ② `Runtime.__init__` 要建 `asyncio.Event()`, 而 **Python 3.8（板端）不允许在没有运行中的
        事件循环时建它** —— 我在 `TestRuntimeWiring` 里写成同步用例, PC（3.14）全绿、板端全红。
        改成 `IsolatedAsyncioTestCase` 就对了。
     教训沿用 T2 那条: 板端套件不是"再跑一遍 PC 套件", 它是**唯一**能抓这类差异的守门人。
☑ 四端: PC python `python tests OK`; PC native ctest 172/172; 板端 python **21 个文件 OK**;
     板端 GUI ctest **19/19**（GUI 改动重建后再跑）。`assistant doctor` 6 项全 OK。

T4 记录（已完成，等验收）：状态权限表
☑ 先把矛盾问清（你拍的板）：我手上那份计划写的是"GAME only back_to_desktop"，而 T1 已验收的
     决定是 back_to_desktop **只在 STUDY**（工具说明里也写着"别在 GAME/IDLE 里乱按"）——
     两者对不上。问过你之后定的是：
     | 状态 | back_to_desktop | next_wallpaper |
     | SLEEP | ✗ | ✗ |
     | IDLE | ✗ | ✓ |
     | STUDY | ✓ | ✓ |
     | GAME | ✗ | ✗ |
     即 **SLEEP/GAME 一个工具都不给**（SLEEP = 什么都别动; GAME = 主区是视频, 换壁纸白换,
     回桌面是"学习收尾"的动作）。
☑ 代码其实已经符合这张表（T1/T3 各自定的）—— 所以 T4 主要是**把它钉住**, 外加修一处名不副实:
     `ToolRouter.allowed_tools()` 的说明一直写着"给 LLM 看的清单按状态过滤是权限控制的一部分",
     但 `provider._chat_with_tools()` 调的是 **`list_tools()`**（不过滤）。改成
     `_advertised_tools()`: 有 `allowed_tools()` 就用它。于是 SLEEP/GAME 下模型**根本看不到**
     任何工具候选（而不是"看得见、一调就被拒、白花一轮"）。执行期的 fail-closed 校验照旧 ——
     这是**少给**, 不是放宽。
☑ `tests/test_tool_permissions.py`（新, 14 项）: `EXPECTED` 是唯一写下来的权限矩阵,
     双向对齐（注册得到的工具必须在表里 / 表里的工具必须注册得到 —— 加工具时忘了决定就会红）；
     逐个状态验 `allowed_tools()`; 4 状态 × 每个工具**跑一遍** `execute()`, 要求
     禁止的组合被拒 **且 handler 一次都没跑**（依赖换成计数替身）、允许的组合正好跑一次;
     另验 `list_tools()` 不随状态变（"注册了什么"与"现在能用什么"别混）;
     以及"给模型的清单"三种形态（STUDY 两个 / IDLE 只有一个 / SLEEP 与 GAME 连 `tools` 键都没有）。
☑ 文档: `docs/architecture.md` §4.1 加权限表 + 三条理由 + "加工具先在这张表里决定";
     `docs/llm.md` §3 注明清单按状态过滤; `Readme.md` 测试清单。
☑ ⚠ 写测试时自己踩的坑（记下来免得再犯）: `go_to()` 一开始按"从初始状态出发的路径表"写,
     而**状态机的初始状态是 IDLE**（不是 SLEEP）—— 于是 SLEEP 之后走到 IDLE 时原地不动,
     三条用例红得莫名其妙。改成"先退回 IDLE 再进目标状态"就对了。
     另外编辑时留下过一个**重名的旧 `go_to`**, Python 取后定义的那份, 表现是"新代码明明写对了
     却还是老行为" —— 同一文件里改函数时, 先确认没有第二份定义。

T5 记录（已完成，等验收）：Phase 7 端到端验收 + 文档收口
☑ 一段脚本跑完的端到端验收（`logs/t5_accept.sh`, 板端 /tmp 里跑的, 输出见验收报告）:
     [0] 前置: live 配置指纹 `34482331…`; llama-server /health = ok
     [1] 起 Agent（**真跑**, 连真主机）: `io 层就绪` → **`ToolRouter 就绪 (2 个工具)`** →
         **`壁纸目录 = /home/kickpi/wallpapers (8 张)`** → `LLMProvider 就绪 (mode=edge)` →
         `llm: edge 后端 = llama-server http://127.0.0.1:9000/v1 model=qwen3-0.6b
         max_tokens=512 temperature=0.7 no_think=True` → `Scheduler 就绪 (4 条日程)`
     [2] 起 GUI（DISPLAY=:0）: Agent 侧 `ipc: GUI 已连接 (当前 1 个)`
     [3] **GUI 按钮那条路**: 真 IPC 客户端发 `next_wallpaper` → 收到
         `wallpaper{"01_landscape_1280x800.png", 0}` → 面板截图确认主区换色
     [4] **LLM 工具那条路**: `assistant chat` → `工具 next_wallpaper({'step': 1}) -> ok`,
         模型答复引用**真实路径**（"壁纸已更新为 "/home/kickpi/wallpapers/02_portrait_800x1280.png""）,
         面板截图确认壁纸真的换了
     [5] **权限表**（真 Runtime、真配置、真目录）: sleep/idle/study/game 四行的"能用"与"给模型的
         候选"都与 T4 那张表一致（SLEEP/GAME 两边都是空）
     [6] **降级**: 停 llama-server → 答复带「（板端模型没有响应，这条是规则兜底）」+
         日志 `WARNING LLM 降级为规则兜底: APIConnectionError`; 起回 llama-server → 又是真答复
     [7] 收尾: Agent/GUI 已停, llama-server 留着（edge 要用）; live 配置指纹与 [0] **完全一致**
         （全程没人写配置）; 板端仓库在 HEAD 上干净
☑ 文档收口（这一轮把"说 Phase 7 还没做"的旧话清干净）:
     · `Readme.md`: §"工具路由"补 `allowed_tools()` 与权限表、把"`agent/tools/`（工具为空）"
       改成真现状（有 2 个工具, 缺依赖只跳过那一个）
     · `docs/architecture.md` §4.1: 补一条**边界** —— 这张权限表管的是"模型能做什么",
       不管"人点了什么": GUI 的 `next_wallpaper` 命令**不做状态校验**（按钮在 GAME 下本来就藏起来,
       且"人显式点的"与"模型自己决定的"是两回事）。要收紧只改 `_handle_next_wallpaper` 一处
     · `docs/llm.md` / `docs/gui-agent-integration.md` / `docs/ipc-protocol.md` 已在 T2/T3 收过,
       这一轮通读确认没有"还没接入"的残留（`STALE_CLAIMS` 那条守卫也盯着）
☑ Phase 7 剩下的（**明确记下来, 不藏着**）:
     · `tools/netease_music.py` / `tools/bilibili.py` —— 你主动往后放的那两项（下一个任务列表）
     · **标签化 / 按内容挑图**: 现在壁纸工具只能"按文件名翻页"; 要"挑一张适合现在心情的图"
       得先给图片打标签（视觉层），那是独立的一块

T6 记录（已完成，等验收）：把 T5 列的两个"可选收口"做掉
☑ ① **GUI 命令也受状态权限表约束**。`next_wallpaper` 既是工具名也是命令名, 而"这个状态下
     能不能换壁纸"只有**一个**答案 —— 否则按钮就是绕过状态表的后门。
     做法: `ToolRouter.allowed_in_current_state(name)`（新, 通用小接口: 返回 None 或一句给人看的
     原因）; 命令路径 `_handle_next_wallpaper` 在调 `Runtime.next_wallpaper()` **之前**问一次,
     被拒就回一条 `llm` 说明。
     · ⚠ **没有**把这层判断写进 `Runtime.next_wallpaper()`: 那会让 main.py 必须知道
       "哪个工具对应这个动作"（本文件一直坚持不认识具体工具）。判据仍然只有一处
       （工具上的 `ALLOWED_STATES`）, 只是"借出去给命令用"。
     · 工具没注册（缺依赖没装）时**不拦** —— 那是"工具没装", 不是"这个状态不允许"。
     · "补推"（下面那条）**不受**这张表约束: 它是"同步显示", 不是"换一张"。
☑ ② **新 GUI 连上就补推当前壁纸**。做法:
     · `LocalServer(on_client_connect=...)`（新参数, 普通/协程函数都行）在 `_on_client` 里
       **读到第一条命令之前**叫一次; 回调抛异常只记 WARNING（补推失败不该弄断刚建立的连接）。
       `NullServer` 也收下这个参数（接口齐全）。
     · `build_ipc` 里把 `runtime.push_current_wallpaper` 挂上去（与 `on_reply`/`on_wallpaper`
       同款"有就接"）。
     · `WallpaperDeck.snapshot(initialise=False)`（新, 纯读不换图）+ `Runtime.push_current_wallpaper()`:
       有当前那张就补那张; **从来没换过就顺手选第一张**当初始画面（只动游标）。
       目录空的/读不了 → 返回 False, 不抛。
☑ 板端验收（`logs/t6_accept.sh`）:
     · GUI 一连上, Agent 侧立刻出现 `wallpaper: 给刚连上的 GUI 补推
       /home/kickpi/wallpapers/01_landscape_1280x800.png (index=0/40, pushed=True)`,
       GUI 自己打 `[ui] 壁纸: …/01_landscape_1280x800.png (1280x800, index=0)` —— **没点任何按钮**
     · 切 SLEEP 后发 `next_wallpaper`: 收到 `llm{"text": "换壁纸没成功：next_wallpaper 在当前
       状态（sleep）下不可用（可用状态: idle, study）"}` + 日志
       `WARNING ipc: next_wallpaper 被状态权限表拒绝: …`, **没有**新的 wallpaper 推送
     · 切回 IDLE 再发: 真的换到 `02_portrait_800x1280.png (index=1/40)`
     · live 配置指纹全程没变（`34482331…`）; Agent/GUI 收尾已停, llama-server 留着
     · ⚠ 顺带发现: 你的壁纸目录里现在有 **40 张**（你自己放的那批）, 所以 index 是 0/40 ——
       工具按文件名排序翻页, 所以"01_…"那几张仍排在最前面
☑ 测试: `test_tool_permissions.py` 加 `TestTheGuiCommandFollowsTheTableToo`（4 个状态逐个走
     **真 Runtime**: 表说不行就必须被拒且**真的没换**）+ `TestTheConnectPush`;
     `test_wallpaper.py` 加 `TestSnapshot`(5) + 5 条补推用例; `test_ipc_local_server.py` 加
     两条**真 socket**用例（连上就触发回调 / 回调炸了连接照样活着）。
     ⚠ 板端又抓到一条只在真 socket 上才复现的问题: 第二条用例我一开始给命令处理器传了
     空 push（什么都不写回客户端）→ 客户端等到超时。改成走 `build_ipc` 真装配就对了 ——
     "用手搓的替身替换真装配"这类测试, 容易把**被测的那条链**也替掉。
☑ 文档: `architecture.md` §4.1（同名命令受同一张表 + 补推不受限, 把 T5 写的"不管人点了什么"
     那条**改掉**）、`ipc-protocol.md`（§3 补推说明、§4 命令受状态表约束）、
     `gui-agent-integration.md`（连上就有一张 + 受状态表约束）、`Readme.md` 工具路由一节。
     `STALE_CLAIMS` 那条守卫继续盯着"还没接入"的旧话。

T7 系列（标签化）：方案与任务列表已定稿（用户审核通过），分期如下
     T7-1 引擎入库（SigLIP 进仓库 + 配置归位 + 对齐）→ **已完成，见下面记录**
     T7-2 打标签 + `config/wall_data.jsonl` + `assistant tag`
     T7-3 Agent 侧：标签索引 + `list_wallpaper_tags` / `next_wallpaper(match=…)`，
         并**删掉 GUI 与 CLI 的手动换壁纸入口**（换壁纸只能通过对话）
     T7-4 板端验收（三轴命中率 / IP 锚点检索 / 对话挑图 / 如实拒绝）+ 文档收口
     方案要点（复查用）: 用板端已验过的 **SigLIP 零样本分类**（1.3–1.7 s/张，NPU）,
     三轴 `scene`/`tone`/`mood` + 第 4 轴 **`ip` 检索**（用用户给的 11 张确认图当**锚点**,
     不靠"模型认识作品名"）; 标签落 `config/wall_data.jsonl`（含预存 embedding →
     `ip` 检索是纯 CPU）; 12 张真值验收集**只入库真值 + 脚本**（图片不入库）。

T7-1 记录（已完成，等验收）：SigLIP 引擎搬进仓库 + 配置归位 + 对齐
☑ 先侦察再动手: 板端**已经有**一份实测验证过的 SigLIP 双塔实现（`sig/siglip/`, 板端本地
     实验树、不在仓库）+ 29 条单测 + 黄金参考向量 + `sig.env` 里写满实测踩过的坑
     （int8 不可用、0-1 浮点会静默劣化、core_mask 只 RK3588 支持、tokenizers 版本…）。
     模型 `siglip_full.rknn` fp16; 实测 **1.3–1.7 s/张（NPU）**; `llm/multimodal_report.md`
     里还有现成对照（VLM 路线 649 s/张 → 本期不用）。所以 T7 的引擎不是从零写,
     而是**把已验过的搬进仓库 + 换配置来源**。
☑ `agent/vision/siglip/`（新, 5 个模块从 `sig/siglip/` 搬入）: `errors.py` / `config.py`
     （**重写**）/ `runtime.py` / `tokenizer.py` / `model.py`; 入口从
     `SiglipModel.from_env()`（读 `sig/config/sig.env`）换成
     `SiglipModel.from_config(config.yaml 的 vision: 段)`。
☑ **配置边界**（T7-1 的核心决定）:
     · 能配（`vision:` 段）: model_path / tokenizer_path / runtime_lib / verbose / warmup_runs
     · **写死（代码常量）**: 256×256、3 通道、nhwc、**uint8 原始 0-255**、64 token、pad=1、
       768 维、L2 归一化、只能按余弦 —— 这些值"改错不报错、只会静默变笨"
       （实测: 喂 0-1 浮点 → 不同图余弦全挤 0.98+; int8 模型文本塔 → 不同文本几乎同向量）。
       `validate()` 顺手把 `warmup_runs < 0` 也拦了（原版没查, "-1 次预热"没意义）。
☑ `tests/test_siglip.py`（新, 31 项）: 配置契约（含"常量冻结"用例）/ 自相矛盾的配置要被
     `validate()` 拦下 / 分词（padding、截断、空串、批量）/ 门面（形状、单位长度、文本缓存、
     **每次新建 data_type list** 的回归、余弦排序、close 只管自己加载的 runtime）/
     板端集成（形状、确定性、判别力）。
     ⚠ 开发机上**没有 numpy**（仓库里的 `conda/` 也没有）→ PC 只跑 12 项、19 项 skip；
     配置层与分词层的纯逻辑不依赖 numpy，两边都能跑（与 `tests/test_vision.py` 同一口径）。
☑ 搬运**对齐证据**（`tests/board/siglip_align.py`, 新）: 同一张图分别过**仓库版**与 `sig/` 原版,
     比 image embedding 余弦 + 零样本排序 + 逐位差。板端实测三张
     （01_landscape / 02_portrait / 06_extreme）: **余弦 1.000000、逐位差 0.00e+00、
     排序完全一致**; 文本塔余弦 1.000000 → **搬运没有改变行为**。环境不满足时退 2
     （不算失败也不算通过）并说清缺什么。
☑ 板端 live config 补上 `vision:` 段（值 = 代码默认值, 行为不变）:
     指纹 `34482331…` → `f0e29d81…`; `llm.env` 没动。
☑ 板端 `tests/test_siglip.py` **31 项全绿**（真 NPU, 28.6 s 含模型加载）。
     ⚠ 板端当场抓到一条**我自己测试写错**的: 判别力用例把纯蓝样张描述成 "a landscape",
     模型诚实地选了 "a blue sky with clouds" —— 改成照实描述（"a solid blue rectangle"）才对。
     教训: 拿合成样张做判别力回归时, 描述必须是**像素的实话**。
☑ 一处**有意的偏离**（对原版的小改进, 2 行）: `tokenizer.encode()` 原来先 `import numpy`
     再校验文本, 于是"空串/类型不对"在没 numpy 的机器上报 ModuleNotFoundError（与调用方
     无关的错）。改成先 `tokens(text)` 校验、再 import numpy → 永远报 SiglipTokenizeError;
     对齐测试证明正常输入下数值完全一致。
☑ 文档: `docs/architecture.md` §4.3（**两条 SigLIP 路径**别混 + 三条规矩）;
     `docs/config-sources.md` §2.2（`vision:` 段只给离线打标签用 + 契约不在配置里）;
     `Readme.md` 目录树; `config.example.yaml` 的 `vision:` 段（含 int8 不可用的警告）。

T7-2 记录（已完成，等验收）：打标签 + `config/wall_data.jsonl` + `assistant tag`
☑ `agent/vision/tag_vocab.py`（新）: 三轴 **32 条**标签（scene 16 / tone 8 / mood 8），
     英文裸标签 + 中文显示名；`vocab_sha8()` 指纹（改词表 → 标签作废）；
     `with_overrides()` 只**追加**（配置里删不掉默认标签，想删就改代码）。
     标签必须是英文: SigLIP-base 是英文图文对训出来的，中文标签对它没有意义。
☑ `agent/vision/wall_data.py`（新, **纯 Python 不 import numpy**）: 数据文件的唯一写者。
     · 记录: path/w/h/bytes/sha256/tagged_at/ms/model_sha8/vocab_sha8/tags/**embedding**
     · 向量用 `struct` 的 float16 + base64（解码也是 struct）→ 40 张 ≈ 234KB
       （**第一行词表** ≈ 131KB + 一张图 2.6KB）；**纯 Python**，开发机也能读/检索
     · **读坏了不装死**: 单个坏行跳过并记一句，其余照读；文件不存在 = 空表（不是错误）
     · 写: 整体重写 + `write_text_atomic`（全仓唯一原子写）+ `.bak`
     · 增量: `tag_plan()` 给每张图一个原因（新图/图变了/模型变了/词表变了/格式变了/强制重打）
       + 孤儿行（图没了）+ `prune_records()`
☑ `agent/vision/tagger.py`（新, 板端）: 逐张解码 → **官方口径预处理**（BGR→RGB、压扁到
     256×256、原始 0-255）→ 图像塔 → 三轴 top-k（默认 3）+ 词表向量**预编码一次**复用。
     依赖（numpy/cv2/rknnlite/tokenizers）全部延迟导入 + `check_dependencies()` 说清缺什么。
☑ `assistant tag`（第 8 条命令, `agent/cli.py`）: 默认 dry-run；`--apply/--force/--limit/
     --dir/--data-file/--top-k/--prune`；**逐张写盘**（跑一半崩了不丢已打好的）；
     结尾报"每张中位耗时 / 总耗时 / 失败数 / 模型加载 / 词表编码"。
☑ 连带三件（都做了）: `.gitignore` 加 `config/wall_data.jsonl`（它住 config/ 但后缀不是 .yaml，
     那条盖不住）；`test_config_source_guard` 的 `ALLOWED_WRITERS` **显式**加
     `agent/vision/wall_data.py`（第三个写入者，写的是派生数据不是真源）；
     `docs/config-sources.md` §2 表格加一行 + 写明"config/ 下现在有两类东西"。
☑ 板端实测（40 张, 真 NPU）: **40/40 成功，总 205.7 s，中位 3.56 s/张**
     （其中**词表编码 61.2 s**、模型加载 2.6 s）；数据文件 108KB（当时还没有词表头）；
     第二次跑 `assistant tag` → **"已是最新 40 张"**，只花 2.0 s（dry-run 不编码词表）。
     实现在"逐张写盘"上是对的: 中途崩了只丢当前那张。
☑ **质量量化**（`tests/board/tag_quality.py`, 新; 真值 `tests/data/wallpaper_tags/truth.json`, 新）:
     · 真值 12 张（11 张有 IP），**我自己按 4×3 联系表逐张标注**（`logs/eval_sheet.png`），
       按约定等你只改错的
     · 命中率: `scene` top-1 **58%**/top-2 83%; `tone` 42%/58%; `mood` 42%/58%
     · **预处理 A/B（官方压扁 vs 中心裁切）: 7/5/5 vs 7/4/4 → 保持官方口径**, 不加开关
       （这条纠正了我 earlier 的口误: 官方 preprocessor_config 就是压扁, 不是保比例）
     · IP 锚点检索: LOO EVA 3/40、GitS 2-3/40、Nier 10-15/40；跨 IP 前 5 名里 own 排最前、
       后面接"无 IP 真值"的同类动画图。**去均值/PCA 白化基本不改善** → 保持原始余弦。
       如实结论: **可用但不稳**, 每个 IP 给 3–5 张锚点会明显更稳
☑ 两条**自己踩的坑**（都记进 tagging.md 了）:
     ① 测量脚本第一版 LOO 写成 `cosine(target, proto)` 放在候选循环里 → 40 个候选**同分**,
        "排名 31"只是按文件名排的假结果。是两个实现对拍（numpy 版 vs 纯 Python 版）
        才发现不一致的 —— 现在脚本会打印"候选分数是否唯一"，同分直接标"排名无效"。
     ② `os.path.join(repo_root, "config/wall_data.jsonl")` 会混出两种分隔符
        （`.../assitant\config/wall_data.jsonl`）→ `resolve_data_file` 一律过 `normpath`。
☑ 文档: `docs/tagging.md`（新: 词表 / 数据文件 / IP 锚点检索 / 实测数字 / 边界 / 怎么验）;
     `docs/cli.md`（八条命令 + `tag` 一节 + "不手动换壁纸"写进边界）;
     `docs/config-sources.md`（第三个写入者 + 派生数据一类）; `Readme.md`（目录树 + 测试清单）。
☑ 测试: `tests/test_wall_data.py`（新, 31 项: 词表/指纹/编码解码/读坏行/备份/原子写/
     增量计划/摘要/路径解析）; `tests/test_cli.py` 加 `TestTagCommand`（8 项: dry-run 不写、
     配置优先级、--limit 校验、缺依赖说清、--prune、目录/配置读不到）。
     ⚠ 这些**都能在开发机跑**（不碰模型）；板端跑同一份。
☑ **词表向量缓存（你指定的补充项: "把标签向量按指纹存进数据文件第一行"）**——已做:
      · 数据文件**第一行**变成 `{"kind":"vocab", version, model_sha8, vocab_sha8, dim:768,
        axes:{轴:[标签…]}, embeds:{轴:[base64 float32 ×768]}}`，第二行起照旧一行一张图
      · **指纹 = model_sha8 + vocab_sha8 + 每轴标签列表逐条相等**；对不上就不用缓存、
        重编一遍再覆盖第一行；`embeds` 解不开（base64 坏）也当没有缓存 —— 宁可贵 61 s，
        不肯用一份错位的缓存（`vocab_matches()` / `vocab_vectors()` 各有一段测试）
      · **词表向量用 float32、图像向量留 float16**（故意的）: float32 往返逐位精确 →
        "复用缓存"与"现场重编码"算出的分数**完全相同**，不会出现"看着一样、数字差 1e-3"
      · 板端实测: 首次写头 **67 s**（"本次编码 61.3s"）；之后增量打 1 张图 **5.8 s**、
        2 张图 9.7 s，报告里明确写"词表 **复用缓存**"（dry-run 也先说"复用数据文件里的缓存"）
      · 顺手验了增量本身: 删掉 1 条记录 + 改 1 张图的字节 → 计划准确说"要打 2 张
        （图变了 1，新图 1）"，打完 40/40 齐全；改图那一次**已按原字节复原**（追加的 1 字节删掉）
      · **faithfulness 实测**（这条最要紧）: 缓存解出的 32×768 = **24576 个分量** vs
        现场重编码（60.6 s）逐个比 → **最大绝对差 0、float32 逐位不同的 0 个**。
        也就是说"复用缓存"不是"差不多"，是**同一个值**
      · 代价: 文件从 108KB 涨到 **234KB**（多出来的是第一行 131KB）—— 换来的是每次 61 s
      · 顺手验了 `--apply` 的**空跑**: 40 张都最新 + 缓存最新 → 打印"什么都不用做"，
        文件 mtime/大小**一动没动**（不白改文件、不白留 .bak）
      · 测试: `tests/test_wall_data.py` 加 `TestVocabRecord`（10 项: 头必在第一行、第二条头记问题、
        指纹各种对不上、手改 axes、float32 逐位、base64 坏 → 无缓存、dtype 拒绝）
⚑ **顺手发现一处与 T7 无关的红**（板端跑 `scripts/run-board-tests.ps1` 时看见的，先记账不动手）:
      `tests/test_llm_integration.py`（**板端本地文件**, 09-21 写的, 不在仓库里）9 项里
      `test_chat_raises_on_bad_key` 报错: 它期望 `llm/llm_client.py::LLMClientError`，
      但 `provider.chat()` 现在**照抛** openai 的 `AuthenticationError`（`_plain_chat` 的约定就是
      "失败照抛"）。同文件的 `chat_with_tools` 那条（错误收进 `result["error"]`）是**通过**的。
      两个修法: ① 在 `chat()` 边界把第三方异常包成 `LLMError`（更干净, 但要改仓库代码 + 加测试）;
      ② 改掉那条板端本地测试的期望（它写于切换到 openai 适配器之前）。
      **T7 没碰 `agent/llm/`，这不是 T7-2 引入的**；要不要修你说一声，我按你的选择做。

T7-3 记录（已完成，等验收）：标签索引 + 两个挑图工具 + 删掉手动换壁纸
☑ `agent/vision/tag_index.py`（新, **纯 Python**: 不 import numpy、不碰 NPU、不读配置）:
      · `TagIndex.from_file()` 读 `config/wall_data.jsonl`（文件不存在 = 空库, 不是错误）;
        坏向量只让那一张"降级成只按标签用"，其余照旧
      · 统计: 每轴各标签几张（`label_counts(top_k=3)`，与打标签时的 top-k 对齐）
      · 排序: **用第一行的词表向量现场算余弦** —— 所以"没进 top-3 的标签"也能算真实分数
        （`rank_by_label`；没有词表头时退回"只看存下来的标签"并如实说明）
      · IP 锚点: 锚点向量求平均 → L2 归一化 → 全库余弦；**锚点没打过标签的会列出来**
        （不假装它参与了），一个都用不上就报"先跑 assistant tag"
      · `match` 语法: `轴=标签` / `只写标签名`（跨轴找 + 按分数去重）/ `ip=名字`
        （大小写不敏感）；**写错一律如实报错并列出可选值**（轴名/标签名/ip_presets 名字）
      · 措辞刻意避开"命中 N 张": 分数**没标定**（见 tagging.md §7），说成"全库按相似度排序"
☑ 工具两个:
      · `agent/tools/wallpaper_tags.py`（新）`list_wallpaper_tags(ip_query?, limit)` —— 只读,
        给模型"先看清单再挑"的能力（词表是配置决定的, 模型看不见）
      · `agent/tools/wallpaper.py` 加 `match=`（透传给 Runtime）；description 写明三条写法
☑ `Runtime`（`agent/main.py`）: `tag_index()`（**懒建** + 缓存，启动只查文件在不在并打一行日志）、
      `wallpaper_tags(ip_query, limit)`、`next_wallpaper(step, match)`;
      `core/wallpaper.py::WallpaperDeck.step(step, pool=…)` —— **候选顺序即优先级**,
      候选里已经不在目录里的会被丢掉, 一个不剩就如实报错
☑ **删掉手动换壁纸**（你的需求）:
      · GUI: 主区右下角「下一张」按钮、`nextWallpaperRequested` 信号、`nextWallpaperButton()`
        访问器、`MainWindow::demoNextWallpaper()`、`--next-wallpaper-demo` 与它的 QSS 规则
        （`gui/src/{main.cpp,main_window.cpp,main_window.h,ui/pages.cpp,ui/pages.h}`）
      · Agent: `COMMAND_NEXT_WALLPAPER` 常量与 `COMMANDS` 里那一项、`_handle_next_wallpaper()`,
        `NO_WALLPAPER_NOTE`, 以及只服务于它的 `_state_refusal()` / 
        `ToolRouter.allowed_in_current_state()` —— 记明**T6① 因此被撤掉一半**
        （现在权限表只管工具, 而工具只有对话一条路能调到, 答案仍然只有一个）
      · 顺手修了一处误用: `gui/src/ui/sys_page.cpp` 的看门狗按钮原来借 `#NextWallpaper`
        的样式（objectName 就写的 NextWallpaper），现在有自己的 `#WatchdogButton` 规则
☑ 测试: `tests/test_tag_index.py`（新, **51 项**: 读文件/统计/排序/锚点/match 语法/报错文案/
      向量小工具）; `tests/test_wallpaper.py`（64 项: 加 `pool` 与 match 透传, 把"手动入口已删除"
      钉成断言）; `tests/test_tool_permissions.py`（表加 `list_wallpaper_tags`,
      禁止组合 5→7、允许组合 3→5, 新增 `TestTheRevertedT6Rule`）;
      `tests/test_ipc_protocol.py`（命令 5→4）; `tests/test_ipc_local_server.py` / `test_ipc.py`
      改用剩下的命令; 两个测试清单脚本都加了 `test_tag_index.py`
☑ 板端实测（真 40 张库, 真 NPU 无关）:
      · 索引 40 张 / 三个轴 / 词表向量在, **读盘 19 ms**; `scene=anime` 排序 **34 ms**、
        `ip=EVA` **32 ms**（锚点 2 张 → 原型检索, top-1 两张锚点各 0.927）
      · 如实报错逐条验过: `scene=cyberpunk`（列出 16 个真标签）、`weather=sunny`
        （列出三个真轴 + 提示 IP 怎么写）、`ip=ZZZ`（列出 8 个真 IP 名）
      · `ip=EVA` + `step=1` → 最像的那张; 再 `step=1` → 第二张（候选按相关度翻）
      · GUI 截图确认主区右下角**没有按钮了**（`/tmp/t73_gui.png`）; 板端 ctest **19/19**
      · 板端 python 套件 **25 个文件全过**; PC 套件全过
☑ 顺手修了 `tests/test_cli.py` 里**两处与 T7 无关的假红**（都是环境依赖, 不是被测代码的错）:
      ① **跨午夜**: 用例拿 `datetime.now() - 5min` 当"今天刚过去的时间点", 本地时间在
         00:00–00:04 跑时它会落到**昨天**, 写进配置就成了"今天的将来" → "已过"断言假红
         （2026-09-23 00:00:46 实测踩到）。加 `_just_passed()`: 跨日就退到 00:00
         （此刻最晚 00:04, 一定还在最近 30 分钟的尾巴里）。
      ② **默认 socket 上正好有 Agent 在跑**: `TestScheduleCommand` 的用例不给 `--socket`,
         板端验收现场 Agent 正跑着 → CLI 真问到了触发记录, 页脚换成"来自运行中的 Agent",
         于是"不代表已触发"的断言假红（实测踩到）。改成默认指向一个不存在的 socket ——
         这类用例的前提本来就是"问不到 Agent"。
         **修完在"Agent 正跑着"的板端重跑 `test_cli.py`: 107 项全过**（之前 1 红）。
      ⚠ 附带一条命令踩坑记录: `grep -e 'agent/mai[n]'` **匹配不到** `python3 -m agent.main`
         （那是点不是斜杠）, 于是"进程数 0"是假的 —— 用 `agent[.]main` 才准。
⚑ **发现一个真问题（模型层, 不是机制层）—— 直接说清, 不粉饰**:
      用真 0.6B 模型（edge）跑了三句对话, 机制每次都对, 但模型的三个毛病:
      ① 它把 `tone` 的标签当成 `scene` 的用（`next_wallpaper(step=0, match="scene=darkness")`
         → 工具**如实报错**"scene 轴上没有 darkness，可用的有 …"）,
         **但它在回复里照样说"已更换为宁静的深色风景"**（壁纸其实没变）
      ② 它用 `step=0`（"重推当前这张"）而不是 1 → 即使 match 命中了也**推的还是当前那张**
         （日志: `match=anime` → index=34/40 推回 01_landscape）
      ③ 它把一句问句当参数: `list_wallpaper_tags(ip_query="这个作品最像哪几张")`
         → 工具如实报错并列出 8 个 ip 名, 它就把那 8 个名字当成"壁纸标签"答给用户
      **三个可行的修法（都很小, 等你拍板再动）**:
      (a) `match` 给了时 `step=0` 改成"从头挑（最像的那张）"而不是"重推当前" ——
          按钮删掉后 step=0 已经没有"补推"的用途, 而模型两次都用了 0
      (b) 把**当前词表**写进 `next_wallpaper` 的 description（"可用标签: scene=landscape/city/…;
          tone=dark/…; mood=calm/…"）—— 从源头堵住 `scene=darkness` 这种编造
          （词表在配置里, 工具 build 时就能拿到）
      (c) 工具失败时在结果里带一句**祈使句**（"换壁纸没有成功, 请把这句话原样告诉用户"）,
          并且/或者由 Agent 自己再推一条 `llm` 说明（像旧命令那样）——
          保证"失败了"一定出现在对话区, 而不是指望模型转述
      → **你的决定: 只做 (b), 而且"先提交 T7-3, (b) 留到下次提交"**。
        所以 T7-3 这一笔不含 (b); (b) 记在下面 T7-4 的开头, 与板端对话验收一起提交。
        (a)(c) 暂不做（先看 (b) 能不能把"编造标签"这条堵住）。

T7-4（进行中）：**先做你选的 (b)**，再做板端对话验收 + 文档收口
☐ (b) 把**当前词表**写进 `next_wallpaper` 的 description（词表在 `wallpaper.tagging.vocab`
      与 `tag_vocab.py`，工具 build 时从 services['config'] 拿到；要求: 轴名 + 每轴标签全列,
      写不下就截断并说明"完整清单用 list_wallpaper_tags"）—— **这一笔与 T7-4 一起提交**
☐ 板端对话验收: 三轴命中率 / IP 锚点检索 / 对话挑图 / 如实拒绝（含"SLEEP/GAME 下换不了"）
☐ 文档收口（tagging.md §6 的措辞按 (b) 之后的实际行为复核）

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