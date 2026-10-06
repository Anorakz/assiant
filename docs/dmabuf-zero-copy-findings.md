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
