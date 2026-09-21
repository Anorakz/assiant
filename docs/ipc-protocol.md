# Agent ⇄ GUI IPC 协议

Agent（Python）与 GUI（C++ / Qt5）都跑在 RK3568 板端，通过 Unix domain socket 通信。
本文是该协议的**唯一事实来源**；常量与编解码实现在 `agent/ipc/protocol.py`。

---

## 1. 传输层

| 项 | 值 |
| --- | --- |
| socket 类型 | Unix domain socket（`AF_UNIX` / `SOCK_STREAM`） |
| 路径 | `/tmp/agent.sock` |
| 消息分隔 | 换行 `\n`（LF，0x0A）；每条消息恰好一行 |
| 编码 | UTF-8（无 BOM） |
| 单行上限 | 1 MiB（`MAX_LINE_BYTES`） |
| 谁监听 | Agent 监听并 `accept`；GUI 作为客户端连接 |

### 为什么用换行分隔（NDJSON）

**消息里不可能出现裸换行**：JSON 编码器会把字符串中的换行转义成 `\n`（两个字符
`\` `n`）。Python 的 `json.dumps` 与 Qt 的 `QJsonDocument::toJson` 都如此，所以：

- 按 `\n` 切分永远是安全的，不需要长度前缀，也不需要额外转义层；
- 一条消息可以边读边解析，接收方不必先知道总长度。

接收方按行读即可，**不要**按"读到多少字节"切分。结尾的 `\n` 是分隔符、不属于消息内容。
实现上容忍行尾出现 `\r\n`（`decode()` 会剥掉），但**发送方必须发 `\n`**。

### 连接生命周期

- GUI 断线重连即可，Agent 不做会话保持：**连接本身不携带状态**，
  状态全在 `status` topic 里。
- 一条坏消息只影响它自己（见 §6），不断开连接。
- Agent 退出前会关闭监听 socket 并删除 `/tmp/agent.sock`；GUI 收到 EOF 后重试连接。

---

## 2. 消息信封

**两个方向的信封不一样** —— 这不是笔误，是照 GUI 的实际实现定的
（Phase 6 决策 1：命令格式以 GUI 为准）。同一层里两种信封共存，混用会被丢弃（见 §6）。

### Agent → GUI（推送）

```json
{"topic": "status", "data": {"mode": "STUDY", "connected": true}, "timestamp": 1234567890.123}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `topic` | string | ✅ | 消息名。取值见 §3 |
| `data` | **object** | ✅ | 负载。**必须是 JSON object**，不能是 array / string / number；没有参数时给 `{}` |
| `timestamp` | number | ✅ | Unix epoch **秒**（浮点），例如 `1234567890.123` |

### GUI → Agent（命令）

```json
{"action": "chat_input", "payload": {"text": "现在几点了"}}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `action` | string | ✅ | 命令名。取值见 §4 |
| `payload` | **object** | ✅ | 参数。**必须是 JSON object**；没有参数时给 `{}` |

**命令方向没有 `timestamp`**：GUI 不发，Agent 也不要求（`decode_command()` 不检查它）
—— 少一个字段就少一处两边可能不一致的地方。字段顺序是 `action` 在前（Qt 按插入顺序
序列化，GUI 先插 `action`），但两侧都应**按 key 取值**，不要依赖顺序。

> ⚠ `topic`/`data` 与 `action`/`payload` **不能混用**：推送用前者，命令用后者。
> Agent 侧的对应关系是 `encode/decode/decode_full`（推送）与
> `encode_command/decode_command`（命令）。收错形态的行会被丢弃并记 warning ——
> **不**做"两种都认"的兼容（那等于"文档说 A、代码也收 B"）。

规则：

1. **字段顺序无关**。JSON object 无序，两侧都必须按 key 取值。
2. **未知字段必须忽略**，不要报错。这是本协议没有版本号时唯一的向前兼容手段
   —— 将来加字段时，老实现忽略它即可继续工作。
3. **`data` / `payload` 必须是 object**。这条是硬约束：放行标量会让两侧的取值代码
   到处写类型分支，而 array 与 object 的边界最容易两边理解不一致。
4. **没有版本号字段**（v1 不需要）。需要演进时靠"加字段 + 忽略未知字段"，
   而不是靠版本协商。

### 时间戳的精度（⚠ 容易踩）

`timestamp` 是 **Unix epoch 秒的浮点数**。C++ 侧**必须用 `double`**：

| 类型 | 有效位 | epoch 尺度（~1.8e9）下的分辨率 |
| --- | --- | --- |
| `float`（32 位） | ~7 位十进制 | **约 128 秒** ← 完全不可用 |
| `double`（64 位） | ~15–16 位 | 约 0.2 微秒 |

Qt 里 `QJsonValue::toDouble()` 返回 `double`，正常使用即可；但**不要**把它存进
`float` 变量或 `qreal`（在部分平台上 `qreal` 是 `float`）。

Python 侧 `time.time()` 本来就是 `double`，无此问题。

数值精度：JSON number 在两侧都按 IEEE-754 double 处理，因此**整数超过 2^53
会丢精度**。协议里的 `index` 这类小整数没问题；将来若有长 ID，用字符串传。

---

## 3. Topic 表（Agent → GUI）

GUI 收到后按 `topic` 分发。**不认识的 topic 忽略**。

| topic | data 字段 | 类型 | 说明 |
| --- | --- | --- | --- |
| `status` | `mode` | string | 当前状态，取值见下：`SLEEP` / `IDLE` / `STUDY` / `GAME` |
| | `connected` | bool | 是否已连上串流主机（moonlight 已连接） |
| `llm` | `text` | string | Agent 给用户看的一段回复文本 |
| `wallpaper` | `path` | string | 壁纸文件绝对路径 |
| | `index` | number | 该壁纸在列表里的序号（从 0 开始） |
| `music` | `title` | string | 当前曲目标题 |
| | `playing` | bool | 是否正在播放 |

### `status.mode` 的取值

**全大写**：`SLEEP`、`IDLE`、`STUDY`、`GAME`（常量 `MODE_*`，全集 `MODES`）。

> ⚠ 与内部状态机的大小写不同：`agent/core/state_machine.py` 的 `State` 值是**小写**
> （`"sleep"`/`"idle"`/`"study"`/`"game"`），配置里也写小写。**IPC 上用大写是协议
> 约定**（GUI 直接拿去比较/显示）。转换只在 ipc server 那一层做，不要把两套大小写
> 混着传。

**示例**

```
{"topic":"status","data":{"mode":"STUDY","connected":true},"timestamp":1234567890.123}
{"topic":"llm","data":{"text":"已经切换到学习模式。"},"timestamp":1234567890.456}
{"topic":"wallpaper","data":{"path":"/home/kickpi/wallpapers/04.jpg","index":3},"timestamp":1234567891.0}
{"topic":"music","data":{"title":"夜曲","playing":true},"timestamp":1234567891.5}
```

---

## 4. Command 表（GUI → Agent）

命令用 §2 的**命令信封**：`{"action": ..., "payload": {...}}`，**没有 `timestamp`**。
Agent 收到后按 `action` 分发。**不认识的 action 忽略**（记 warning，不是错误 —— 两侧
版本可能不一致）。

| action | payload 字段 | 类型 | 说明 |
| --- | --- | --- | --- |
| `switch_mode` | `value` | string | 目标状态，取值同 `MODES`（`SLEEP`/`IDLE`/`STUDY`/`GAME`）。⚠ **键是 `value`，不是 `mode`**：`mode` 是 §3 里 `status` **推送**的字段，方向不同，别混 |
| `next_wallpaper` | — | — | 切下一张壁纸；`payload` 必须是 `{}` |
| `chat_input` | `text` | string | 用户在 GUI 里敲的一行输入，等价于终端输入 |
| `next_bilibili` | — | — | 播放下一集 B 站视频；`payload` 必须是 `{}` |

注意：

- **没有参数的 command 也必须带 `payload`**，写成 `{}`。缺 `payload` 字段会被判为非法消息。
- 少了 `payload` 里该有的键（例如 `switch_mode` 缺 `value`）**不会**被当成"用默认值"：
  该条命令被丢弃，并记一条**点名字段**的 warning。
- **`next_wallpaper` / `next_bilibili` 目前还没接下游**（属 Phase 7）：Agent 收到后会回推一条
  `llm{"text": "…还没接入（Phase 7）…"}` —— 让"点了"有反馈，而不是毫无动静
  （文案见 `agent/ipc/__init__.py` 的 `UNWIRED_COMMAND_NOTES`）。
- `switch_mode` 的合法性由 Agent 侧状态机判定：非法转换（例如 `STUDY → GAME`）
  **不会**报协议错，而是被拒绝并回一条 `status` 说明当前真实状态。
  GUI 应当以随后收到的 `status` 为准，不要乐观地自行切换显示。

**示例**

```
{"action":"switch_mode","payload":{"value":"GAME"}}
{"action":"next_wallpaper","payload":{}}
{"action":"chat_input","payload":{"text":"帮我看看现在几点了"}}
{"action":"next_bilibili","payload":{}}
```

---

## 5. 完整消息示例（字节级）

`status` 一条，UTF-8 编码后写进 socket 的字节：

```
7b 22 74 6f 70 69 63 22 3a 22 73 74 61 74 75 73   {"topic":"status
22 2c 22 64 61 74 61 22 3a 7b 22 6d 6f 64 65 22   ","data":{"mode"
3a 22 53 54 55 44 59 22 2c 22 63 6f 6e 6e 65 63   :"STUDY","connec
74 65 64 22 3a 74 72 75 65 7d 2c 22 74 69 6d 65   ted":true},"time
73 74 61 6d 70 22 3a 31 32 33 34 35 36 37 38 39   stamp":123456789
30 2e 31 32 33 7d 0a                              0.123}.
```

文本形式（`\n` 表示 0x0A）：

```json
{"topic":"status","data":{"mode":"STUDY","connected":true},"timestamp":1234567890.123}
```

带中文的 `llm`（`ensure_ascii=False`，中文以 UTF-8 原样出现）：

```json
{"topic":"llm","data":{"text":"已经切换到学习模式。"},"timestamp":1234567890.456}
```

> 两种写法都能解析：中文既可以直接是 UTF-8 字节，也可以是 `\uXXXX` 转义
> （`"\u5df2\u7ecf..."`）。**两侧都必须接受这两种形式** —— JSON 解析器本来就都支持。
> Python 侧 `encode()` 用原样 UTF-8（便于抓包/看日志），Qt 侧 `toJson()` 也只输出
> 原样 UTF-8。

命令方向（`encode_command()`，注意**没有 `timestamp`**，长度 52 字节）：

```
7b 22 61 63 74 69 6f 6e 22 3a 22 63 68 61 74 5f   {"action":"chat_
69 6e 70 75 74 22 2c 22 70 61 79 6c 6f 61 64 22   input","payload"
3a 7b 22 74 65 78 74 22 3a 22 e4 bd a0 e5 a5 bd   :{"text":"你好
22 7d 7d 0a                                       "}}.
```

文本形式：

```json
{"action":"chat_input","payload":{"text":"你好"}}
```

---

## 6. 错误处理约定

**核心原则：一条坏消息只影响它自己。丢弃 + 记日志，不断开连接。**

| 情况 | 处理 |
| --- | --- |
| 不是合法 JSON | 丢弃该行，记 warning，不断开 |
| 不是合法 UTF-8 | 丢弃该行，记 warning，不断开 |
| 是 JSON 但不是 object（数组/标量） | 丢弃，记 warning |
| **推送**方向：缺 `topic` / 不是非空字符串 | 丢弃，记 warning |
| **推送**方向：缺 `data` / `data` 不是 object | 丢弃，记 warning |
| **推送**方向：缺 `timestamp` / 不是数字 | 丢弃，记 warning |
| **命令**方向：缺 `action` / 不是非空字符串 | 丢弃，记 warning |
| **命令**方向：缺 `payload` / `payload` 不是 object | 丢弃，记 warning |
| **信封发错方向**（命令位给了 `{topic,data,…}`，或推送位给了 `{action,payload}`） | 丢弃，记 warning —— 报的正是"缺本方向那个必填字段"（如缺 `action`）。**不**做双信封兼容 |
| 超过 1 MiB（一直不发换行） | 丢弃该行，记 warning，不断开 |
| `topic` / `action` 不认识 | **忽略**（不是错误）：可能对端版本更新 |
| 已知命令，但 `payload` 里缺该有的键（如 `switch_mode` 缺 `value`） | 丢弃并记**点名字段**的 warning；**不影响其它消息** |
| 对端关闭连接（EOF） | 关闭该连接，继续 `accept` 新连接 |

约定细节：

- 丢弃时**必须记日志**（包含原始行内容的截断形式，便于定位），否则坏消息会变成
  静默丢数据，非常难查。
- **不做**任何"猜测性修复"：例如缺 `data` / `payload` 就去猜它想干什么，或者
  `switch_mode` 少了 `value` 就当成默认模式。宁可丢一条。
- 解析失败的**计数器**建议暴露出来（日志或诊断接口），方便判断是协议不兼容还是
  偶发抖动。
- 单条消息处理失败（例如 `switch_mode` 触发非法状态转换）**不算协议错误**，
  按业务逻辑处理并回 `status`。

---

## 7. 两侧实现要点

### Python（Agent）

```python
from agent.ipc import (encode, decode_full, encode_command, decode_command,
                       TOPIC_STATUS, COMMAND_CHAT_INPUT, MODE_STUDY)

# 发推送 (Agent -> GUI): topic 信封
sock.sendall(encode(TOPIC_STATUS, {"mode": MODE_STUDY, "connected": True}))

# 发命令 (GUI -> Agent): action 信封, 没有 timestamp
sock.sendall(encode_command(COMMAND_CHAT_INPUT, {"text": "现在几点了"}))

# 按行读 (这里以 Agent 为例: 入方向是命令):
buf = b""
buf += sock.recv(4096)
while b"\n" in buf:
    line, buf = buf.split(b"\n", 1)
    try:
        action, payload = decode_command(line)   # 命令信封
    except IpcProtocolError as exc:              # 丢弃 + 记日志, 连接继续
        log.warning("丢弃非法 IPC 命令: %r (%r)", line[:200], exc)
        continue
    handle(action, payload)
```

> **两端各自只收一个方向**：Agent 的读循环只解命令（`decode_command`），只发推送
> （`encode`）；GUI 反过来 —— 只发命令（`encode_command`），只解推送（`decode_full`）。
> 所以"收错形态"永远是**对端发错了方向**或版本不一致，丢掉并记 warning 是对的。

要点：

- `encode()` / `encode_command()` 都返回**含结尾 `\n` 的完整字节**，直接 `sendall` 即可，
  不要重复加换行。
- `decode()` / `decode_full()` / `decode_command()` 结尾有无 `\n` 都能吃，但只剥**结尾**：
  行首空白会让 JSON 解析失败，那是应该报错的。
- 推送要时间戳用 `decode_full()`（`decode()` 按签名只返回 `(topic, data)`）；
  命令没有时间戳，用 `decode_command() -> (action, payload)`。
- 三个 encode 的负载传错类型（list/str/…）都会抛 `InvalidMessageError`，不要吞。

### C++ / Qt5（GUI）

- 读：`QTcpSocket` 不支持 `AF_UNIX`，用 `QLocalSocket`（Windows 上是命名管道，
  在 Linux 上就是 Unix domain socket）。用 `readyRead` → 追加到缓冲 →
  `while` 找 `\n` 切分。**不要**假设一次 `readyRead` 正好是一条消息。
- 解析：`QJsonDocument::fromJson(line, &err)`；`err.error != QJsonParseError::NoError`
  → 丢弃 + `qWarning`。
- 取值（**收**推送）：`doc.object().value("data").toObject()`；**校验 `isObject()`**，
  别直接 `toObject()`（标量会被静默变成空对象，把协议错误吞掉）。
- 时间戳：`double ts = obj.value("timestamp").toDouble();`（见 §2 的精度警告）。
- 写（**发**命令）：信封是 `{"action","payload"}`（**没有 timestamp**）——
  照 `gui/src/services/local_client.cpp::sendCommand()` 那样按插入顺序塞两个字段，
  再 `QJsonDocument(obj).toJson(QJsonDocument::Compact) + "\n"` 然后 `flush()`。
  参考实现见 `gui/src/services/local_client.cpp`。
- 发送前校验 `payload` 是 object；`next_wallpaper` / `next_bilibili` 发 `{}`。
- 断线重连：`disconnected` → 定时重连 `/tmp/agent.sock`。
  重复连接前先删掉自己创建的 socket 文件（Agent 侧负责 `unlink`）。

---

## 8. 常量对照

`agent/ipc/protocol.py` 里的名字（C++ 侧请照抄字面值）：

| Python 常量 | 值 |
| --- | --- |
| `SOCKET_PATH` | `"/tmp/agent.sock"` |
| `MESSAGE_SEPARATOR` | `b"\n"` |
| `ENCODING` | `"utf-8"` |
| `MAX_LINE_BYTES` | `1048576` |
| `TOPIC_STATUS` | `"status"` |
| `TOPIC_LLM` | `"llm"` |
| `TOPIC_WALLPAPER` | `"wallpaper"` |
| `TOPIC_MUSIC` | `"music"` |
| `COMMAND_SWITCH_MODE` | `"switch_mode"` |
| `COMMAND_NEXT_WALLPAPER` | `"next_wallpaper"` |
| `COMMAND_CHAT_INPUT` | `"chat_input"` |
| `COMMAND_NEXT_BILIBILI` | `"next_bilibili"` |
| `ACTION_FIELD` | `"action"`（命令信封的字段名） |
| `PAYLOAD_FIELD` | `"payload"`（命令信封的字段名） |
| `MODE_SLEEP` / `MODE_IDLE` / `MODE_STUDY` / `MODE_GAME` | `"SLEEP"` / `"IDLE"` / `"STUDY"` / `"GAME"` |

---

## 9. 本协议不涉及

- **没有二进制帧**：全程文本 JSON，可读性优先于极致带宽。
- **没有版本号字段**：v1 不需要；演进靠"加字段 + 忽略未知字段"。
- **没有鉴权/加密**：同机 Unix socket，靠文件权限（`/tmp/agent.sock` 属主与 mode）
  隔离；不要跨机暴露这个 socket。

## 10. 实现位置

| 一侧 | 实现 | 说明 |
| --- | --- | --- |
| Agent（server） | `agent/ipc/local_server.py` 的 `LocalServer` | 监听、`push()` 推状态、`on_command()` 收命令（收的是**命令信封**）；`agent/ipc/__init__.py` 的 `build_ipc()` 是 `agent/main.py` 的接入点 |
| GUI（client） | `gui/src/services/local_client.cpp`（C++ / Qt5） | 发命令用 `{"action","payload"}`（`sendCommand`），收推送解 `{"topic","data","timestamp"}`（`handleLine`）—— 两个方向的信封不同，见 §2 |
| 测试用 client | `agent/ipc/local_client.py` 的 `LocalClient` | **只用于测试/联调**，不是生产 GUI；它发的命令信封与 C++ GUI 逐字节一致 |

