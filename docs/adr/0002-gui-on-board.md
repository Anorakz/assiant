# ADR 0002 — GUI 是板端的 Qt5 C++ 程序，不是 PC 上的客户端

- **状态**：已生效（Phase 5 起；本文 T14-1 追记）
- **影响面**：`gui/`（整棵树）、`scripts/sync-gui.ps1`、`docs/gui.md`、`docs/architecture.md` §4.3

> ⚠ 本文按**现有代码与文档**追记（T14-1），不是当时的会议记录。能指出出处的都标了
> 路径；指不出出处的写成"当时的判断"。

## 背景

这台设备的主体是 RK3568 板子：**它自己带屏、带触摸**，串流来的 PC 画面也显示在它上面。
"助手"要能在**不依赖 PC** 的情况下用起来：起居（时间/日程）、放歌、放视频、调模式、
看状态 —— 这些都不该要求"先开 PC"。

PC 在这套系统里的角色是**资源提供方**，不是操作台：

- Sunshine 把 PC 桌面串过来（板子解码显示）
- `neteasecli` 在 PC 上出声（板子用 ssh 指挥，见 `agent/net/netease_cli.py`）
- 认游戏时要问 PC"跑着什么进程"（`agent/net/pc_probe.py`）

## 决定

GUI 做成**板端的 Qt5 Widgets 程序**（`gui/`，C++17），在**板端本机编译**；
它与 Agent 之间只走本机 IPC（ADR 0001）。

- 板端依赖：Qt 5.12（板子本来就有）+ `libyaml-cpp-dev`（日程区**只读**解析用）
- 编译：`scripts/sync-gui.ps1` 送源码到板上 → `cmake -S gui -B gui/build && cmake --build`
- 运行：默认**全屏 kiosk**，`--windowed` 只给验收/截图用
- 真机尺寸（实测，别再猜）：`xrandr` 把它旋成 `DSI-1 connected 1280x800+0+0 left`
  → X 桌面与 kiosk 窗口都是 **1280×800**；`/sys/class/graphics/fb0/virtual_size` 报的
  `800,1280` 是**面板模式**，不是给应用用的逻辑尺寸（`docs/gui.md` §2）

出处：`docs/architecture.md`（GUI 那一节："GUI **不是** PC 上的 Python 程序……**在板端本机
编译**（板端装了 Qt 5.12 aarch64；PC 上没有 Qt）"）、`docs/gui.md` §1/§2。

## 理由

1. **屏和触摸在板子上**：交互对象就是那块 1280×800 的 DSI 屏；把界面放到 PC 上，
   等于给"看这块屏"再加一条网络链路。
2. **板子本来就装了 Qt 5.12 aarch64，PC 上没有 Qt**（`docs/architecture.md`）：
   在 PC 上做界面要新引一套 GUI 技术栈（历史上有过 PySide6 那一版，现在是
   `tests/test_docs.py` 过时声明黑名单里的一条："GUI 是板端的 Qt5 C++ 程序"）。
3. **同机 IPC 的确定性**：GUI 与 Agent 在同一台机器上，掉网也能连（板子与 PC 的
   网络断了只是没有串流，界面与助手照旧可用）。
4. **常驻与 kiosk**：设备形态是"开机就是这个界面"，全屏 kiosk + 触摸比"PC 上开个窗口"更贴。

## 替代方案与代价

| 方案 | 为什么没选 | 代价 |
| --- | --- | --- |
| PC 上跑 GUI（PySide6 那版） | 屏在板子上；PC 还得装 GUI 栈；PC 关机界面就没了 | 已删（过时声明黑名单里有 `PySide6`） |
| 浏览器前端（板端跑 kiosk 浏览器） | 板端要额外跑一个浏览器进程，资源更贵；触摸手势与全屏行为都要另调 | 现在没有这条路 |
| 手机 App / 远程控制 | 需要暴露网络面、要配对与鉴权；与"设备自己能用"的目标不符 | 现在没有远程控制 |

**真实的代价（这条最该记住）**：跨语言让"同一份语义"有了**两份实现**。最典型的是
**日程**：GUI 的日程区要自己展开 `recurring` / `oneoff`（`gui/src/core/schedule_model.cpp`
镜像了 Python 的 `parse_clock` / `_weekday_index` / `date.fromisoformat` / `occurs_on`）。
镜像会漂，所以有两条**互为表里**的守卫：

- `tests/test_schedule_parity.py`（Python 侧）：Python 语义变了 → 它按真的 `Scheduler`
  重新生成期望，与入库夹具 `tests/data/schedule_parity/*.expect.json` 对不上就红
- `gui/tests/test_schedule_model.cpp::parityWithPythonFixtures`（C++ 侧）：C++ 镜像变了 →
  读同一份夹具 + 同一份期望，行级比对不一致就红

**保证边界**（写清，别吹）：只承诺"夹具覆盖到的写法两边等价"；夹具之外（锚点、显式
`!tag`、复杂 flow、不同 Python 版本对日期宽容度不同的写法如 `20260920`）**不承诺**。
见 `docs/config-sources.md` §5。

## 后果与边界

- **GUI 的所有构建/测试都在板端**：`scripts/sync-gui.ps1 -Test` 会重编并跑 ctest
  （板端 ctest 是目前 GUI 唯一的自动化门禁；PC 上没有 Qt，编不了）。
- 新增"跨语言语义"时必须同时改 Python、C++ 镜像、文档，并重新生成夹具
  （`python3 tests/test_schedule_parity.py --write`，**在板端生成**，用板端的 python3.8）。
- 界面参数**没有**自己的配置文件：统一住在 `config/config.yaml` 的 `gui:` 段
  （归一化 D 系列），见 `docs/config-sources.md`。
- **板端资源是要算的**：GUI 常驻占内存，而 SigLIP 常驻就 923 MB（ADR 0003/T13 的实测）。
  加界面功能时别把"常驻的东西"当免费的（T14-11 的同跑验收就是量这个）。

## 参考

- `docs/architecture.md` §4.3（GUI 目录树 + 板端编译 + 配置那一段）
- `docs/gui.md` §1（怎么跑、依赖、验收参数）、§2（真机尺寸与布局）
- `docs/gui-agent-integration.md`（哪个控件发什么命令、收 topic 后界面怎么变）
- `docs/config-sources.md` §5（日程语义的两条守卫与保证边界）
- 过时声明守卫：`tests/test_docs.py` 的 `STALE_CLAIMS`（`PySide6`、`gui.yaml`）
