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
     （sunshine-pairing-findings §5.1、architecure、decoder-mpp、Readme 目录树）与
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
     · docs/architecure.md、Readme.md 里声称"主机键盘 → Host Input RB → pybind11"的图与表
   板端 test_binding_api.py 反过来断言 host_input_rb **不存在**，防止它被带回来。

项目归一化处理（新增，与 Phase 6 并行）
☑ 唯一真源收敛：**已完成**（B1 `a364269` + B2 `5333bff`）。ipc-protocol.md 加了 §0 划清真源边界
     （只管线上格式）；字段定义表只留协议文档一份，gui-agent-integration.md 降级为"GUI 行为 + 指针"；
     Readme 删掉"实现未做"并补上命令信封。另加 tests/test_docs.py 文档守卫（相对链接 + 过时说法黑名单），
     已注册进三端共享的套件清单 —— 以后漂移会直接让测试变红。
     还发现并修掉：Readme 的 ZeroMQ 约定、architecure.md 整篇旧设计（ZeroMQ + PC 侧 GUI）。
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
       板端不入库清单 / 常见操作 / 踩过的坑），`docs/architecure.md` §2 §8.2 §9.2 同步，
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
     `architecure.md` 同步。
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

Phase 7 — 工具层
□ tools/base.py：工具基类（name、schema、execute、权限、allowed_states）
□ tools/init.py：工具注册入口
□ core/tool_router.py：注册、权限、调度（Phase 3 已建骨架，此处完善）
□ tools/back_to_desktop.py（原 lockscreen.py）：调 InputSender.show_desktop()（= WIN+D），仅 STUDY 未完成时
     为什么不是锁屏：Windows 过滤**合成输入**的 Win+L（实测连 host 本机 keybd_event 合成也锁不上，
     与 Ctrl+Alt+Del 同属安全动作）→ 按你的决定改成"回到桌面"，而 WIN+D 在真机上是通的。
     机制已经在 `agent/io/input_sender.py::InputSender.show_desktop()` 里（Phase 6 收尾时就位）。
     ⚠ 它是**开关**且没有回执：实测连按两次里有一次没落地（第 2 次丢了，第 3 次才还原），
       而且那次还原的截图字节数与基线不同（1352621 vs 1718395，右侧的 VS Code 没回来）。
       所以工具层要么发完校验效果（例如看 image_rb 或让主机回执），要么别把它当幂等动作使。
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