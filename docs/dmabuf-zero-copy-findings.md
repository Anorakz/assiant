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



