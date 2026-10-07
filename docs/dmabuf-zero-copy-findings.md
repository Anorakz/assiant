# dmabuf 零拷贝路线：实测结论与落盘记录（2026-10-06 ~ 10-07）

> 一句话结论：**「MPP 硬解 ⇒ dmabuf ⇒ Qt 自己的 EGL 上下文 ⇒ GL 纹理」这条路完全可行**（原型实测
> **210/210 帧零失败**）。此前 `qmlglsink`/`glupload` 的失败**与驱动无关**，而是 **GStreamer 侧
> EGL 初始化不了**。GUI 接入将走 **PC 侧交叉编译**（`scripts/build.ps1` 那套）。
>
> ⚠ 本文所有数字都来自板端实测；凡是没验到的，都单独标注了。

---

## 1. 结论与判据

| # | 项 | 结果 | 证据 |
|---|---|---|---|
| ① | 驱动是否支持 dmabuf | ✅ **支持** | `eglQueryString` 含 `EGL_EXT_image_dma_buf_import` ＋ `..._modifiers` ＋ `EGL_EXT_yuv_surface` |
| ② | `mppvideodec` 是否输出 dmabuf | ✅ **是** | `fakesink signal-handoffs` 回调里 `gst_is_dmabuf_memory` 命中 **592/592、745/745、210/210** 帧 |
| ③ | 能否拿到 dmabuf fd | ✅ **能** | `gst_dmabuf_memory_get_fd` ⇒ `fd=51/54/63/67…`（每帧复用） |
| ④ | 在 **Qt 自己的 GL 上下文**里导入成纹理 | ✅ **成功** | `eglCreateImageKHR(EGL_LINUX_DMA_BUF_EXT…)` ＋ `glEGLImageTargetTexture2DOES` ⇒ **`导入成功=210`（100%）** |
| ⑤ | 纹理里真有画面 | ✅ 是（弱证据） | `glReadPixels(0,0) = 00 00 00 ff` ⇒ **非全 0**（(0,0) 恰为暗像素，alpha=0xff） |
| ⑥ | 不需要 GStreamer 的 GL | ✅ 是 | 全程**不用** `glupload`/`qmlglsink`；GL 上下文由 **Qt** 提供 |
| ⑦ | 接进产品后能省多少 | ⏳ **待实测** | 预期 `vqueue:src` 0.82 核 ⇒ 接近 0（见 §2） |

### ★★ 两个关键点（缺一个就失败，都实测过）

1. **必须在 GStreamer 的 handoff 回调线程里 `makeCurrent`**
   —— EGL 的 current context 是**线程本地**的；否则 `eglGetCurrentDisplay()` 返回 `NO_DISPLAY`。
2. **NV12 是双平面：`eglCreateImageKHR` 必须同时给 plane1 的 fd/offset/pitch**
   —— 只给 plane0 ⇒ **`EGL_BAD_PARAMETER (0x300c)`**。

---

## 2. 实测数字（干净口径：串流已停、360P 流）

| 项目 | 数值 | 说明 |
|---|---|---|
| B 站视频总占用（现状 QtMultimedia 路径） | **≈1.13 核** | `agent_gui` 112.7% ＋ `ffmpeg` 1.0% |
| └─ **`vqueue:src`** | **0.82~0.94 核（82~83%）** | Qt 的 `videoqueue`：map＋YUV→RGB＋拷贝 |
| └─ `mppvideodec`（硬解） | **1.0%** | 硬解极轻，瓶颈从不是解码 |
| 源分辨率/帧率 | **640×360 / 24 fps** | 360P 改动生效；**帧率上限本来就是 24** |
| qmlglsink B2（`videoconvert!glupload`） | **119.1 / 119.6%（稳定复现）**，帧率仅 ~2–3 fps | 有中间拷贝 ⇒ **无收益** |
| `mppvideodec ! glupload`（真零拷贝尝试） | **`not-negotiated (-4)`** | 有效实测错误 |
| 钉 `video/x-raw(memory:DMABuf)` caps | **`not-linked (-1)`**；再插 `glcolorconvert` ⇒ `not-negotiated` | 同上 |
| `glimagesink`（裸 gst-launch） | `Failed to initialize egl: **EGL_NOT_INITIALIZED**` | ★ GStreamer 侧 EGL 起不来 |
| GL 警告（历史） | `unknown EGL_COLOR_BUFFER_TYPE value 3300` | 佐证 GStreamer 的 EGL 配置异常 |
| 参考：`waylandsink`（weston 下，早先实测） | **12.6%** | 唯一"不引入拷贝"的既有方案 |
| 参考：`glimagesink` 真实流（早先） | 30.4% | |
| 内存（画面识别解耦后） | `available 638 MB ⇒ 2.2~2.7 GB` | 见 §3.1 |
| Moonlight `VideoDec`（串流在跑时） | **≈1.07 核** | 串流一停，agent 121% ⇒ 3% |

---

## 3. 今天做的全部改动（都可逆）

### 3.1 产品代码（已提交、已上板）

| 提交 | 内容 |
|---|---|
| `17240f6` | **画面识别 ↔ 视频播放互斥**：在播则不抓帧、且不常驻 SigLIP（`agent/main.py::_game_watch_loop`，判据用 `self._buffer.snapshot()` 的 `saw_playing and playing`，与 `toggle` 同一条真值） |
| `a5ef019` | 匿名单文件清晰度 **1080P ⇒ 480P**（`agent/net/bilibili_api.py`，`qn=80 ⇒ 32`） |
| `a5d0bd5` | 再降一级 ⇒ **360P**（`qn=16`） |
| `2954b7b` | **DASH 档位也钉 360P**（`qn=112/80 ⇒ 16`）—— 高清会把像素量推上去、反而吃满 `vqueue:src` |

> ⚠ `2da0a8e`（B-wayland 的 buildroot 配置：weston ＋ qt5wayland）**仍在仓库历史里**；板上已回滚。

### 3.2 板端运行期改动（都在 `/data` 或 `/sys`，可逆）

- **绑核隔离**（drop-in）：
  `/etc/systemd/system/agent.service.d/zz-cpu.conf` ⇒ `AllowedCPUs=2 3`；
  `/etc/systemd/system/agent-gui.service.d/zz-cpu.conf` ⇒ `AllowedCPUs=0 1`。
  ⇒ 效果：**GUI/视频独占 CPU 0-1，agent＋LLM 独占 2-3**；`vqueue:src` 曾从 **75.4% ⇒ 23.9%**。
  ⇒ 回滚：删这两个文件 ⇒ `systemctl daemon-reload` ⇒ 重启对应服务。
- **`config.yaml`**：`llm.timeout_s 90 ⇒ 600`（按实测预估：2 线程 prompt ≈6–7 tok/s × ~1900 token
  ＋ 生成 200 token ≈ 8–11 分钟）；`llm.threads/threads_batch = 2`；
  `music.timeout_s` 45、`bilibili.timeout_s` 15 **已修回原值**。
  ⇒ 备份：`config.yaml.bak-llm2` / `config.yaml.bak-llm2b`。
- **`llm/config/llm.env`**：`LLM_THREADS=2` / `LLM_THREADS_BATCH=2`（真源是 `config.yaml`；
  ⚠ 注意**板上生效的是 `/usr/lib/assistant/llm/config/llm.env`** —— 由 agent 派生）。
- **B 站 cookie**：经正规入口写入 `assistant set cookie --sessdata … --apply --verify`
  ⇒ 落在 `/usr/lib/assistant/config/bilibili_cookie.json`（`.bak` 已留；该文件是凭据、已 gitignore）。
  ⇒ **SESSDATA 有效** ⇒ 走 DASH。

### 3.3 板上新增件（全在 `/data/assistant/`，可删）

```
qmlgl-test/           早先的 qmlglsink 原型
gst/libgstqmlgl.so    自编的 qmlglsink 插件（本路线最终未采用）
dmabuf-probe/         ★ dmabuf 直导原型（源码 + 交叉编译产物）—— 本路线的证据来源
board-*.sh            一批测试脚本（均支持"跑完自动恢复"）
perf/*.txt logs/*.log 全部测量与日志（见 §5 留档清单）
```

### 3.4 构建机（WSL/Windows）新增

```
/mnt/e/rk3568/tmp/build-dmabuf-probe.sh   ★ 交叉编译 dmabuf-probe（buildroot 工具链 + sysroot）
/mnt/e/rk3568/tmp/stage-gstapp*.sh        找 app 插件（结论：没编，见 §4）
/mnt/e/rk3568/tmp/dmabuf-probe/           源码与产物
$NEST/... 的 .config 四处已开 QMLGL（备份 .bak-qmlgl）
$NEST/host/bin 的 lrelease/lupdate 包装（备份在 /mnt/e/rk3568/tmp/hostbin-qt-tools-backup/）
```

---

## 4. 排查中确认的环境事实 / 坑（下次直接用）

1. **板上没有 `g++`**（`command not found`）⇒ 一切编译都在 **PC 侧交叉编译**。
   - 工具链：`$NEST/host/bin/aarch64-buildroot-linux-gnu-g++`（gcc 13.4.0）
   - pkgconf：`$NEST/host/bin/pkgconf`；sysroot：`$NEST/host/aarch64-buildroot-linux-gnu/sysroot`
   - 用法：`PKG_CONFIG_SYSROOT_DIR` ＋ `PKG_CONFIG_LIBDIR` ＋ `--sysroot`（模板见 `build-dmabuf-probe.sh`）
2. **板上缺 `app` 插件** ⇒ `appsink`/`appsrc` **不可用**（`gst-inspect-1.0 appsink` ⇒ no such element）；
   只有库 `libgstapp-1.0.so*`，**没有插件 `libgstapp.so`**。⇒ 原型改用
   **`fakesink signal-handoffs=true`**（core 元素，板上一定有）。
3. **GStreamer 侧 EGL 起不来**（`EGL_NOT_INITIALIZED`；`glimagesink`/`glupload` 全废）
   ⇒ ★ 这就是 qmlglsink 路线的死因，**与 Mali/dmabuf 能力无关**。
4. **B 站本机流 token 一次性/与 GUI 播放器绑定**：停 GUI 后再用旧 URL 会 `bad token (403)`；
   取"新 token"要用**行数增量法**（`grep -c 可以播了` 增一才取那条 `http://…`）。
5. **`systemctl restart agent` 会清掉 bilibili 会话**（播放中断）⇒ 实验后要恢复 LLM 时，
   用 `stop.sh` + `start.sh`（带 `LLM_ENV_FILE=/data/assistant/llm/config/llm.env`、
   `LLM_STATE_DIR=/data/assistant/llm`），**不要重启 agent**。
6. **LLM 速度是硬件约束**：生成 0.94–1.2 tok/s（内存带宽瓶颈），prompt 修复后 ~9.6 tok/s
   （见 `docs/perf-cpu-mem.md`）⇒ 一轮带工具往返的对话需要数分钟；`timeout_s` 要给足。
7. **`pkill -f <自身命令行里出现的字符串>` 会自杀**（ssh 命令行被匹配）⇒ 只用 PID 杀。
8. **ssh 内联命令里的引号/重定向会被 PowerShell 吃掉**（`2>/dev/null` 变路径、`sed 's|…|'` 报
   `unmatched /`）⇒ **一律写成脚本文件再执行**。
9. **`eglCreateImageKHR` 的错误码**：`0x300c = EGL_BAD_PARAMETER`（本次=少了 plane1）。
10. 跨设备文件路径：**`/data/assistant/...`（userdata）可写**；rootfs 只做极小的 drop-in 写入。

---

## 5. 留档清单（板端）

```
perf/framefps.txt perf/fps2.txt perf/fps3.txt    帧率/全核实验
perf/rot.txt                                    旋转代价（只跑了 rot90：117.4% / ~3 fps）
perf/b1final.txt perf/b2final.txt perf/b3final.txt perf/b4final.txt   qmlglsink 各档
perf/b345.txt perf/b345b.txt                    三档对照（含无效轮次）
perf/dmabuf1.txt dmabuf2.txt dmabuf3.txt dmabuf4.txt   ★ dmabuf 路线全过程
perf/apsink.txt                                  appsink 缺失的判定
perf/llm2.txt llm2b.txt fixto.txt guicpu.txt free.txt stutter.txt     LLM/绑核/卡顿
logs/dmabuf4-raw.log                             ★ 含 `✅ 零拷贝导入成纹理 OK` ×N 与 fd 铁证
```

---

## 6. 下一步：GUI 接入（**已批准**，尚未开工）

**构建方式：PC 侧交叉编译**（`scripts/build.ps1` 那套：`cmake/toolchain.cmake` ＋ `E:/rk3568/sysroot`；
必要时先跑 `scripts/setup-sysroot-deps.ps1` 补 dev 包）。

```
① 新增 `gui/src/ui/gst_video_widget.{h,cpp}` —— 继承 QOpenGLWidget（Widgets 原生，不引入场景图 FBO 合成）
   · 管线：souphttpsrc <本机流> ! tsdemux ! h264parse ! mppvideodec
           ! video/x-raw(memory:DMABuf) ! fakesink name=sink signal-handoffs=true sync=false
   · handoff 回调：只 ref 入队（2–3 帧），**不碰 GL**
   · paintGL()（widget 自己的 GL 上下文）：eglCreateImageKHR(plane0+**plane1**) ⇒
     glEGLImageTargetTexture2DOES ⇒ 画全屏四边形
   · 对外接口照抄现有语义：setSource/play/pause/toggle/isPlaying/position/duration/EOF
② `gui/src/ui/video_panel.cpp`：QVideoWidget ⇒ GstVideoWidget；删 QMediaPlayer；
   **对外信号与方法保持不变**；★ 初始化失败自动回退到原 QMediaPlayer 路径（不黑屏）
③ `gui/CMakeLists.txt`：gui_widgets 加 Qt5::OpenGL 与 GStreamer
④ 验收：`ps -eLo comm | grep vqueue` **为空**；agent_gui 从 ~1.1 核 ⇒ 0.2–0.4 核；
   帧率稳定 24 fps；目视画面/方向/暂停恢复正常；内存无异常增长
⑤ 回退：改动只在新文件 + video_panel 一处切换 ⇒ git revert 即恢复
```

### 未验证 / 风险（如实）

- `QOpenGLWidget` 的上下文里 EGL 导入**尚未实测**（原型是在 `QOffscreenSurface` 上下文里成功的）；
  若不行 ⇒ 回退（原路径）。
- 音频第一版**沿用原路径**（不动），只替换视频渲染。
- §2 里"接进产品后省多少"仍是**预期**，需接入后实测。
- `rot0`（无旋转）对照**没跑完** ⇒ "90° 旋转值多少"仍未知。
- SigLIP 的"**暂停才加载**"只做了一半（循环里已 gate，`_enter_state` 那条路**待补**）。

---

## 7. 追加：GUI 接入的实测（2026-10-07，提交 `2a905ea` + `8ff4af9`）

> 结论一句话：**零拷贝链路在 GUI 里已经打通**（`vqueue:src` 归零、CPU 降约 7.6 倍、画面正确），
> **但 `paintGL` 只有 3.7 fps（目标 ≥20）** ✗ —— 卡在 **Qt 的绘制调度**上，不在 dmabuf/EGL/解码。

### 7.1 已实测达成的数字（板端，360P 流、GAME 模式、`DSH_GST_VIDEO=1`）

| 指标 | 改动前 | 现在 |
|---|---|---|
| **`vqueue:src`** | 0.82–0.94 核（82%） | ★ **0**（`ps -eLo comm \| grep -c vqueue` ⇒ 0） |
| **`agent_gui` 进程** | 112.7% | ★ **14.8%**（约 **7.6 倍**；最好一轮 4.6%） |
| `mpp_dec_parser` | — | ~2% |
| 帧**到达**（`handoff` 回调） | — | ★ **22~27 fps**（≈源 24fps ⇒ 上游/解码/取流全部正常） |
| 帧**绘制**（`paintGL`） | — | ✗ **3.7 fps**（117 帧 / 32 秒）｜ 改前 2.0 fps |

画面 ✅：几何正确（808×300）、**绿条消失**、着色器 `link=1`、无 `eglCreateImageKHR` 失败；
稳定性 ✅：自愈关卡两次取样 `is-active`/`NRestarts` 一致（多次零重启）。

### 7.2 ★★ 七个真因（全部经板端证据定位并修复）

| # | 真因 | 证据/表现 | 修法 |
|---|---|---|---|
| 1 | **`VideoPanel` 自身没被 `show()`** | 心跳日志：父可见而 `gst_` 仍 `isVisible=false` | `relayoutStage` 里对整条链 `show()`；**必须 GAME 模式** |
| 2 | **`gst_bus_pop_filtered` 返回类型写成 `int`** | 指针被**截断** ⇒ `gst_message_unref` 拿到非法地址 ⇒ **段错误/无限重启** | 声明为 `void*` |
| 3 | **用了控件尺寸当缓冲区尺寸**（808×300） | 画面**噪点** | 用真实值 **640×360、stride=640** |
| 4 | **dmabuf 两个函数取错了库** | `gst_is_dmabuf_memory`/`gst_dmabuf_memory_get_fd` 在 **`libgstallocators-1.0.so.0`**（不在 libgstreamer）⇒ 恒 `nullptr` ⇒ 一有帧就段错误 | 从正确的库 `dlopen` |
| 5 | **`queue_` 跨线程无锁** | GStreamer 回调线程 enqueue × GUI 线程 dequeue ⇒ 内存破坏/崩 | **`QAtomicPointer` 单槽换手**（满则丢最旧） |
| 6 | **`update()` 按名字 `invokeMethod` 静默失败** | `QWidget::update()` 不是槽/`Q_INVOKABLE` | 改**函子重载** `invokeMethod(self, [self]{ self->update(); }, QueuedConnection)` |
| 7 | **NV12 的 UV 平面要 16 对齐高度** | 用 360 ⇒ UV 从 Y 平面取样 ⇒ **顶部绿条**（着色器有 Y 翻转 ⇒ 底部的错显示在顶部） | `H=368`、`uvOff = 640×368` |

★ 另做了两处（**非瓶颈**的优化，已在库）：纹理**只建一次**、每帧只换 EGLImage 并销毁上一帧。

### 7.3 ✗ 已排除的方向（都有实验证据，别再试）

| 方向 | 结论 |
|---|---|
| `QOpenGLWindow` ＋ `QWidget::createWindowContainer` | ✗ **eglfs 上启动即崩**（5 次重启 ⇒ `failed`，`paintGL`/`handoff` 一条日志都没有） |
| "每帧整窗 FBO 合成/blit 太贵" | ✗ **缩小视频区到 320×180，fps 完全不变**（`DSH_GST_SMALL=1` 实验）⇒ 与绘制成本无关 |
| "回退管线独占事件循环" | ⚠ **只对了一部分**：去掉 `player_->setMedia`（走零拷贝时）后 **`vqueue` 归零** ✓、fps 2.0⇒3.7 ✓，但不是全部 |
| "重绘风暴" | ✗ 那是真因 2/4 的**假象**（Qt 本来就会合并同一轮的多次 `update()`） |

### 7.4 ⏳ 剩下的唯一方向

**绕开 Qt 的绘制调度**：自建 EGL 表面 ＋ 主动 `eglSwapBuffers`（不经过 QtWidgets 的 FBO/合成）。
可直接复用的资产：dmabuf fd、**plane0＋plane1**、GLES2 着色器（`link=1`）、原子单槽换手、
UV 16 对齐 368、`DSH_GST_VIDEO` 开关、自愈式部署关卡（异常自动回滚 `agent_gui.bak-1007`）。

### 7.5 工程环境（这次踩出来的，务必沿用）

- 板上**没有 g++/cmake** ⇒ 一切编译走 **PC 侧交叉编译**：
  WSL 里 `bash /mnt/e/rk3568/tmp/build-gui-br.sh`（增量、产物稳定；**先 grep `' error'` 再看尾部**）。
  工具链 = buildroot sysroot（**Qt 5.15.11，与板端一致**）；`E:/rk3568/sysroot` 是 5.12.8 ⇒ **弃用**。
- 板上**缺 `app` 插件**（`appsink` 不可用）⇒ 用 **`fakesink signal-handoffs=true`**。
- 部署纪律：**确认 `build rc=0` 且产物存在才部署**；每次上板都带**两次取样 + 自动回滚**；
  播放前先备队列（`bilibili search`/`video next`），**别先切 IDLE**（会清 bilibili 会话 ⇒ 取不到 token）。
- ⚠ **教训**：原型阶段的 `✅ 零拷贝导入成纹理 OK` **只查了 `glGetError` ＋ 一个像素**，
  **不足以当作成功判据** ✗ —— 平面描述给错也照样"导入成功"（就是后来的"由黑变花"）。
  ★ **画面正确才是判据**。

### 7.6 ★★★ 2fps/3.7fps 的**定案**（第 6 次实验，`clock()` 分段计时）

> ★★ **本节结论已在 §7.8 被更正** ✗：当时判定的"**修法唯一 = 自建 EGLSurface**"是**错的** ✗。
> 第 68 轮把 Qt 5.15.11 源码读通之后，真因是 **Qt 把 `QOpenGLWidget` 的重绘请求当
> `Qt::LowEventPriority` 事件投递**，被 handoff 的"每帧一个 queued 调用"**饿死** ✗；
> 而且 `eglCreateWindowSurface(winId())` 这条路**在 eglfs 上根本走不通**（§7.8 有崩的证据 ✓）。
> 下面这段"paintGL 内部只要 1ms、瓶颈在外面"的**测量本身仍然成立** ✓，留着当过程证据 ✓。

在 `paintGL()` 首尾用 `clock()` 计时并每 10 帧打印，实测（板端，360P，GAME，零拷贝启用）：

```
[gstvideo] paintGL#140 内部耗时(ms)= 1.11  pending=1 size=100x1
[gstvideo] paintGL#150 内部耗时(ms)= 0.735 pending=1
[gstvideo] paintGL#160 内部耗时(ms)= 1.202 pending=1
[gstvideo] paintGL#170 内部耗时(ms)= 1.424 pending=1
[gstvideo] paintGL#180 内部耗时(ms)= 1.717 pending=1
[gstvideo] paintGL#190 内部耗时(ms)= 0.681 pending=1
[gstvideo] paintGL#200 内部耗时(ms)= 4.975 pending=1
[gstvideo] paintGL#210 内部耗时(ms)= 0.659 pending=1
```

- 同期帧率仍是 **≈3.2 fps**（`#140`@03:18:51 ⇒ `#210`@03:19:13 = 70 帧/22 秒）
- 同轮：**`vqueue` = 0** ✓、`agent_gui` CPU **14.8%**（原版 112.7% ⇒ **7.6 倍**）

⇒ ★★★ **结论**：**`paintGL` 内部只要 ~1ms** ✓ ⇒ **那 ~300ms 的间隔发生在 `paintGL` 之外** ✗，
即 **Qt 的 `QOpenGLWidget`「FBO ⇒ 窗口合成 ⇒ 交换」路径**。

★ 这与前面所有旁证一致：CPU 只有 10~15% ✓、Mali 后端线程 ≤0.8% ✗ ⇒ **不是算力，是阻塞/等待**。

**⇒ 因此修法唯一**：**绕开 `QOpenGLWidget` 的交换路径** —— 自建 `EGLSurface`
（`QWidget::winId()` ⇒ `eglCreateWindowSurface` ⇒ `eglMakeCurrent` ⇒ 画 ⇒ **主动 `eglSwapBuffers`**），
绘制部分可直接复用现有资产（dmabuf fd、**plane0＋plane1**、GLES2 着色器、原子单槽换手、UV 16 对齐 368）。

★ 已用实验排除的方向（**别再试**）：`QOpenGLWindow`＋`createWindowContainer`（eglfs 崩）／
"与尺寸相关的绘制成本"（视频区 320×180 fps 不变）／`update()` 排队被合并（同步 `repaint()` 同样 ~3.4fps）／
`glGenTextures`+`glDeleteTextures` churn／每帧 `eglCreateImageKHR`（fd 缓存后 create 降到 11 次/2 分钟，fps 不升反降）。

### 7.7 ★ 开工清单（给下一个会话：一次做到 ≥20fps）

> ★★ **本清单的 1~3 步（自建 EGLSurface）已被 §7.8 判死** ✗：`QWidget::winId()` 在 eglfs 下
> 不是原生窗口（实测值 `0x14` ✓），真去调 `eglCreateWindowSurface` **把 GUI 搞崩了一次**
> （`NRestarts=1` ✓）⇒ **别再做这条路** ✗。**第 4~5 步（构建/部署/关卡/算 fps）仍然有效** ✓，
> 是每次上板都要走的闭环 ✓。

**目标**：只在 `GstVideoWidget` 里加一条"自建 EGL 表面 ＋ 主动交换"的路（建议 `DSH_GST_EGL=1` 开关守卫，
**默认不开**，确保不破坏现有可播路径）。绘制部分**原样复用** `paintGL` 里的逻辑。

1. **创建表面**（在 `initializeGL()` 里，或首次绘制时）：取 `winId()`（`QWidget::winId()` ⇒ `EGLNativeWindowType`）
   ⇒ `eglCreateWindowSurface(dpy, cfg, winId, (EGLint[]){EGL_NONE})`；`cfg` 用与当前上下文兼容的 `EGLConfig`
   （可用 `QOpenGLContext::format()` 匹配，或直接沿用 Qt 已选的 config）。
2. **每帧**：`eglMakeCurrent(dpy, surf, surf, ctx)` ⇒ 复用绘制（dmabuf ⇒ `eglCreateImageKHR`(plane0+**plane1**)
   ⇒ `glEGLImageTargetTexture2DOES` ⇒ 着色器画全屏四边形）⇒ **`eglSwapBuffers(dpy, surf)`**。
3. **驱动**：`QTimer` ~25~40ms ⇒ 帧率 = 我们主动交换的次数（目标 ≥20）。
4. **收尾**：`eglDestroySurface`／销毁上下文（或进程退出时随 Qt 释放）。

**验证闭环（务必按序）**：
```bash
# 1) 构建（WSL，增量；先看 error 再看尾部）
wsl -e bash -lc "bash /mnt/e/rk3568/tmp/build-gui-br.sh 2>&1 | grep -a -e ' error' -e 'build rc' -e 'agent_gui ' | head -12; md5sum /home/anorak/build-gui-br/agent_gui"
# 2) 部署前**必须**确认 md5 变了（否则会白测旧版本）；然后：停服务 ⇒ scp 二进制 ⇒ 起服务
#    ★ 别忘了这一步：scp /mnt/e/rk3568/tmp/zz-gstvideo.conf（DSH_GST_VIDEO=1）到
#      /etc/systemd/system/agent-gui.service.d/  ⇒ systemctl daemon-reload
# 3) assistant mode GAME（必须！否则面板不可见）⇒ 先备队列（bilibili search / video next）再取流
# 4) 自愈式关卡：两次取样 is-active 与 NRestarts，异常就在同一条命令里回滚 agent_gui.bak-1007
# 5) 算 fps：journalctl -u agent-gui --since=-80s | grep -a 'paintGL#'（打点间隔 ×120）
```

**本次会话留下的现成物**：
- 分支 `main`：`8ff4af9`（链路打通＋致命 bug 修复）、`fc5ee59`/`407bbe5`（本文档 §7）、
  `0c18271`（**已预置** `eglCreateWindowSurface`/`eglMakeCurrent`/`eglSwapBuffers`/`eglDestroySurface`，
  并含 fd 缓存 EGLImage、`repaint()`、`paintGL` 计时、`DSH_GST_SMALL` 实验开关）
- 最新可部署二进制：`~/build-gui-br/agent_gui` = `dd1eef4b3c3643660b0bdc8cba5fde02`（**未上板**）
- 板端：**出厂原版** `c67fb08ea277cd389960d4762e3c0358`（`agent_gui.bak-1007` 同 md5），
  零拷贝开关未启用 ⇒ 可正常使用；部署新版本后**务必**保留回滚路径。
- ★ 关键目标数据（必须复现）：`vqueue:src` = **0**；`agent_gui` CPU 从 112.7% 降到 ~15%；
  `paintGL` 内部耗时 ~1ms；**唯一待攻**：paint 间隔 ~300ms（Qt 合成/交换路径）。

---

## 7.8 ★★★ 第 68 轮（2026-10-08）：真因是"**重绘请求被饿死**"＋一次 A/B 实测

### 7.8.1 真因（有 Qt 5.15.11 源码证据，不是猜）

板子上的 Qt 源码树（buildroot 输出里就有一份 ✓）：
`…/build/qt5base-da6e958319e95fe564d3b30c931492dd666bfaff/src/`

1. `QOpenGLWidget` 的画面**只能**由 `paintEvent` 驱动：
   `QOpenGLWidgetPrivate::render()` 的调用点只有 `QOpenGLWidget::paintEvent()` 与 `grabFramebuffer()`，
   而 `render()` 又是 `invokeUserPaint()` ⇒ `paintGL()` 的**唯一**入口
   （`widgets/kernel/qopenglwidget.cpp` 源码行 901/915/958/1327 ✓）。
2. 谁给 `QOpenGLWidget` 发 `paintEvent`：`QWidgetRepaintManager::sync()` 里那条
   "render-to-texture 控件不在常规脏表里"的分支 ⇒ `w->d_func()->sendPaintEvent(w->rect())`
   （`widgets/kernel/qwidgetrepaintmanager.cpp` 源码行 924~975 ✓）。
3. 上面那次 `sync()` 由"**UpdateRequest 事件**"触发，而那个事件是：
   ```cpp
   // qwidgetrepaintmanager.cpp  sendUpdateRequest()
   case UpdateLater:
       updateRequestSent = true;
       QCoreApplication::postEvent(widget, new QEvent(QEvent::UpdateRequest), Qt::LowEventPriority); // ★★★
       break;
   case UpdateNow: {                       // repaint() 想走这条（同步 sendEvent ✓）
       QEvent event(QEvent::UpdateRequest);
       QCoreApplication::sendEvent(widget, &event);
   ```
   而且同一函数开头还有一条**降级**：窗口正在合成、且距上次合成 ≤ `1000/refreshRate` 毫秒时，
   `UpdateNow` **被改成 `UpdateLater`** ⇒ **`repaint()` 并不保证同步** ✓。
4. ⇒ **`Qt::LowEventPriority` 的事件只有在"队列里没有普通优先级事件"时才会被处理** ✓。
   而 handoff 线程原本**每帧 post 一个 queued 调用**（24 个/s 的**普通**优先级事件 ✗）
   ⇒ 队列永远不空 ⇒ 那条 UpdateRequest **被饿死** ⇒ 实测 **20 个请求/s 只换来 0.8 次 paint** ✗
   = 历史上的 **2~3fps** ✓✓。这就是真因。

### 7.8.2 一次上板把"驱动方式"逐档量完（诊断版 `6e0bb323`，GAME 模式，360P，零拷贝）

用一个**运行时可切的模式文件**（`/data/assistant/gstvideo-mode`，500ms 轮询 ✓）把几种驱动方式
在同一进程里逐档量（每档 25 秒，号志来自日志计数器的真实增量 ✓）：

| 档位 | `paintGL` | 窗口合成 `frameSwapped` | 结论 |
|---|---|---|---|
| baseline（handoff 每帧 queued 调用 repaint ✗） | **1.2/s** | 2.4/s | 复现旧现象（2~3fps）✓ |
| blank（只清屏、不导入 dmabuf） | 0.8/s | 3.2/s | **与画什么无关** ⇒ 排除"GL 工作慢" ✓ |
| **GUI 线程自己 25ms QTimer 驱动 repaint**（仍是空白清屏） | **33.6/s** | 39.2/s | ★ 驱动方式一换就通 ✓ |
| **同上 + 真画（dmabuf ⇒ 外部纹理 ⇒ 着色器）** | **35.6/s** | 41.6/s | ★★ **画什么不影响** ⇒ 瓶颈是驱动 ✓ |
| finish（真画 + `glFinish()`） | 1.6/s | 4.0/s | `glFinish` 耗时 **1.9~10.0ms** ⇒ GPU 本身不慢 ✓ |

★ 顺带用 `EGL探针` 判死了 §7.6/§7.7 的那条"唯一修法"：
`widget-winId= 0x14`、`window-winId= 0x1`（**不是**原生窗口句柄 ✓）；真去调
`eglCreateWindowSurface` ⇒ **进程崩了一次**（`NRestarts=1` ✓，靠"模式文件先清空"的自愈才没崩成循环 ✓）
⇒ **自建 EGLSurface 这条路关闭** ✗。

### 7.8.3 本轮改了产品代码（`gst_video_widget.{h,cpp}`）

1. `onHandoff()`：**删掉** `QMetaObject::invokeMethod(self, [self]{ repaint(); }, Qt::QueuedConnection)`
   —— 那正是"每帧一个普通优先级事件" ✗；现在回调里**只做一次原子换手**（单槽、丢最旧 ✓）。
2. 新增 **GUI 线程 25ms `QTimer`** ⇒ `tickPaint()`：`pumpBus()`（EOS/错误照常上报 ✓）＋ `repaint()`。
3. `DSH_GST_PARTIAL=1` / `DSH_GST_REPAINT_ALWAYS=1` 两个 A/B 开关（默认关 ✓，用于继续分档 ✓）。
4. 打点：每 80 拍（2 秒）一行 **心跳**（pipeline / playing / **有帧** / **已画帧** / 零拷贝 ✓），
   每 120 次绘制一行 `绘制帧率 fps=` ✓ ⇒ **"已画帧"增量就是真实绘制帧率** ✓（比数 paint 次数可靠 ✓）。
5. 删掉死路与脚手架：EGL 自建表面探针 ✗、`DSH_GST_SMALL`、模式文件机制。

### 7.8.4 ⚠ 仍未达标（**如实记录，别当成成功** ✗）

同一 60 秒窗口里同时量（板端，`DSH_GST_VIDEO=1`，GAME 模式 ✓）：

```
帧到达   handoff#1560 @03:51:49 → #1800 @03:51:58   ⇒ ≈ 27 帧/s ✓（源就是这么快 ✓）
paintGL  「每 120 帧 35212 ms」                     ⇒ 3.4 次/s ✗
真的画出 心跳 已画帧 113 → 138 / 10 秒              ⇒ 2.5 帧/s ✗（零拷贝帧数与它同步 ✓）
心跳里「有帧= false」几乎是每一拍               ✗ ← 槽在 tick 时是空的
CPU      agent_gui 24.8%（QNetworkAccessManager 1.6%、vqueue=0 ✓、无 eglCreateImageKHR 失败 ✓）
```

⇒ **结论（写给下一个会话）**：
- **驱动方式这一层已经定案并改对** ✓（"起播瞬间"的心跳是**每 24ms 一次绘制** = ~40fps ✓）；
- **但稳态只有 2.5~3.4 帧/s** ✗，而且"**有帧=false**"说明：**帧到达 27/s、真正被消费/画出的只有 2.5/s**
  ⇒ 下一步要查的是**"谁把单槽里的帧拿走了 / 为什么 tick 时槽是空的"** ✗，而不是再动合成路径 ✗。
  优先怀疑（下轮按序排除）：① `paintGL` 的**静默早退分支**（`!mem || !is_dmabuf_memory` ⇒ 返回前
  已经把帧消费掉 ✗ —— 现在**没有打点**，先给它加计数 ✓）；② 是否**存在两个 `GstVideoWidget`
  实例**（`initializeGL` 一次运行里被调用两次 ✓ 需确认是不是同一个对象 ✓）；
  ③ `setSource()` 是否被反复调用（每次都会 `teardown()` 清槽 ✓，URL 带 `?v=` 会绕过 `stream == source_` 守卫 ✗）。

### 7.8.5 板端状态与"怎么交回用户"（截至本轮结束）

- 当前板端**已回滚成出厂原版** ✓ `agent_gui` = `c67fb08ea277cd389960d4762e3c0358`
  （`agent_gui.bak-1007` 同 md5 ✓）、**删掉** `zz-gstvideo.conf` ⇒ 与用户开工前一致 ✓、可正常播放 ✓。
- 本轮构建出来的二进制留在板上：`/data/assistant/gui-new/agent_gui`（见提交信息里的 md5 ✓）。
  一键部署（**自愈式关卡**，失败自动回滚 ✓）：
  ```sh
  scp agent_gui rk3568:/data/assistant/gui-new/agent_gui && \
  ssh rk3568 "sed -i 's/\r\$//' /data/assistant/board-deploy-probe.sh; sh /data/assistant/board-deploy-probe.sh"
  ```
  （关卡脚本本体 `/data/assistant/board-deploy-probe.sh`，回滚件 `agent_gui.bak-1007` ✓）
- 本轮踩到并修掉的**部署坑** ✗：从 Windows `scp` 上去的文件在板上是 **0644** ✓，
  `cp -f` 覆盖目标后**丢掉可执行位** ⇒ systemd 报 `203/EXEC Permission denied` ✓
  （**关卡正确拦下并回滚了** ✓）⇒ **每次拷完必须 `chmod 755`** ✓（脚本里已加 ✓）。
- 本轮用到的板端脚本（都在 `/data/assistant/`）：`board-final.sh`（客观验收：关卡＋布局＋自截图 ✓）、
  `board-ab.sh`（三档 A/B ✓）、`board-rate.sh`（同一窗口量"到达 vs 绘制" ✓）、
  `board-brightness.sh`（临时调背光 ✓ 只写 sysfs ✓ 重启即恢复 ✓）。
- 客观验收里 **布局那一项是过的** ✓：`GstVideoWidget#GstVideoSurface size=808x300 visible=1`
  （`--dump-layout` 取证 ✓）⇒ 面板位置/可见性没问题 ✓；差的就是**稳态绘制帧率** ✗。
- 另外：`window.grab()` 的 `--screenshot` 那次**段错误**（退出码 139 ✗）⇒ 以后别用 `--screenshot`
  来验零拷贝画面 ✗（用"心跳里的已画帧增量"✓ 或人工目视 ✓）。

### 7.8.6 第 69 轮（2026-10-08，离线）：把"矛盾"写清楚 ＋ 一组**决定性判据**

**先把矛盾摆出来**（都来自 7.8.4 那次 60 秒窗口的同一次运行 ✓）：

| 量 | 值 | 由它推出的结论 |
|---|---|---|
| 帧到达（`handoff#` 增量 ✓） | 26.6 次/s | 单槽每 ~37ms 就被写入一次 ✓ |
| `paintGL` 调用 | 2.8~3.4 次/s（且每次打点都 `pending=1` ✓） | **取走**单槽的只有 2.8~3.4 次/s ✓ |
| 心跳里 `有帧=` | **false 占 5/6** ✗ | 采样时单槽**是空的** ⇒ 被以 ~26/s 的速度取走 ✗ |

⇒ 三条**互相打架** ✓：写入 26.6/s、唯一取走者 2.8/s、可是槽却常常是空的 ✗。
只可能是下面两者之一 ✓：

- **(A) 进程里不止一个 `GstVideoWidget`** ✓：帧写进了**另一个实例**的单槽 ✓，而心跳/打点看的是这个实例 ✓。
  —— 而上一轮的诊断计数器全是 **`static`** ✗（`cbCount` / `tickN` / `paintCount` 都写在函数里 ✓），
  **多实例时会把两边的流量混成一个序列** ✗ ⇒ 正好能把这种矛盾**掩盖掉** ✓。
  （代码上 `Pages/MainPage/VideoPanel/GstVideoWidget` 都只 `new` 一次 ✓，所以这条要靠实测确认 ✓）
- **(B) 只有一个实例，但单槽被谁悄悄清掉** ✓：除 `paintGL` 外，只有 `teardown()` 会清槽 ✓
  ⇒ 要么 `setSource()` 被反复调用（每次换 URL 会绕过 `stream == source_` 守卫 ✓），
  要么 `paintGL` 的**静默早退**把帧取走却不画 ✓（`!mem` / `!is_dmabuf_memory` 两条分支以前**没有打点** ✗）。

**本轮改了什么（就是为了把这 A/B 一次判死 ✓）**：

1. 诊断计数器**全部改成每实例**（`handoffN_` / `tickN_` / `paintN_` / `retNoBuf_` / `retNoMem_` /
   `retNotDmabuf_` / 每实例 `QElapsedTimer fpsClk_` ✓），并且**每条诊断日志都打 `this=`** ✓
   ⇒ 一眼看出"是不是两个实例" ✓。
2. `onHandoff` 里新增 **`槽内旧帧=`** ✓ —— 这是最锋利的一条 ✓：
   若单槽真被 ~26/s 取走 ⇒ 几乎每次都 `false` ✓；若取走只有 2.8/s ⇒ 绝大多数是 `true` ✓。
3. `paintGL` 的两条静默早退**各自计数** ✓（原来合在一个 `if` 里、且不打点 ✗）。
4. 修正帧率打点的**算错** ✗：上一版前 20 帧也打点却仍用固定 120 当分子 ⇒ 打出 `fps=5000.0` 这种假值 ✗；
   现在只在 120 的整数倍打点 ✓（分子分母都是真实增量 ✓）。
5. `setSource()` / `teardown()` / 构造 / 析构都打 `this=` ✓（用来抓"反复重建/多实例"✓）。

**下一个上板会话只做一件事**（一次播放即可判定 ✓，不需要重建 ✓）：

```sh
# 1) 把已构建好的探针版送上去（板上现在是出厂原版 ✓）
scp E:/rk3568/tmp/agent_gui.round69 rk3568:/data/assistant/gui-new/agent_gui   # md5 34f4c3ad309d830365f41f3c4ae1d609
ssh rk3568 "sh /data/assistant/board-deploy-probe.sh"                          # 自愈式关卡 + chmod 755 ✓
# 2) 取一条新流，看 60 秒日志里这三样：
#    · 出现几个不同的 `this=`（1 个 ⇒ 走 B 分支；≥2 个 ⇒ 走 A 分支）
#    · `槽内旧帧= true/false` 的比例
#    · 心跳里 `早退(无帧/无内存/非dmabuf)` 三个计数有没有在涨
```

**这条探针的构建物**：`~/build-gui-br/agent_gui` = **`34f4c3ad309d830365f41f3c4ae1d609`**（1,223,096 字节 ✓）
＋ Windows 侧留档 `E:\rk3568\tmp\agent_gui.round69` ✓（**未上板** —— 板子这一轮被用户要求断电了 ✓）。

### 7.8.7 第 72 轮（2026-10-08，仍然离线）：先用**已有日志**判掉一个分叉 ＋ 换一条不依赖"槽"的主驱动

**(1) 用已经下载到 PC 的日志，离线判掉了"两个实例"这个分叉** ✓

`--dump-layout` 会把**整棵控件树**打出来（第 68 轮 run A 的 `final-layout.log` ✓ 已回传 ✓），
按名字数一遍就知道有几个实例：

```
GstVideoSurface = 1     VideoScreen = 1     VideoPage = 1     PageStack = 1
LAYOUT   GstVideoWidget#GstVideoSurface size=808x300 min=-1x-1 hint=-1x-1 visible=1
```

⇒ **只有一个 `GstVideoWidget`** ✓ ⇒ §7.8.6 的 **(A) 分支（帧写进另一个实例）判死** ✓。
（顺带：`probe-modes-journal.log` 里 `initializeGL` 出现 **4 次** ✓ ⇒ 这个 `QOpenGLWidget`
的 GL 上下文在一次会话里被 Qt **重建过多次** ✓ —— 值得记着，但**不是**"槽为空"的解释 ✓。）
⇒ 剩下的只能是 **(B)**：**单槽被谁悄悄取走/清掉** ✓（`paintGL` 的静默早退？`teardown()`？）
⇒ 第 69 轮那套探针（`id=` / `存活实例=` / `回调=` / `存槽=` / `槽内旧帧=` / 三个早退计数 ✓）
就是为这个准备的 ✓，下一次上板一次即可定案 ✓。

**(2) 同时换一条**不依赖"心跳那一刻槽里恰好有帧"**的主驱动** ✓（这是本轮的产品改动）

```cpp
// onHandoff()：存完帧之后，投一个**低优先级**事件给 GUI 线程 ✓
QCoreApplication::postEvent(self, new QEvent(QEvent::User), Qt::LowEventPriority);
// GstVideoWidget::event()：收到就 repaint() ✓
if (e->type() == QEvent::User) { ++evtN_; if (playing_) repaint(); return true; }
```

为什么这条更稳 ✓：
- 它是 **`Qt::LowEventPriority`** ✓ ⇒ 与 Qt 自己的重绘请求**同一条低优先级通道** ✓，
  **不会**重演"每帧一个普通事件 ⇒ 把 Qt 的重绘请求饿死"那个真因 ✗（§7.8.1 ✓）；
- 由**到达**触发（每帧恰好一次 ✓，~24/s ✓）⇒ 不再要求"25ms 心跳采样时槽里还有帧" ✓
  —— 上一轮恰恰是这个条件不成立（`有帧=false` 占 5/6 ✗）⇒ 心跳驱动的实际绘制只有 2.5~3.4fps ✗；
- 25ms 心跳**保留**做兜底 ✓；两条通道都失效才会退回旧现象 ✓。

**(3) 顺手修掉一个真 bug**：`sink_` 来自 `gst_bin_get_by_name`（**多一个引用** ✓），
以前的 `teardown()` 只把指针置空、**从不 unref** ✗ ⇒ **每换一次源就泄漏一个 sink** ✓（已修 ✓）。

**本轮构建物**：`~/build-gui-br/agent_gui` = **`89ac038cda9e880616e3bc4a6df5ff87`**（1,223,408 字节 ✓）
＋ Windows 留档 `E:\rk3568\tmp\agent_gui.round72` ✓（**未上板**：板子仍是断电的原版 ✓ ✓）。

**下一个上板会话（一次播放即可判定 ✓＋顺手就是验收 ✓）**：

```sh
scp E:/rk3568/tmp/agent_gui.round72 rk3568:/data/assistant/gui-new/agent_gui
ssh rk3568 "sh /data/assistant/board-deploy-probe.sh"        # 自愈式关卡 ✓ 内含 chmod 755 ✓
# 然后取一条流看 60 秒日志：
#   ① `绘制帧率 fps=`（**这次才是真验收数字** ✓ 目标 ≥20）与心跳里的 `已画帧=` 增量
#   ② `存活实例=`（应恒为 1 ✓）、`回调=` vs `存槽=`（应同步增长 ✓，不同步就是"没存进去"）
#   ③ `事件=` 是否跟帧率同阶（~24/s ✓）；`早退(无帧/无内存/非dmabuf)=` 三档有没有在涨
#   ⇒ 若 fps ≥20 且画面正确 ⇒ 直接收口（默认打开零拷贝 + 更新文档 + 提交 ✓）
#   ⇒ 若仍低 ⇒ 按 ② 的三对数定位到"取走"那一环 ✓
```

### 7.8.8 第 73 轮（2026-10-08，仍离线）：补上"到达的形状"，并修掉一个自造的事件号风险

**(1) 一个被"平均数"掩盖的可能** ✓：前几轮只知道"到达 **平均** 26/s" ✓ —— 但**平均**完全可能是
"**一阵一阵**" ✗（本地 http 流是按块喂的 ✓，`souphttpsrc` 一给就是一块，解码器把这一块一口气解完 ✓）。
而"一阵一阵"恰好能解释那个矛盾 ✓：**心跳每 2 秒才采一次样** ✓，若帧是"来 0.2 秒、歇 4 秒"，
采样自然大多落在**空档**上 ⇒ `有帧=false` 占多数 ✓✓。⇒ 这一轮把"形状"直接打进心跳 ✓：

```
窗口到达=            ← 本窗口（两次心跳之间）真实到达的帧数 ✓
最大到达间隔(ms)=    ← 本窗口内两次到达之间的**最大**间隔 ✓（几十 ms ⇒ 平滑 ✓；上千 ms ⇒ 一阵一阵 ✗）
距上次到达(ms)=      ← 采样那一刻离上次到达多久 ✓（≈2000 ⇒ 现在是空的 ✓）
```

配合第 72 轮的 `回调=` / `存槽=` / `事件=` / `已画帧=` ✓，这一次上板就能把"**没存进去**" ✗、
"**被谁取走**" ✗、"**到达本来就是一阵一阵**" ✗ 三种情况**彻底分开** ✓。

**(2) 修掉一个我自己埋的风险** ✗：第 72 轮那把"帧到达事件"用了裸 **`QEvent::User`** ✗ ——
那是"用户事件"的**基址** ✓，别的代码/别的库也可能往控件上投同号事件 ⇒ **撞车** ✗。
已改成专用号 **`QEvent::User + 77`**（`const int kFrameEventType` ✓，收发两侧都用它 ✓）。

**本轮构建物**：`~/build-gui-br/agent_gui` = **`7665c0a3db2718a6b68a2cf85dcd2805`**（1,227,664 字节 ✓）
＋ Windows 留档 `E:\rk3568\tmp\agent_gui.round73` ✓（**未上板** ✓，板子仍是断电的原版 ✓）。

⇒ **下一个上板会话用 round73 这一版**（它包含 §7.8.7 的"到达驱动"＋本轮全部诊断 ✓）：
```sh
scp E:/rk3568/tmp/agent_gui.round73 rk3568:/data/assistant/gui-new/agent_gui
ssh rk3568 "sh /data/assistant/board-deploy-probe.sh"     # 自愈式关卡（含 chmod 755）
```

### 7.8.9 第 74 轮（2026-10-08）：**离线能查的假设已经全部判死** ⇒ 只剩上板

把 §7.8.6 的 (A)/(B) 两支在**不上板**的前提下逐条查完 ✓：

| 假设 | 判据（都在代码/已回传日志里 ✓） | 结论 |
|---|---|---|
| (A) 进程里有**两个** `GstVideoWidget` | `final-layout.log` 数控件树：`GstVideoSurface = 1` ✓ | **判死** ✗ |
| (A') 有**第二个窗口**（`--dump-layout` 只走一棵树，查不到别的窗口 ✗） | `main.cpp` 只 `MainWindow window;`（第 598 行 ✓），全文没有第二处构造 ✓ | **判死** ✗ |
| (A'') `gst_` 被 `setParent(nullptr)` 摘出树外 | `video_panel.cpp` 里只有 `video_->setParent(screen_)`（回退时挂回 ✓），**从不动 `gst_`** ✓ | **判死** ✗ |
| (B①) `setSource()` 被反复调用（每次都 `teardown()` 清槽 ✓） | 它会把 `framesShown_`/`zeroCopyFrames_` **清零** ✓，而实测这两个数**单调增长**（113→138 ✓）⇒ 该窗口内**没发生过** ✓ | **判死**（该窗口内）✗ |
| (B②) `teardown()` 被反复调用 | 同上：`teardown()` 后 `pipeline_ = nullptr` ⇒ 心跳会打 `pipeline=false` ✗，实测恒 `true` ✓ | **判死** ✗ |
| (B③) `paintGL` 的静默早退把帧取走 | 实测 `已画帧` 增量 2.5/s ≈ `paintGL` 2.8/s ✓ ⇒ 早退**几乎没发生** ✓（第 72 轮已加独立计数，上板复核 ✓） | 存疑但**不像** |
| "到达本来就是一阵一阵"（平均数骗人 ✓） | **本机无法判** ✗ ⇒ 只能靠第 73 轮新加的 `最大到达间隔(ms)=` 上板看 ✓ | **待上板** |

★ 宿主侧测试的边界（顺带查清 ✓）：`scripts/test-host.ps1` 走的是 **MinGW g++ 的原生构建**
（`-G "MinGW Makefiles"`，无 toolchain 文件 ✓），而**这台 PC 上没有 Windows Qt**
（`C:\Qt`/`E:\Qt` 都不存在 ✓，`E:\rk3568\sysroot` 里是 **Linux** 的 Qt5.12.8 ✓ 不能在 Windows 跑 ✗）
⇒ **`GstVideoWidget` 的 GL/事件路径无法在宿主侧验证** ✗ ⇒ 只能上板 ✓
（宿主 CI 那套由 GitHub 的 `host-ci` 跑 ✓，本轮提交都是 **success** ✓。）

⇒ ⇒ **结论：离线能做的已经做完** ✓ —— 诊断、候选修法（到达驱动 ✓）、
可查假设的全部排除 ✓、以及每一步的产物/命令都落在文档里 ✓。
**剩下的每一步都需要板子上电** ✗（板子是**用户要求断电**的 ✓，且这块板子没有远程开机 ✗）。

**上电后照这个顺序走（一次播放即可判定＋顺手验收 ✓）**：

```
① scp E:/rk3568/tmp/agent_gui.round73 rk3568:/data/assistant/gui-new/agent_gui
② ssh rk3568 "sh /data/assistant/board-deploy-probe.sh"        # 自愈式关卡（含 chmod 755 ✓）
③ ssh rk3568 "sh /data/assistant/board-rate.sh"                # 取流 + 60 秒窗口
④ 看四组数：绘制帧率 fps=（目标 ≥20）｜存活实例=（应恒 1）
             回调= vs 存槽=（应同步）｜事件=（应 ~24/s）｜窗口到达=/最大到达间隔(ms)=
             早退(无帧/无内存/非dmabuf)=
⑤ 判：
   · fps ≥20 且画面正确（无绿条/噪点、方向宽高比正常 ✓）⇒ **收口**
   · 存槽 不涨 ⇒ 帧没进槽（查 buffer_ref/回调线程 ✓）
   · 存槽 涨但 有帧=false 且早退涨 ⇒ 被 paintGL 静默早退取走（查 peek_memory/dmabuf ✓）
   · 最大到达间隔 上千 ms ⇒ 到达本身一阵一阵（是**上游**问题：souphttpsrc/mpp 取流节奏 ✗，
     不是 GUI 的锅 ✓）⇒ 那时该改的是"按块喂"的节奏（或给 sink 加 `sync=true`/队列 ✓）

**收口清单（一旦达标就做，别再拖 ✓）**：
1. `video_panel.cpp` 打开默认：`const bool gstEnabled = qEnvironmentVariable("DSH_GST_VIDEO") != QLatin1String("0");`
   （只改这一行 ✓，注释里已写好理由 ✓）
2. 板端删掉 `zz-gstvideo.conf`（不再需要显式开关 ✓）＋ 重跑一次确认 `vqueue:src = 0` ✓
3. 本文档补 **最终 fps/CPU 数字**＋**用户目视结论** ✓（并把 §7.6 的旧结论标注为"已被 §7.8 更正" ✓ 已标 ✓）
4. 提交（只 `git add` 改动文件 ✗ 禁止 `git add -A` ✓）＋ `push` ＋ `gh run list` ✓

---

## 7.9 ★★★ 第 75–79 轮（2026-10-07 上板实测）：从"能画但节奏乱"到"稳定节拍"

### 7.9.1 第 75 轮：**驱动方式确认 1:1，但"只画出 2.8 帧/s"**（同一 65 秒窗口）

```
paintGL = 心跳号            ⇒ 定时器驱动是 **1:1（40 次/s）** ✓✓（驱动这一层修对了 ✓）
回调 = 存槽 = 20/s          ⇒ 帧确实到、确实存 ✓
已画帧 = 2.8/s ✗ ｜ 早退(无帧)=37/s ✗   ⇒ 40 拍里 37 拍"缓冲是空的" ✗
★ 但每 120 帧的到达打点里「槽内旧帧= true」⇒ 帧**挤在一起**（一波里只留下最后一帧 ✗）
★ `最大到达间隔 ≈600ms` ⇒ 上游按块灌 ⇒ **画帧间隔忽长忽短 = 用户说的"卡/倍速"** ✓
```

### 7.9.2 改法与实测（第 75–79 轮，逐条都上板验过）

| # | 改动 | 板端实测 |
|---|---|---|
| 1 | `fakesink`：`sync=false` ⇒ **`sync=true`**（让 sink 按实时时钟出帧 ✓） | `最大到达间隔 35~42ms` ✓；`已画帧` **24.3/s**（4 秒 1107→1204 ✓）⇒ **≥20fps 达标** ✓ |
| 2 | 单槽 ⇒ **帧缓冲（队列 8）+ 丢最旧 + 只取最新一帧**（**用户要求"加帧缓冲稳定帧数"** ✓） | `平均画帧间隔 33.3ms`、`最大 51ms`、缓冲深度 0 ✓✓ |
| 3 | 固定**画帧节拍**（心跳 10ms ＋ 33ms 画帧门 ✓） | `平均 39.6~40.5ms`（≈25fps）、`最大 47~64ms` ✓ ⇒ 节拍稳、**不再倍速** ✓ |
| 4 | 换流时**丢弃 EGLImage 缓存** ✓（旧 dmabuf 已关 ⇒ 复用=死引用 ⇒ 闪旧帧/SIGSEGV ✗） | `换流：丢弃 EGLImage 缓存 N 个` ✓，闪旧帧消失 ✓ |
| 5 | 几何不再硬编码：**caps 取宽高** ✓ ＋ **内存大小反推对齐高度** ✓（修"第二个视频有绿条" ✓） | `caps 尺寸 = 640 x 360` ✓、`对齐高= 368` ✓ |
| 6 | **不拉伸**：按视频宽高比取最大内接矩形、居中 ✓（用户要求 ✓） | （目视项 ✓） |
| 7 | `pumpBus()` 每拍 `gst_element_get_bus` **从不 unref** ⇒ 40 引用/秒泄漏 ✗ | 改成建管线取一次、`teardown` 还回去 ✓ ⇒ **4 分钟长跑零崩溃** ✓ |
| 8 | GL 名字（纹理/程序/vbo）随**上下文重建**失效 ✗ ⇒ `initializeGL()` 里统一清零 ✓ | 上下文重建不再用失效名字 ✓ |

### 7.9.3 ★ 踩坑记录（都要记住，别再犯）

- **`"width=(int)"` 是 11 个字符** ✓ —— 我写成 `+12` ⇒ 把首位吃掉（640⇒40、360⇒60 ✓）
  ⇒ EGLImage 建不起来 ⇒ **整屏黑** ✗（已改成 `strlen(kW)` ＋ 加"合理性兜底" ✓）。
- **退出/换流时不要 `eglDestroyImageKHR`** ✗ —— 板端实测 `systemctl stop` 那一刻销毁 EGLImage 会 **SIGSEGV** ✗
  （那时 EGL/GL 上下文正在拆 ✓）⇒ 只**丢缓存引用**就够 ✓（image 由驱动在 display 销毁时回收 ✓）。
- **零拷贝模式下不要碰 `QMediaPlayer`** ✗ —— `setSource("")` 里原来的 `player_->stop()/setMedia(空)`
  会在换流瞬间拆它自己那条老管线 ✓（换流崩溃的嫌疑之一 ✓，已改成只在"确实用老路"时才动它 ✓）。

### 7.9.4 ⚠ 仍未解决（如实记录）

**换流那一下仍会偶发 SIGSEGV** ✗（实测：3 次换流里崩 1 次、2 次里崩 1 次 ✓，`NRestarts` +1 ✓，
约 5 秒后 systemd 自动拉起 ✓，播放继续 ✓）。已经排除/缓解：
- bus 引用泄漏 ✓（第 7 条，4 分钟零崩溃 ✓）
- 退出时销毁 EGLImage ✓（第 4 条）
- 换流时先关 handoff（`signal-handoffs=0`）再 `set_state(NULL)` ✓
- 换流时不动 `QMediaPlayer` ✓
⇒ 剩下的嫌疑：**handoff 线程与拆管线之间的窄竞争** ✗、或 **GUI 侧别的代码**（QMediaPlayer 的错误回调 ✓、
封面/预览 ✓）。**下一步**：给 GUI 加一个**临时的 SIGSEGV backtrace handler** ✓（`execinfo.h` 的
`backtrace_symbols_fd` ✓）—— 板端崩溃日志现在**没有栈** ✗，没有栈就只能猜 ✓。

### 7.9.6 第 80 轮：换流崩溃**又找到一个真凶**（GStreamer 的异步状态切换 ✗）

```
症状：稳态 4 分钟 0 崩溃 ✓，但**每次换流后 20~40 秒**里可能崩 1 次 ✗（5 次换流崩 1~2 次 ✓）
真凶（GStreamer 的经典陷阱 ✓）：`gst_element_set_state(NULL)` **是异步的** ✗ ——
   它立刻返回（`GST_STATE_CHANGE_ASYNC` ✓），流线程还在拆 ✓，而我们**马上 unref 管线** ✗
   ⇒ 那些线程（多数阻塞在网络读里 ✓）稍后回来时踩到**已释放的内存** ✓✓
   ⇒ 崩在"几秒~几十秒之后" ✓✓✓（与实测时间差完全吻合 ✓）
修法：`set_state(NULL)` 之后 **`gst_element_get_state(..., 500ms)` 等它真的到 NULL** ✓ 再 unref ✓
   （`gst_video_widget.cpp::teardown()` ✓，超时也会打警告并继续 ✓）
另外两处加固：
   · **收帧闸门 `acceptFrames_`**（`std::atomic<bool>` ✓）：`teardown()` 一进门就关 ✓、
     `buildPipeline()` 建好才开 ✓；handoff 里**锁内二次确认** ✓ ⇒ 旧管线的迟到帧进不了新缓冲 ✗
     （否则会拿已关闭的 dmabuf 去建 EGLImage ⇒ 驱动层 SIGSEGV ✓）
   · `paintGL` 在闸门关着时**不画** ✓
效果：崩溃频率从"2~3 次换流 1 次"降到 **"5 次换流 1 次"** ✓（改善但**未根治** ✗）

为什么还是拿不到调用栈 ✗：`logs/crash/*.log` 里**没有 backtrace** ✗ ⇒
   我们的 SIGSEGV 处理器**被别人换掉了** ✗（`libmali` / GStreamer / Qt 里的某个组件 ✓）。
   本轮试过"周期性重装处理器"✓，但那个入口会把 `gui_widgets` 与 core 库的链接关系搞乱 ✗
   （测试可执行文件不链 core ✓ ⇒ `undefined reference` ✗）⇒ **已撤掉** ✓，
   只保留 `crash_log.cpp` 里的 `backtrace_symbols_fd` ✓（万一处理器跑到，就能看到栈 ✓）。
下一步候选（按性价比排序 ✓）：
   ① 开 core dump（服务加 `LimitCORE=infinity` ＋ `kernel.core_pattern` 落 `/data` ✓）⇒ 用 gdb 看栈；
   ② 把"换流"从"拆管线 + 重建"改成**复用同一条管线、只换 source 的 location** ✓（少一次拆装 ✓）；
   ③ 给 handoff 回调加"管线代次号"✓，代次不符直接丢 ✓（把闸门做成无窗口期的 ✓）。
```



```
绘制节拍   平均 39.6~40.5ms ／ 最大 47~64ms ✓（≈25fps ✓ ≥20 ✓；固定节拍 ⇒ 不再倍速 ✓）
绘制帧率   24.3~25.3 次/s（板端实测 60~240 秒窗口 ✓）
零拷贝帧   == 已画帧 ✓（每一帧都是 dmabuf 直导 ✓）
到达       30/s 左右，`最大到达间隔 35~42ms` ✓（sync=true 起效 ✓）
丢弃       ~4/s（上游略快于显示 ⇒ 主动丢最旧 ✓ 内容**不加速** ✓）
CPU        agent_gui ~14~23%、mali-cmar-backe ~5~7%、`vqueue = 0` ✓（原版 112.7% ⇒ 仍快 5 倍 ✓）
崩溃       稳态 4 分钟 0 次 ✓；换流 **5 次里 1 次** ✗（第 80 轮已从"2~3 次 1 次"改善 ✓，见 §7.9.6）
几何       `caps 尺寸` 逐流解析 ✓（实测过 640x360 / 640x320 / 640x272 三种 ✓，`对齐高` 自动跟随 ✓）
不拉伸     按视频宽高比取最大内接矩形、居中 ✓（用户要求 ✓；目视待确认 ✓）
```










