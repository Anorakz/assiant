# ADR 0001 — Agent ⇄ GUI 用 Unix domain socket，不用 ZMQ / TCP

- **状态**：已生效（Phase 4 起；本文 T14-1 追记）
- **影响面**：`agent/ipc/`、`gui/src/services/local_client.*`、`docs/ipc-protocol.md`、`config.example.yaml` 的 `ipc:` 段

> ⚠ 本文按**现有代码与文档**追记（T14-1），不是当时的会议记录。能指出出处的都标了
> 路径；指不出出处的写成"当时的判断"。追记的意义是：**下次想换回来时，先读这里**。

## 背景

Agent（Python）与 GUI（Qt5 C++）**都跑在 RK3568 板端这一台机器上**（ADR 0002）。
两者要交换的东西有两类：

- Agent → GUI 的**推送**：模式、日程、音乐、B 站封面、对话气泡……（topic 形式）
- GUI → Agent 的**命令**：切模式、发消息、上一首/下一集……

历史：早期（Phase 2 之前）用的是 **ZMQ**。迁移痕迹还在 —— 板端旧 live 配置里当时
还留着 zmq 的 `pub_bind`，`git show d858fbe` 的提交说明就记着这件事（"只要那份配置旧
一点（比如还留着 zmq 的 pub_bind）就红"）。现状类文档里再出现 `ZMQ` 会被
`tests/test_docs.py` 的过时声明黑名单直接抓红。

## 决定

用 **Unix domain socket（`AF_UNIX` / `SOCK_STREAM`）+ NDJSON**：

- 路径 `/tmp/agent.sock`（配置键 `ipc.socket_path`，可用 `--socket` 覆盖）
- Agent **监听**，GUI 作为**客户端**连接
- 每条消息恰好一行（LF 分隔）、UTF-8、单行上限 1 MiB
- 线上格式的**唯一实现**是 `agent/ipc/protocol.py`；C++ 侧照 `docs/ipc-protocol.md` §8
  的字面值实现，不另立一份（见该文 §0 的"真源边界"表）

出处：`docs/ipc-protocol.md` §1 的传输层表与"为什么用换行分隔（NDJSON）"。

## 理由

1. **同机通信不需要网络栈**：两个进程都在板端，走 `AF_UNIX` 就没有端口、没有网卡、
   没有"局域网里谁能连上来"这个面。TCP 要占端口（旧方案的 5555/5556 就是 ZMQ 时代
   的端口，现在是过时声明黑名单里的一条）。
2. **少一个本地库 + 一个 Python 包**：板端装 Python 包要绕 PEP 668（`scripts/test-python.sh`
   头部注释就记着这件事 —— 系统 Python 拒装、得 `pip3 install --target /tmp/...`）。
   而 `AF_UNIX` 两边都自带：Python 侧 `asyncio.start_unix_server`，Qt 侧 `QLocalSocket`。
   当时的判断：为一个"两个本机进程"的场景引入 libzmq + pyzmq 不划算。
3. **线格式简单到能写进文档**：NDJSON 一行一条，JSON 编码器会把字符串里的换行转义成
   `\n`（两个字符），所以"裸换行"不可能出现 —— 这条让"按行读"成为**可证明**的分帧方式，
   而不是靠约定（`docs/ipc-protocol.md` §1）。跨语言只有这一份格式要对着写，还能用
   夹具测试钉住（`tests/test_ipc_protocol.py`、`tests/data/schedule_parity/` 是同一套路）。
4. **能明确表达"没连上"**：GUI 侧连接失败就是失败，命令会被**丢弃并打日志**
   （`gui/src/services/local_client.cpp` 的 `sendCommand()`：连上之前一律丢弃、不排队不补发）。
   当时的判断：ZMQ 的 PUB/SUB 默认"发出去就不管"，会把"GUI 根本没连上"和
   "Agent 没处理"混成同一件事，排查时更贵。

## 替代方案与代价

| 方案 | 为什么没选 | 代价 |
| --- | --- | --- |
| ZMQ（PUB/SUB + REQ/REP） | 多一个本地库 + Python 包；语义远超需求（我们要的是"一个服务端 + 一个客户端"） | 已删；板端旧配置里的 `pub_bind` 是历史残留 |
| TCP + `127.0.0.1` | 占端口、多一层网络栈、多个可连面 | 端口冲突要处理（本机 HTTP 缓冲那条路已经占了一个端口） |
| 共享内存 / 落盘文件 | 得自己造同步与边界；**帧**那条路已有专用环形缓冲（ADR 0003） | 两个方向都要重新设计 |
| HTTP / WebSocket | 为两个本机进程引入 Web 栈与更多依赖 | 板端资源与依赖面都更大 |

## 后果与边界

- **只能本机**：这是刻意的（GUI 与 Agent 同机）。要远程控制必须另开一条路（现在没有）。
- 换路径要**两边同时改**：`ipc.socket_path`（配置）+ `--socket`（GUI 命令行）。
- 命令是**单向**的：`{action, payload}` 发出去没有回执；现有"回应"靠 Agent 主动 push
  另一个 topic（例如 `query_schedule` → 推 schedule 快照）。**要"写入并拿到结果"
  必须新加回执机制** —— 这就是 T14-2 要做的 `config_result`。
- 单行 1 MiB 上限（`MAX_LINE_BYTES`）：超了按协议断连，不要在一条消息里塞大对象。
- 断线不补发、不排队（GUI 侧）：屏幕上的"未连接"就是真相，别指望它自己追上。

## 参考

- `docs/ipc-protocol.md` §0（真源边界）、§1（传输层与 NDJSON 理由）、§8（常量表）
- `agent/ipc/protocol.py`、`agent/ipc/local_server.py`、`gui/src/services/local_client.cpp`
- `tests/test_ipc_protocol.py`、`tests/test_ipc_local_server.py`、`tests/test_ipc.py`（pytest）
- 迁移痕迹：`git show d858fbe`（板端旧配置里的 `pub_bind`）
- 过时声明守卫：`tests/test_docs.py` 的 `STALE_CLAIMS`（`ZeroMQ` / `\bzmq\b` / 5555-5556 端口）
