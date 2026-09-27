# ADR 0004 — 跨语言调用要放 GIL（pybind11 `gil_scoped_release`）

- **状态**：已生效（Phase 2 起；本文 T14-1 追记）
- **影响面**：`native/binding.cpp`、`native/binding_utils.h`、`native/input_sender.h`、
  `agent/io/`、`agent/io/_native.py`

> ⚠ 本文按**现有代码与文档**追记（T14-1），不是当时的会议记录。能指出出处的都标了
> 路径；指不出出处的写成"当时的判断"。

## 背景

Python 侧是**单进程 asyncio**：状态机、IPC server、日程、串流心跳、LLM 调用都在同一个
事件循环里。native 扩展（`agent_native`）要替这个循环干几件**会阻塞**的事：

- `moonlight.start_with_session()` —— `LiStartConnection` 是阻塞调用
- `moonlight.stop()`
- `send_key` / `send_mouse` / `send_hotkey` —— 网络写，会阻塞
- `image_rb.read_latest()` / `read_by_timestamp()` —— 每次 **196 KB** 的帧拷贝（ADR 0003）

CPython 的规矩是：**进入 C 扩展默认持有 GIL**。持着 GIL 阻塞，等于把整个事件循环按住。

## 决定

**所有会阻塞的 native 调用都释放 GIL**，并按"哪一半需要 GIL"把调用切开：

| 调用 | 做法 | 出处 |
| --- | --- | --- |
| `start_with_session` | 手写 `py::gil_scoped_release` 包住 `LiStartConnection`（注释："LiStartConnection 是阻塞的，必须放开 GIL"） | `native/binding.cpp:129`（函数绑定在 `:122`） |
| `stop` | 同上 | `native/binding.cpp:166` |
| `send_key` / `send_mouse` / `send_hotkey` | `py::call_guard<py::gil_scoped_release>()` | `native/binding.cpp:236/258/271`；`native/input_sender.h:14` 也写了同一约定 |
| `image_rb.read_latest` / `read_by_timestamp` | **先放 GIL 把帧读进本地 `Frame`，再持 GIL 建 numpy** | `native/binding_utils.h:116-141` |

最后一条是这条 ADR 里最容易被写错的地方：`py::array_t` 的构造**会分配 Python 对象**，
必须在持有 GIL 时做（`frame_to_numpy` 的注释："必须持有 GIL"）；而 196 KB 的
`read_latest` + `memcpy` 又没必要占着 GIL。所以顺序是
**"放 GIL 读 → 收 GIL 建数组"**，不能颠倒。

## 理由

1. **不释放 = 整个助手停摆**：一次 `LiStartConnection` 或一次帧读如果压住事件循环，
   状态机、IPC 推送、日程、监督循环全部停在同一时刻 —— 而 GUI 那边只会看到"界面没反应"，
   很难定位到"某个 C 调用没放 GIL"。当时的判断：这是最容易踩、也最难查的一类问题，
   所以把"放 GIL"当成**默认动作**，而不是优化。
2. **196 KB 的拷贝占了实测延迟里不可忽略的一块**：一帧 256×256×3（`native/image_rb.h`），
   读一次就是一次 `memcpy`；把它放到 GIL 之外，Python 侧其他任务才有机会插进来。
3. **与 ADR 0003 配套**：不返回零拷贝视图（槽位可能被生产者覆盖），所以每次都是真拷贝 ——
   既然非拷不可，就别让这次拷贝独占解释器。
4. **消费侧也照着这条纪律走**：`agent/io/image_reader.py` 把三次 native 调用放到
   `image_rb` 的**专属单线程执行器**（`agent/io/_native.py::run_native`）——
   放 GIL + 专属线程 = 既不阻塞事件循环，也不违反 SPSC。

## 替代方案与代价

| 方案 | 为什么没选 | 代价 |
| --- | --- | --- |
| 不放 GIL（用默认行为） | 一次阻塞调用压住整个 asyncio：状态机/IPC/日程全停，且症状与原因隔得远 | 现在每个绑定都显式写了释放 |
| 放 GIL 但返回零拷贝视图 | 需要"Python 持有期间冻结槽位"，等于给环形缓冲加锁/引用计数 | 与 ADR 0003 的取舍冲突，固定一次 memcpy 更简单 |
| 把 native 调用搬到独立进程 | 多一层 IPC + 每帧序列化 196 KB | 现在没有这条路（本机 IPC 只给 GUI 用） |
| 把 numpy 分配也放到 GIL 之外 | `py::array_t` 构造 Python 对象，**做不到** | 顺序必须"先读后建" |

## 后果与边界

- **持 GIL 期间不许做任何可能阻塞的事**：新加绑定默认要写 `call_guard<gil_scoped_release>`；
  需要碰 Python 对象的那一段（建 numpy、拼 dict、抛异常）再收回 GIL。
- **放 GIL 的那一段里绝对不能碰 Python 对象**（引用计数、异常、`py::object`）——
  那是未定义行为。这条要在 code review 里盯着。
- **不要"半开半闭"地放**：`read_latest_numpy` 那种"读的时候放、建数组时收"的写法要照抄；
  如果有人把整段（含建数组）都放进 `gil_scoped_release`，就是崩溃而不是变慢。
- **板端是 4 核 A55 + 3.9 GB**：放 GIL 不等于变快，它的目的是**不让一个调用独占解释器**；
  真正的算力账在 `docs/bench.md`（T14-6 会给出基线）。

## 参考

- `native/binding.cpp` 头部（§GIL 那一段）与 `start_with_session` / `stop` / `send_*` 的写法
- `native/binding_utils.h`（`frame_to_numpy` / `read_latest_numpy` / `read_by_timestamp_numpy`）
- `native/input_sender.h:14`
- `agent/io/image_reader.py`（专属线程的由来）、`agent/io/_native.py`
- ADR 0003（RingBuffer 只读 + 为什么要拷贝）
