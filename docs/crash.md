# 崩溃日志（T14-8）

> 一句话：**板端没有调试器，出事之后只能靠留在磁盘上的东西**。本文说清"崩了会留下什么、
> 在哪儿、怎么看、怎么按需抓现场"，以及为什么 `logs/crash/` 里留下的每一份都值得看。
>
> 相关：[`deploy.md`](deploy.md)（开机自启与日志去处）、[`architecture.md`](architecture.md)（进程拓扑）、
> [`gui.md`](gui.md)（GUI 侧）。

---

## 1. 两个进程，同一个目录

| 进程 | 谁装的 | 报告文件名 |
| --- | --- | --- |
| Agent（`python3 -m agent.main`） | `agent/core/crash_log.py`（`install_crash_logging`） | `logs/crash/agent-<YYYYmmdd-HHMMSS>-<pid>.log` |
| GUI（`gui/build/agent_gui`） | `gui/src/core/crash_log.{h,cpp}`（`installCrashLogger`） | `logs/crash/gui-<YYYYmmdd-HHMMSS>-<pid>.log` |

两边是**同一套约定**（刻意如此，免得两头要记两套）：

1. **每个进程一开始就把自己这一份建好**（带时间 / pid / argv / 工作目录 / 配置路径 / git 版本）；
2. **正常退出时删掉它** —— 所以 `logs/crash/` 里剩下的每一份都是"上一次没干净退出"；
3. 崩了/未捕获异常时，把**原因 + 最近若干条日志**追加进去；
4. 本次启动会把**上一份报告的头尾**打进 `logs/agent.out` / `logs/gui.out`（同一份只报一次）；
5. 只保留最新 **20** 份（`AGENT_CRASH_KEEP` 可改）。

`logs/` 已被 `.gitignore` 覆盖，所以这些东西不会让板端 `git status` 变脏。

---

## 2. 各自兜住了什么

| 情况 | Agent | GUI |
| --- | --- | --- |
| 未捕获异常（主线程） | `sys.excepthook` | （Qt 消息处理器 + `std::terminate`） |
| 未捕获异常（子线程） | `threading.excepthook` | — |
| 回调里的异常（unraisable） | `sys.unraisablehook` | — |
| **段错误等致命信号** | `faulthandler.enable()` → 全线程 Python 栈 | 自装 SIGSEGV/SIGABRT/SIGBUS/SIGILL/SIGFPE 处理器 → 写"最近消息"裸缓冲 |
| `qFatal` / 断言 | — | Qt 消息处理器（写完报告再 `abort`） |
| `std::terminate` | — | `std::set_terminate` |
| **按需抓现场（没崩但卡住）** | `kill -USR1 <pid>` → 全线程栈写进会话文件 | （GUI 侧没装这个） |

⚠ 两条边界，别指望它做更多：

- **native 栈（C++/mppvideodec/moonlight）拿不到**：Python 的 faulthandler 只给 Python 栈；
  GUI 的信号处理器只能写"崩溃前最近的消息"。真要 C++ 栈得开 core dump + gdb。
- **信号处理器里不能分配内存、不能调 Qt**：所以 GUI 侧另有一份**裸字节缓冲**（`g_tail`，
  64 KB，消息进来时 memcpy 进去），信号处理器只做 `write(2)` + 重新抛信号。这是刻意的设计约束，
  不是省事。

---

## 3. 正常退出会删文件 —— 那"收尾"怎么知道该不该删

这是最容易写反的一处，两边都用同一个开关（`_reported` / `g_reported`）：

```
正常退出（没有报告）        → close_cleanly() 删掉会话文件
未捕获异常（写过报告）      → close_cleanly() **不删**（否则刚写好的现场被自己删掉）
段错误/被 kill -9（没人收尾）→ 文件自然留着
```

Agent 用 `atexit.register(crash.close_cleanly)`、GUI 用 `std::atexit(...)` 收尾：
未捕获异常退出时 **atexit 照样会跑**，所以"先写报告、后收尾"这个顺序必须由 `_reported` 兜住。
`tests/test_crash_log.py` 与 `gui/tests/test_crash_log.cpp` 各有一条用例专门钉它。

---

## 4. 怎么看

```bash
ls -lt logs/crash/                       # 剩下的都是"没干净退出"的
cat logs/crash/agent-20260927-183001-1234.log
grep -n '崩溃/异常\|Fatal Python error\|致命信号' logs/crash/*.log
tail -40 logs/agent.out | grep -A30 '上次崩溃报告'   # 开机横幅（上次崩在哪）
```

报告的结构（顺序刻意如此）：

```
==== 头部 ====            进程 / 命令行 / 工作目录 / 解释器或 Qt 版本 / 配置 / git 版本
--- 最近 N 条日志/消息 ---  崩之前发生了什么
!!!! 崩溃/异常 + 时间 !!!! 为什么死
Traceback / faulthandler   现场
```

⚠ 为什么"原因"放在**最后**：开机横幅只取报告的**头尾**（中间省略），原因放中间就会被
省略掉，横幅里只剩一坨日志。

---

## 5. 怎么按需抓现场（没崩但想看看）

```bash
systemctl status agent.service           # 拿到 MainPID
kill -USR1 <MainPID>                     # Agent：全线程 Python 栈写进它的会话文件
cat logs/crash/agent-*.log | tail -60
```

⚠ 这次 dump **不会**让 Agent 退出；用完把会话文件删掉即可（它本来只该在"没干净退出"时留下）。

---

## 6. 怎么测

```bash
python tests/test_crash_log.py                  # PC / 板端都能跑（真起子进程崩给你看）
python tests/board/t14_8_accept.py              # 板端门禁：真 Agent + 真 GUI + 真信号
cd gui/build && QT_QPA_PLATFORM=offscreen ctest -R test_crash_log --output-on-failure
```

验收用的触发器（平时别用）：

```bash
python3 -m agent.main --crash-demo raise|thread|segv    # Agent
./gui/build/agent_gui --crash-demo fatal|segv           # GUI
```

---

## 7. 想关掉 / 改位置

| 环境变量 | 作用 |
| --- | --- |
| `AGENT_CRASH_DIR` | 换崩溃目录（默认是**可执行文件相对的 `<仓库根>/logs/crash`**；两侧同一个推导规则） |
| `AGENT_CRASH_KEEP` | 保留份数（默认 20） |
| `AGENT_CRASH_DISABLE` | 非空 = 完全不装（跑纯逻辑测试时用） |
