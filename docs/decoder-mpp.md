# 视频解码：为什么是 MPP，以及踩过的坑

本文记录 RK3568 上视频硬解的选型结论、MPP 的接入方式，以及实现时**实际踩到的
六个坑**（每个都带症状）。改动集中在 `native/decoder.h`、`native/decoder.cpp`、
`native/decoder_mpp.cpp`、`native/decoder_backend.h`。

---

## 1. 结论

板端视频硬解走 **Rockchip MPP**：

| 项 | 板端实测值 |
| --- | --- |
| 内核驱动 | `/dev/mpp_service`、`/proc/mpp_service/rkvdec0`，`supports-device` 里有 `RKVDEC HW_ID:0x032a3f03` |
| 用户态库 | `/lib/aarch64-linux-gnu/librockchip_mpp.so.1`（→ `.so.0`，git 提交到 2022-10） |
| 头文件 | `/usr/include/rockchip/{rk_mpi,mpp_frame,mpp_buffer,mpp_packet,rk_type,rk_vdec_cfg}.h` |
| pkgconfig | `/usr/lib/aarch64-linux-gnu/pkgconfig/rockchip_mpp.pc`（Version 1.3.8） |
| 交叉 sysroot | **已包含以上全部**，不需要额外补依赖 |

---

## 2. 为什么不是 FFmpeg + V4L2 M2M

原实现（`decoder.cpp` 的旧版）走的是 FFmpeg 4.2.7 的 `hevc_v4l2m2m` /
`h264_v4l2m2m`。**这条路在这块板子上不可能工作**：

```
$ v4l2-ctl --list-devices
rkisp-statistics (platform: rkisp):  /dev/video8 /dev/video9
rkisp_mainpath (platform:rkisp-vir0): /dev/video0 ... /dev/video7

$ ls -l /dev/video-dec0
-rw-rw---- 1 root video 4 ... /dev/video-dec0     # 注意: 4 字节的**普通文件**,
$ cat /dev/video-dec0                             # 里面写着 "dec" —— 不是设备节点
dec
```

也就是说：**板子上根本没有 V4L2 M2M 解码设备**。而

```c
avcodec_find_decoder_by_name("hevc_v4l2m2m")   // 返回非空! 因为编进库里了
```

所以旧代码判定"硬解可用"，然后 `avcodec_open2()` 因为没有设备而失败。

### 顺带修掉的那个 bug：软解回退从来没生效

旧实现用**"解码器名字存不存在"**来决定硬解/软解：

```c
av_codec = avcodec_find_decoder_by_name("hevc_v4l2m2m");
if (av_codec) hardware = true;          // ← 名字存在就当成硬解可用
...
if (avcodec_open2(...) < 0) return false;   // ← 这里失败后**不会**再试软解
```

于是 `moonlight.start()` 在 HTTP 握手**之前**就返回 `"decoder init failed"`，
而且外面完全看不出原因。

现在：候选后端**必须 `init()` 成功**才算可用，失败的把原因记下来换下一个；
全部失败时 `Decoder::last_error()` 会给出**每一层**的原因（例如
`"MPP 硬解: ...; FFmpeg 软解: ..."`）。

`h264_v4l2m2m` / `hevc_v4l2m2m` 这条路**已经整体删除**，不再保留。

---

## 3. 两个后端与回退策略

| 后端 | 什么时候用 | 说明 |
| --- | --- | --- |
| `kMpp` | 板端默认（`kAuto` 的第一个候选） | 真硬解，`is_hardware() == true`，名字 `rkmpp` |
| `kFfmpegSw` | MPP 起不来时兜底；宿主机测试 | 纯软解，名字 `ffmpeg-sw(hevc)` / `ffmpeg-sw(h264)` |

* `DecoderBackend::kAuto`（默认）：先 MPP，失败才软解，并把"为什么没用上硬解"
  记进 `last_error()`。
* `DecoderBackend::kMpp`：只用 MPP，起不来就直接失败，**不回退**。
  板端冒烟测试用这个 —— 否则"悄悄退到软解"会让测试失去意义。
* `DecoderBackend::kFfmpegSw`：只用软解，排障对比用。

> ⚠ 软解 HEVC 720p60 在 4×A55 上跑不动实时。它的价值是"不崩 + 报清楚原因"，
> 不是保证流畅。

---

## 4. 编码格式按协商结果来（不再写死 H.265）

旧代码在握手之前写死：

```c
sc.supportedVideoFormats = VIDEO_FORMAT_H265;      // 只报 H.265
decoder.init(width, height, VideoCodec::kH265);    // 握手前就按 H.265 初始化
```

服务端只给 H.264 时就会拿 H.265 解码器去解，必然失败。

现在：

* `sc.supportedVideoFormats = VIDEO_FORMAT_H264 | VIDEO_FORMAT_H265` ——
  两种都报，让服务端挑（板端 MPP 对两种都有硬解，不挑食反而更兼容）。
* 解码器在 moonlight 的 `dr.setup(videoFormat, ...)` 回调里按**协商结果**初始化
  （`MoonlightAdapter::on_decoder_setup`）。这个回调返回非 0 会让
  `LiStartConnection` 直接失败 —— 所以**仍然是快速失败**，只是位置从"握手前"
  挪到了"协商后"，而且拿到的编码格式一定是对的。
* 10bit（HDR）和 4:4:4 明确拒绝并给出可操作提示（I420 这条路只表达 8bit）。
  映射逻辑是纯函数 `classify_video_format()`，宿主机可单测；
  它用的 `VIDEO_FORMAT_*` 掩码在交叉编译时有 `static_assert` 跟 Limelight.h 核对。

---

## 5. MPP 调用流程

```
mpp_check_support_format(MPP_CTX_DEC, coding)      // 先问支不支持
mpp_create() -> mpp_init(ctx, MPP_CTX_DEC, coding)
control(MPP_DEC_SET_PARSER_SPLIT_MODE, 1)          // mpp_init **之后**
mpp_packet_init() x2                               // 数据包 / EOS 包, 各建一次

每个 AU:  packet 复用 -> decode_put_packet -> 循环 decode_get_frame
          info_change 帧 -> 配帧缓冲组 + INFO_CHANGE_READY(不是图像)
          EOS/空帧      -> 跳过 (不是图像)
          errinfo != 0  -> 坏帧, 丢掉
          discard != 0  -> 只是"不做显示用", 内容有效, 照常使用
          正常帧         -> mpp_buffer_get_ptr -> 收拢成 I420 -> mpp_frame_deinit

收尾:  mpi->reset() -> packet_deinit -> mpp_destroy -> mpp_buffer_group_put
```

刻意**没有**设 `MPP_DEC_SET_OUTPUT_FORMAT`：rkvdec 实际只吐 NV12，硬要 I420
在有些版本上会安静地不出帧。输出格式按帧判定（`classify_mpp_format`），
NV12 / NV21 / I420 都能转。

---

## 6. 六个坑（每个都带症状，改之前先读）

这是本文最有价值的部分 —— 六个问题都是在板端实机上一个一个撞出来的。

### 6.1 `MppPacket` 不能每帧新建/销毁

MPP 会把 packet 挂在输出帧的 meta 上（`KEY_INPUT_PACKET`）还给应用，
**也就是解码器自己持有这个对象**。应用提前 `mpp_packet_deinit()` 等于把 MPP
队列里的对象释放掉。

* 症状：`decode_put_packet` 一直返回成功，但 `decode_get_frame` 永远"无帧"，
  硬件 `task_count` 一个都不涨，最后输入队列满（`MPP_ERR_BUFFER_FULL`），
  进程还可能卡在 `mpp_destroy` 里（`futex_wait`，kill -9 都杀不掉）。
* 做法：**一个数据包 + 一个 EOS 包，各建一次，到 `release()` 才销毁。**

### 6.2 `info_change` 必须回"帧缓冲组"，不能只回 ready

顺序必须是：

```c
mpp_buffer_group_get_internal(&grp, MPP_BUFFER_TYPE_ION);
mpp_buffer_group_limit_config(grp, mpp_frame_get_buf_size(frame), 24);
mpi->control(ctx, MPP_DEC_SET_EXT_BUF_GROUP, grp);      // ← 少了这句就没帧
mpi->control(ctx, MPP_DEC_SET_INFO_CHANGE_READY, NULL);
```

* 症状：只回 `INFO_CHANGE_READY` 时，info_change 帧照常出现、ack 也返回 `MPP_OK`，
  但之后**一帧图像都不会吐**。
* 依据：官方 `utils/mpi_dec_utils.c` 的默认 `MPP_DEC_BUF_HALF_INT` 模式就是
  "应用给组、MPP 分配帧"。

### 6.3 输入队列满要原地重试，不能丢包

`decode_put_packet` 返回 `MPP_ERR_BUFFER_FULL` 只表示"现在塞不进去"。
一个包 = 一个访问单元 = 一帧，丢了就是画面缺帧。

* 症状：H.264 夹具稳定少解出最后一帧（5/6）。
* 做法：睡 1ms 重试同一个包（官方样例就是死等重试）。

### 6.4 "暂时没有帧"要睡 1ms 再问

硬件解码是异步的。`decode_get_frame` 在帧没准备好时返回 `MPP_OK` + 空指针，
**问一次就放弃基本等于永远拿不到帧**。

* 症状：喂完 6 个 AU 只解出 0~2 帧，且每次跑结果还不一样。
* 做法：空轮询时睡 1ms 再问（正常路径上限 10ms，flush 时 100ms）。

### 6.5 `discard` 不等于坏帧

MPP 会给某些帧打 `discard` 标志，意思只是"这一帧不拿去做显示用"，
`errinfo` 仍然是 0、像素内容是对的。官方样例对 `discard` 也只写日志、照样输出。

* 症状：把 `discard` 当坏帧丢掉 → 又少一帧。
* 另外：**流结束时 MPP 会给一个 `width=height=0`、没有 buffer 的"帧"**，
  它只是结束标记，也要跳过（否则会报假的"MPP 帧没有 buffer"）。

### 6.6 EOS 必须跟着最后一段码流一起送

Annex-B 流里，解析器要知道"这个 NAL 到此结束"，靠的是**下一个起始码**。
最后一个访问单元后面什么都没有，只送一个**空**的 EOS 包时，解析器无法判定它
已经结束 —— 最后一帧永远出不来。

* 症状：H.265 夹具恰好少 1 帧（5/6），而官方 `mpi_dec_test` 能解出全 6 帧。
* 依据：官方工具把整段流当一个**带 EOS 标志**的包送出去。
* 做法：`send_eos()` 把最后喂进去的那段数据重新挂到 EOS 包上。

### 6.7 收尾顺序：先 `reset()` 再 `packet_deinit`

```c
mpi->reset(ctx);            // ← 先把这个, 让 MPP 丢掉队列里对 packet 的引用
mpp_packet_deinit(&packet);
mpp_destroy(ctx);
mpp_buffer_group_put(grp);  // 必须 destroy 之后
```

* 症状：反过来写时，进程在退出阶段**段错误**（解码线程踩到已经释放的 packet）。

---

## 7. 接口变化

```cpp
enum class DecoderBackend { kAuto, kMpp, kFfmpegSw };

bool init(int w, int h, VideoCodec codec,
          DecoderBackend backend = DecoderBackend::kAuto);   // 后端可选
DecoderBackend active_backend() const;
const char*    last_error() const;      // 新增: 失败原因(可读), 不再只有一句 "init failed"

// flush 现在和 decode 一样能给出帧指针
bool flush(std::uint8_t** yuv_out = nullptr, int* w = nullptr, int* h = nullptr);

// 新增纯函数: 两个后端共用的"收拢 stride + 摆平平面顺序"
void collapse_yuv420_to_i420(const uint8_t* y, const uint8_t* u, const uint8_t* v,
                             int y_stride, int uv_stride, YuvLayout layout,
                             bool uv_swapped, int width, int height, uint8_t* i420_out);
```

**对外契约不变**：`decode()` / `flush()` 交出的是**连续 I420、stride == width**，
指针只在"下一次 `decode()`/`flush()`/`release()` 之前"有效。
下游 `preprocess_frame()` 及之后完全没动。

> ⚠ `flush()` 会给出**新的**借用指针 —— 用它，别用 `decode()` 那一轮的旧指针。
> 板端冒烟测试第一版就是拿旧指针去读，直接段错误。

---

## 8. 测试

### 8.1 宿主机（不需要 MPP）

`scripts/test-host.ps1`（ctest）覆盖：

* `collapse_yuv420_to_i420`：planar/semi-planar、U/V 互换（NV21 / YV12）、
  stride 收拢、奇数宽高拒绝、stride 小于宽拒绝、参数非法不写内存。
* `Decoder` 契约：非法入参、没初始化、失败后出参清空、
  三种 `DecoderBackend` 在"没编进来"时的失败原因、`flush` 的出参契约。
* `classify_video_format` / `on_decoder_setup`：H.264/H.265 认，10bit/4:4:4/AV1 拒。

### 8.2 板端离线冒烟（真硬解）

```sh
# 1) 生成夹具 (WSL / Linux, 需要 ffmpeg)
python3 scripts/gen-decoder-testdata.py

# 2) 交叉编译
scripts/build.ps1            # 产出 build-rk3568/tests/board/mpp_decode_smoke

# 3) 推到板端并跑
scp build-rk3568/tests/board/mpp_decode_smoke \
    build-rk3568/../tests/data/color_*  root@<板端>:/home/kickpi/myproject/assitant/tests/...
ssh root@<板端> "cd /home/kickpi/myproject/assitant && \
    ./tests/board/mpp_decode_smoke tests/data/color_1280x720_8bit.h265"
```

夹具（`tests/data/`，由 `scripts/gen-decoder-testdata.py` 生成，共几 KB）：

| 文件 | 验什么 |
| --- | --- |
| `color_1280x720_8bit.h265` | 生产分辨率，H.265 |
| `color_1280x720_8bit.h264` | 同一画面 H.264，验"按协商格式初始化" |
| `color_1272x720_8bit.h265` | 宽度**故意不对齐**（1272 不是 16/64 倍数）→ MPP 的 `hor_stride` 是 1280 > 1272，专门验收拢 stride |

每个码流旁边有 `.expect`（`width height frames` + 逐帧 `y u v`）。
帧序是 **红 绿 蓝 红 绿 蓝**：红和蓝的 U/V 正好相反，所以 U/V 搞反、平面顺序错、
stride 收拢错都逃不掉；每帧是纯色，所以平面内部必须所有像素相等。

冒烟测试做三件事：

1. 用 `DecoderBackend::kMpp` **显式**初始化，并检查 `is_hardware()` ——
   不允许悄悄退到软解然后"测试通过"。
2. 逐帧核对尺寸 + 平面均匀性 + Y/U/V 值（±2）。
3. 自己拆 Annex-B 访问单元（一个 VCL NAL 起一个新 AU），按生产路径一包一帧地喂。

排障开关：`AGENT_MPP_VERBOSE=1` 会把每次 MPP 调用的返回值打到 stderr；
`mpp_decode_smoke -v` 会逐个 AU 打印状态。

### 8.3 实测结果（板端）

```
color_1280x720_8bit.h265   6/6 帧, 颜色 R G B R G B 全对   PASS
color_1272x720_8bit.h265   6/6 帧, 同上                     PASS
color_1280x720_8bit.h264   6/6 帧, 同上                     PASS
```

连跑 3 轮结果一致、无段错误。

### 8.4 还没验到的部分

`MoonlightAdapter::on_decoder_setup()` 的**板上**行为还没跑到 ——
板端 `creds/` 不存在且 `pair_status=0`（Sunshine 那边还没配对），
`/launch` 直接失败，连接建立不起来，所以走不到协商那一步。
这一段目前由宿主机单测覆盖（`classify_video_format` + `on_decoder_setup`
的返回值与错误信息），**配对完成后应当在板上复跑一次实流验证**。

---

## 9. 不支持的东西

| 项 | 现状 |
| --- | --- |
| 10bit / HDR（`MPP_FMT_YUV420SP_10BIT`） | 明确拒绝，报 `kUnsupportedFormat`。需要另开 P010 → 10bit 通路，或在 Sunshine 侧钉 8bit |
| 4:4:4 | 明确拒绝（映射阶段就拦掉） |
| AV1 | 明确拒绝（当前只支持 H.264 / H.265） |
| 多 slice 的访问单元 | 板端冒烟测试的 AU 拆分假设"一帧一个 slice"（x264/x265 默认如此）。生产路径由 moonlight 直接给 AU，不受影响 |

---

## 10. 已知的后续优化（本期不做）

* **省掉收拢拷贝**：现在是 MPP NV12 → 我们自己的连续 I420（720p60 约 83 MB/s
  内存带宽）。下一步可以让 `preprocess_frame()` 直接接受 stride，在 MPP 缓冲上
  做 ROI 缩放，省掉这次拷贝。
* **帧缓冲外部分配**：现在用 `MPP_DEC_BUF_HALF_INT`（MPP 分配、应用给组）。
  改成 external 模式可以对接 DRM/dmabuf，配合零拷贝。
