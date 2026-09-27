# ADR 0003 — RingBuffer 的写入侧只在 native，Python 只读

- **状态**：已生效（Phase 1 起；本文 T14-1 追记）
- **影响面**：`native/ring_buffer.h`、`native/image_rb.*`、`native/binding.cpp`、
  `agent/io/image_reader.py`、`tests/board/`、根 `CMakeLists.txt`

> ⚠ 本文按**现有代码与文档**追记（T14-1），不是当时的会议记录。能指出出处的都标了
> 路径；指不出出处的写成"当时的判断"。

## 背景

视频链路要在**解码线程**与**Python（asyncio 事件循环）**之间传帧：

```
Sunshine 流 → moonlight-common-c → 解码 → ROI 裁剪 → 256×256 RGB888 → ImageRingBuffer
                                                                      → pybind11 read_latest() → Python
```

一帧 256×256×3 = **196 608 字节**；容量 **300 帧 ≈ 56 MB**（定长，堆分配——放成员里会把
栈撑爆，也会让缓冲不可移动）。生产速率是 30–60 fps，消费速率是"想起来了看一眼"。

## 决定

1. RingBuffer 是**严格 SPSC 无锁**结构：`push()` 只能由**一个**线程调用（生产者），
   `pop()` / `read_latest()` / `peek()` 等只能由**另一个**线程调用（消费者）；
   两个消费者线程并发读是**未定义行为**（`native/ring_buffer.h` 的"并发约定"）。
2. **覆盖式（overwrite）**：写满后新数据直接盖掉最老的未读数据，`push()` 永不阻塞、
   永不失败（`PushResult.overwritten_unread` 报告这次盖掉了多少）。
3. **写入侧不暴露给 Python**：`agent_native` 只绑定 `read_latest` / `read_by_timestamp` /
   `size`；**没有** `push`。生产者唯一 —— moonlight 的视频接收线程
   （`native/binding.cpp` 头："生产者是 moonlight 内部线程；消费者**必须是同一个 Python
   线程**"）。
4. Python 侧的读法也固定线程：三次 native 调用全部走 `image_rb` 的**专属单线程执行器**
   （`agent/io/image_reader.py` 头 + `agent/io/_native.py::run_native`），不用 asyncio 的
   共享线程池。
5. 板端要"注帧"时**另建一个测试扩展**：`agent_native_test`（只在交叉编译时构建，
   `tests/board/CMakeLists.txt`，通过 capsule 拿到**同一个** image_rb），进不了 ctest、
   也不随 `deploy.ps1` 部署。根 `CMakeLists.txt` 里那段注释就是它的存在理由
   （"正式模块刻意不暴露'写帧'入口"）。

## 理由

1. **视频流的正确取舍是丢帧，不是排队**：消费者慢时，让解码线程被背压拖死比丢旧帧
   糟得多（画面直接卡住）。覆盖式让 `push()` 恒定 O(1) 且无阻塞。
2. **无锁的代价是"纪律"，而纪律要靠接口形状来保证**：SPSC 只有在"写入口只有一处"
   时才成立。把 `push()` 暴露给 Python，就等于让任何一段脚本都能从"消费者线程"写帧 ——
   那不是把结构写坏了，而是让**前提**不再可查。
3. **测试不该改变生产契约**：板端要验 numpy 数据通路（`tests/board/test_image_rb_numpy.py`），
   于是用**单独的扩展模块**注帧，而不是给正式模块开一个后门。
   （顺带：numpy 支持不需要 numpy 的 C 头 —— `pybind11/numpy.h` 自带 API 声明，
   交叉编译期的 sysroot 不必有 numpy 头，板端有 numpy 即可。）
4. **正确性有机械证据**：槽位序号 + acquire/release 的自定义同步协议（生产者写数据前后
   各发布一次 `slot.seq`，消费者读后**复查** seq 防撕裂），`tests/test_ring_buffer.cpp` /
   `tests/test_image_rb.cpp` 覆盖；TSan 的抑制规则写在 `tests/tsan.supp`（**只抑制载荷，
   控制面不抑制** —— 抑制本身也要有边界）。

## 替代方案与代价

| 方案 | 为什么没选 | 代价 |
| --- | --- | --- |
| 给 Python 暴露 `push()` | 破坏"生产者唯一"这条前提；真机上会造出假帧（标定/误判风险） | 现在用 `agent_native_test` 代替 |
| 每帧加锁 / 用 `asyncio.Queue` 传帧 | 196 KB × 30–60 fps 的锁与拷贝开销；跨语言还要序列化 | 现在这条路上没有任何锁 |
| 返回**零拷贝**的 numpy 视图 | 槽位随时会被生产者覆盖，不能让 Python 长期持有裸指针 | 每次读固定一次 196 KB memcpy（`native/binding_utils.h::frame_to_numpy` 的注释写了这条） |
| 传统"牺牲一个槽位"判满/空 | 序号机制天然区分空/满/正在写/已覆盖，不必牺牲槽位 | — |

## 后果与边界

- **消费者必须固定线程**：从多个 Python 线程并发 `read_*` 是 UB（docstring 里已写明）。
  新加"读帧"的代码要接到 `run_native` 那条专属执行器上，别就地起线程。
- **两种读语义不要混用**：`read_latest()` 是流式（只前进、拿到就不重复返回）、
  `read_by_timestamp()` 是查询式（非破坏性，同一 ts 重复调用结果一致）；`overruns()`
  的统计口径也受"混用"影响（`native/ring_buffer.h` 里 `consumed()` 那段注释）。
- **TSan 会报载荷误报**：这是锁自由结构的经典误报，抑制规则在 `tests/tsan.supp`；
  但**控制面**（序号本身）不许抑制 —— 抑制范围变大就等于把守卫关了。
- PC 上编不了 MPP/moonlight：host 单测（`scripts/test-host.ps1`）测的是**同一份模板代码**
  + 自己造的假生产者，**真解出帧**那条只能在板端跑（`tests/board/mpp_decode_smoke`）。

## 参考

- `native/ring_buffer.h`（并发约定 / 槽位序号机制 / 内存模型 / TSan 说明）
- `native/image_rb.h`（容量 300、覆盖式、两条读语义、`static_assert` 钉住时间戳字段位置）
- `native/binding.cpp`（头部的 SPSC 与"单一适配器实例"说明）、`native/binding_utils.h`
- `agent/io/image_reader.py`（专属线程）、`agent/io/_native.py`
- 根 `CMakeLists.txt`（`agent_native_test` 的存在理由）、`tests/board/CMakeLists.txt`
- `tests/test_ring_buffer.cpp`、`tests/test_image_rb.cpp`、`tests/tsan.supp`
