// ============================================================================
//  decoder_mpp.cpp — MPP (Rockchip Media Process Platform) 硬解后端
//
//  为什么是 MPP (板端实测, 见 docs/decoder-mpp.md)
//  ---------------------------------------------------------------------------
//      /dev/mpp_service            +  /proc/mpp_service/rkvdec0
//      librockchip_mpp             +  /usr/include/rockchip/*.h
//
//  板子上**没有** V4L2 M2M 解码设备 (`v4l2-ctl --list-devices` 只有 rkisp 摄像头;
//  /dev/video-dec0 是个写着 "dec" 的普通文件), 所以 FFmpeg 的 h264_v4l2m2m /
//  hevc_v4l2m2m 一 open 就失败 —— MPP 是唯一能真正吃上硬解的通路。
//
//  解码流程 (照 RK 官方 test/mpi_dec_test.c 的 simple 模式写)
//  ---------------------------------------------------------------------------
//      mpp_check_support_format()   先问 MPP 支不支持这种编码
//      mpp_create() + mpp_init()    MPP_CTX_DEC + HEVC/AVC
//      control(SPLIT_MODE, 1)       让 MPP 自己切 NAL (容忍带内 SPS/PPS)
//      mpp_packet_init() 两次       一个装数据, 一个装 EOS —— 都只建一次
//
//      每个包:  packet 复用 -> decode_put_packet -> 循环 decode_get_frame
//               info_change 帧 -> 配帧缓冲组 + INFO_CHANGE_READY, 不是图像
//               errinfo != 0   -> 坏帧, 丢掉
//               discard != 0   -> 只是"不做显示用", 内容有效, **照常使用**
//               正常帧          -> mpp_buffer_get_ptr -> 收拢成 I420
//     收尾: mpi->reset() -> packet_deinit -> mpp_destroy() -> buffer_group_put()
//
//  六个必须小心的地方 (前六个都是板上真踩过的坑, 症状都写在旁边)
//  ---------------------------------------------------------------------------
//  1) **MppPacket 只能建一次, 反复复用, 到 release 才销毁。**
//     绝对不要"每帧 mpp_packet_init / mpp_packet_deinit"。
//     MPP 会把 packet 挂在输出帧的 meta 上 (KEY_INPUT_PACKET) 还给应用, 也就是
//     说解码器**自己持有这个对象**; 应用提前 deinit 等于把 MPP 队列里的对象释放掉。
//     症状: put 一直成功、get 永远"无帧"、硬件一个 task 都不接, 最后输入队列满。
//
//  2) **info_change 帧必须回"帧缓冲组"再回 ready。**
//     只调 MPP_DEC_SET_INFO_CHANGE_READY 是不够的, 顺序必须是:
//         mpp_buffer_group_get_internal(ION) -> limit_config(buf_size, 24)
//         -> control(MPP_DEC_SET_EXT_BUF_GROUP, group)
//         -> control(MPP_DEC_SET_INFO_CHANGE_READY, NULL)
//     少了 SET_EXT_BUF_GROUP 那一句, 症状同样是"永远没有帧"。
//     (默认的 half-internal 模式: 帧缓冲由 MPP 分配, 但组要应用给。)
//
//  3) **输入队列满不是错误, 要在原地等 1ms 重试同一个包, 不能丢。**
//     decode_put_packet 返回 MPP_ERR_BUFFER_FULL 只表示"现在塞不进去"。
//     一个包 = 一个访问单元 = 一帧, 丢了就是画面缺帧 (H.264 夹具因此少过一帧)。
//
//  4) **"暂时没有帧"要睡 1ms 再问, 不能问一次就放弃。**
//     硬件解码是异步的; 官方样例在 EOS 之后也是一直 sleep+continue 等到 EOS 帧。
//     问一次就返回的话, 大部分帧都会被漏掉。
//
//  5) **discard 不等于坏帧。**
//     MPP 会给某些帧 (例如最后一帧) 打 discard 标志, 意思只是"不做显示用",
//     errinfo 仍是 0、像素内容是对的。官方样例对 discard 也只写日志、照样输出。
//     当坏帧丢掉会少帧。
//
//  6) **输入/输出缓冲的生命周期**
//     输入: moonlight 的码流指针在 on_video_frame() 返回后就失效, 而 MPP 可能到
//           下一轮才读完 —— 所以先拷进自己的 input_ 再交给 MPP。
//     输出: MPP 帧池很小, 解出一帧就立刻收拢进我们自己的 I420 缓冲, 然后马上
//           mpp_frame_deinit。对外借出的指针因此仍然"下一次 decode()/flush()
//           之前有效"。
//     收尾顺序也要照官方样例: **先 mpi->reset() 再 deinit packet** —— 反过来的话
//     MPP 内部队列里还可能压着我们的 packet, 释放它会让解码线程踩野指针 (段错误)。
//
//  平台
//  ---------------------------------------------------------------------------
//  AGENT_HAVE_MPP 没定义时 (宿主机就是), 本文件只提供一个返回 nullptr 的工厂,
//  不 include 任何 MPP 头文件 —— 这样宿主机不装 MPP 也能编、能跑单测。
// ============================================================================

#include "decoder_backend.h"

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <deque>
#include <string>
#include <vector>

#ifdef AGENT_HAVE_MPP
#include <unistd.h>  // usleep: 输入队列满 / 等帧时的退避

extern "C" {
#include <rockchip/mpp_buffer.h>
#include <rockchip/mpp_err.h>
#include <rockchip/mpp_frame.h>
#include <rockchip/mpp_packet.h>
#include <rockchip/rk_mpi.h>
#include <rockchip/rk_type.h>
}
#endif

namespace agent {
namespace detail {

#ifdef AGENT_HAVE_MPP

namespace {

/// 一次 decode() 里最多收多少帧; 纯粹是防"MPP 抽风一直返帧"把循环卡死
constexpr int kMaxFramesPerCall = 64;

/// 输入队列满时重试几次 (每次睡 1ms)。官方样例是不限次数地睡 1ms 重试,
/// 这里给个上限, 免得解码器真出问题时把 moonlight 的视频线程拖死太久。
constexpr int kPutRetryMax = 30;

/// decode_get_frame 返回 MPP_ERR_TIMEOUT 时重试几次 (每次 1ms)
constexpr int kGetRetryMax = 30;

/// "暂时没有帧"时最多空轮询几次 (每次睡 1ms)。见文件头第 4 条。
/// 正常路径 10ms: 解码跟得上时 1~2ms 就拿到帧; 真拿不到就先返回, 下一帧再问
/// (帧不会丢, 它在 MPP 的输出队列里)。
constexpr int kEmptyPollMax = 10;

/// flush 时要更耐心: 目的就是把流水线榨干
constexpr int kEmptyPollMaxFlush = 100;

/// put 重试期间只顺手把现成的帧拿走, 不等 (等会把重试节奏拖坏)
constexpr int kEmptyPollMaxNoWait = 0;

/// 帧缓冲的块数 (照官方样例的 24)。太少会卡解码, 太多白占内存。
constexpr RK_S32 kFrameBufferCount = 24;

/// 待交出帧的队列上限。真到上限说明上层很久没来取, 丢最旧的并记账,
/// 不能让内存无限涨 (一帧 720p I420 就是 1.3MB)。
constexpr std::size_t kMaxPendingFrames = 8;

/// 输入队列上限 (访问单元个数)。
///
/// 为什么需要这个队列: 接口是"一次 decode() 收一个 AU, 返回一帧", 但**解码器
/// 常常领先于消费者** (一次 collect 可能吐好几帧)。如果这时直接返回积压的帧、
/// 不把新来的 AU 喂进去, 那个 AU 就永远丢了 —— 板端 H.264 夹具因此少一帧。
/// 所以: 新 AU 一律先入队, 再尽量喂给 MPP, 最后才交帧。
constexpr std::size_t kMaxInputQueue = 8;

/// 已经喂给 MPP 但还要多留几轮的缓冲。
/// MPP 可能到下一轮才真正读完那块数据, 立刻释放就是悬挂指针。
/// MPP 的输入队列深度约 4, 留 4 个足够。
constexpr std::size_t kMaxInflightInputs = 4;

/// 一次 decode() 里为了把输入队列喂空, 最多等多少毫秒 (队列满时)
constexpr int kPumpWaitMs = 20;

/// 排查 MPP 时打开: AGENT_MPP_VERBOSE=1 会把每次 MPP 调用的返回值打到 stderr。
/// 硬解"一帧都不出"这类问题上, 光看 status() 是不够的 —— 得知道是 put 失败了,
/// 还是 info_change 没回、还是 get 一直空。
bool trace_enabled() {
    static const bool on = []() {
        const char* v = std::getenv("AGENT_MPP_VERBOSE");
        return v != nullptr && v[0] != '\0' && v[0] != '0';
    }();
    return on;
}

void trace(const std::string& msg) {
    if (trace_enabled()) {
        std::fprintf(stderr, "[mpp] %s\n", msg.c_str());
    }
}

const char* coding_name(MppCodingType coding) {
    switch (coding) {
        case MPP_VIDEO_CodingAVC:
            return "H.264";
        case MPP_VIDEO_CodingHEVC:
            return "H.265";
        default:
            return "unknown";
    }
}

/// MPP 错误码 → 可读文本 (排障时比一个 -1012 有用得多)
std::string mpp_err_text(MPP_RET ret) {
    switch (ret) {
        case MPP_OK:
            return "MPP_OK";
        case MPP_NOK:
            return "MPP_NOK (通用失败)";
        case MPP_ERR_UNKNOW:
            return "MPP_ERR_UNKNOW";
        case MPP_ERR_NULL_PTR:
            return "MPP_ERR_NULL_PTR";
        case MPP_ERR_MALLOC:
            return "MPP_ERR_MALLOC";
        case MPP_ERR_VALUE:
            return "MPP_ERR_VALUE";
        case MPP_ERR_TIMEOUT:
            return "MPP_ERR_TIMEOUT";
        case MPP_ERR_INIT:
            return "MPP_ERR_INIT";
        case MPP_ERR_VPU_CODEC_INIT:
            return "MPP_ERR_VPU_CODEC_INIT";
        case MPP_ERR_STREAM:
            return "MPP_ERR_STREAM";
        case MPP_ERR_NOMEM:
            return "MPP_ERR_NOMEM";
        case MPP_ERR_PROTOL:
            return "MPP_ERR_PROTOL";
        case MPP_ERR_VPUHW:
            return "MPP_ERR_VPUHW";
        case MPP_ERR_BUFFER_FULL:
            return "MPP_ERR_BUFFER_FULL";
        case MPP_ERR_DISPLAY_FULL:
            return "MPP_ERR_DISPLAY_FULL";
        default:
            return "MPP 错误码 " + std::to_string(static_cast<int>(ret));
    }
}

/// MPP 输出格式 → 我们的平面布局; 不支持时 supported=false
struct FmtPlan {
    bool supported = false;
    YuvLayout layout = YuvLayout::kPlanar;
    bool uv_swapped = false;
    const char* label = "unknown";
};

FmtPlan classify_mpp_format(MppFrameFormat fmt) {
    switch (fmt) {
        case MPP_FMT_YUV420SP:  // YYYY... UVUV...  (NV12)
            return {true, YuvLayout::kSemiPlanar, false, "NV12"};
        case MPP_FMT_YUV420SP_VU:  // YYYY... VUVU...  (NV21)
            return {true, YuvLayout::kSemiPlanar, true, "NV21"};
        case MPP_FMT_YUV420P:  // YYYY... U...V...  (I420)
            return {true, YuvLayout::kPlanar, false, "I420"};
        // 10bit (HDR) 以及其它格式: 明确拒绝, 不猜布局。
        // I420 这条路只表达 8bit; 真要支持 HDR 得另开 P010 → 10bit 通路。
        default:
            return {false, YuvLayout::kPlanar, false, "非 8bit YUV420"};
    }
}

}  // namespace

class MppBackend : public VideoDecoderBackend {
public:
    ~MppBackend() override { release(); }

    bool init(int width, int height, VideoCodec codec, std::string* error) override {
        release();

        coding_ = (codec == VideoCodec::kH265) ? MPP_VIDEO_CodingHEVC : MPP_VIDEO_CodingAVC;

        // 先问一句支不支持 —— 不然后面只会拿到一个看不懂的错误码
        if (mpp_check_support_format(MPP_CTX_DEC, coding_) != MPP_OK) {
            *error = std::string("MPP 不支持该编码格式 (") + coding_name(coding_) +
                     "); 板端 MPP 或内核缺少对应的解码器";
            return false;
        }

        MPP_RET ret = mpp_create(&ctx_, &mpi_);
        if (ret != MPP_OK) {
            *error = "mpp_create 失败: " + mpp_err_text(ret) +
                     " (通常是 /dev/mpp_service 打不开或权限不够)";
            return false;
        }
        if (ctx_ == nullptr || mpi_ == nullptr) {
            *error = "mpp_create 返回成功但 ctx/mpi 为空";
            release();
            return false;
        }

        ret = mpp_init(ctx_, MPP_CTX_DEC, coding_);
        if (ret != MPP_OK) {
            *error = std::string("mpp_init(") + coding_name(coding_) +
                     ") 失败: " + mpp_err_text(ret);
            release();
            return false;
        }

        // ⚠ 顺序照抄 RK 官方样例 (mpi_dec_test.c): mpp_create -> mpp_init ->
        //   control(SPLIT_MODE)。头文件里 "Need to setup before init" 那句注释
        //   是挂在下一行 MPP_DEC_SET_PARSER_FAST_MODE 上的, 容易看串。
        //   moonlight 一次给一个 AU, 开 split 让 MPP 自己切 NAL 更稳 (容忍带内
        //   SPS/PPS 和 AUD)。失败不致命: 那就退化成"一个包一帧"。
        RK_S32 need_split = 1;
        const MPP_RET split_ret =
            mpi_->control(ctx_, MPP_DEC_SET_PARSER_SPLIT_MODE, &need_split);
        trace("control(SPLIT_MODE, 1) -> " + mpp_err_text(split_ret));

        // 刻意**不**设 MPP_DEC_SET_OUTPUT_FORMAT:
        //   rkvdec 实际上只会吐 NV12, 硬要 I420 有些版本会安静地不输出。
        //   输出格式本来就是按帧判定的 (classify_mpp_format), NV12 照样能转。

        // 两个 packet 各建一次, 之后一直复用 (见文件头第 1 条):
        //   data_packet_ 装码流; eos_packet_ 只在 flush 时用, 所以可以直接带
        //   EOS 标志 (没有公开的 clr_flag, 用两个对象最干净)
        ret = mpp_packet_init(&data_packet_, nullptr, 0);
        if (ret != MPP_OK || data_packet_ == nullptr) {
            *error = "mpp_packet_init(数据包) 失败: " + mpp_err_text(ret);
            release();
            return false;
        }
        ret = mpp_packet_init(&eos_packet_, nullptr, 0);
        if (ret != MPP_OK || eos_packet_ == nullptr) {
            *error = "mpp_packet_init(EOS 包) 失败: " + mpp_err_text(ret);
            release();
            return false;
        }
        mpp_packet_set_eos(eos_packet_);

        width_ = width;
        height_ = height;
        name_ = "rkmpp";
        status_ = DecodeStatus::kOk;
        trace("init 完成: coding=" + std::string(coding_name(coding_)) + " " +
              std::to_string(width) + "x" + std::to_string(height));
        return true;
    }

    bool decode(const std::uint8_t* data,
                std::size_t size,
                std::uint8_t** yuv_out,
                int* w,
                int* h) override {
        if (ctx_ == nullptr || mpi_ == nullptr) {
            status_ = DecodeStatus::kNotInitialized;
            return false;
        }

        const bool have_input = (data != nullptr && size > 0);

        // 1) 新码流一律先入队 —— 绝不能因为"手上还有帧没交"就把这个 AU 丢掉
        if (have_input) {
            queue_input(data, size);
        }

        // 2) 尽力把队列里的包喂进 MPP (喂不进去就留着, 下次继续)
        pump_input(kPumpWaitMs);

        // 3) 输入队列空了而且是"没有新输入"的调用 -> 该送 EOS 了 (flush 场景)
        if (!have_input && in_queue_.empty() && !eos_sent_) {
            eos_sent_ = true;
            if (!send_eos()) {
                trace("EOS 包没送进去, 继续取帧");
            }
        }

        // 4) 收帧。EOS 送过之后多等一会儿, 目的是把流水线榨干。
        collect_frames(eos_sent_ ? kEmptyPollMaxFlush : kEmptyPollMax);

        if (pending_.empty()) {
            // 还没吐帧 —— 硬解流水线延迟, 正常现象
            if (status_ == DecodeStatus::kOk) {
                status_ = DecodeStatus::kNeedMoreInput;
            }
            return false;
        }
        return pop_pending(yuv_out, w, h);
    }

    bool flush(std::uint8_t** yuv_out, int* w, int* h) override {
        if (ctx_ == nullptr) {
            status_ = DecodeStatus::kNotInitialized;
            return false;
        }
        return decode(nullptr, 0, yuv_out, w, h);
    }

    void release() override {
        // ⚠ 顺序很重要, 抄官方样例 (test/mpi_dec_test.c 的 MPP_TEST_OUT 段):
        //     reset()  ->  packet_deinit  ->  destroy  ->  buffer_group_put
        //   必须**先 reset**: 解码器内部队列里很可能还压着我们喂进去的 packet,
        //   先 deinit packet 等于把 MPP 还在引用的对象释放掉 —— 它的解码线程随后
        //   踩到野指针就是段错误 (板上就是这么崩的)。
        if (ctx_ != nullptr && mpi_ != nullptr) {
            mpi_->reset(ctx_);
        }
        if (data_packet_ != nullptr) {
            mpp_packet_deinit(&data_packet_);
        }
        if (eos_packet_ != nullptr) {
            mpp_packet_deinit(&eos_packet_);
        }
        if (ctx_ != nullptr) {
            mpp_destroy(ctx_);
        }
        ctx_ = nullptr;
        mpi_ = nullptr;
        if (frm_group_ != nullptr) {
            // 必须 mpp_destroy 之后再放: 解码器还引用着这个组
            mpp_buffer_group_put(frm_group_);
            frm_group_ = nullptr;
        }
        in_queue_.clear();
        inflight_.clear();
        pending_.clear();
        current_ = QueuedFrame{};
        out_w_ = 0;
        out_h_ = 0;
        frames_ = 0;
        dropped_ = 0;
        dropped_input_ = 0;
        errinfo_seen_ = 0;
        discard_seen_ = 0;
        eos_sent_ = false;
        name_ = nullptr;
        error_.clear();
        status_ = DecodeStatus::kNotInitialized;
    }

    const char* name() const override { return name_ != nullptr ? name_ : "rkmpp"; }

    DecoderBackend kind() const override { return DecoderBackend::kMpp; }

    bool hardware() const override { return true; }

    DecodeStatus status() const override { return status_; }

    std::size_t frames() const override { return frames_; }

    const char* last_error() const override { return error_.c_str(); }

private:
    /// 一帧收拢好的 I420 (+ 它自己的尺寸: 队列里可能混着不同尺寸的帧)
    struct QueuedFrame {
        std::vector<std::uint8_t> i420;
        int width = 0;
        int height = 0;
    };

    /// 把码流拷进输入队列 (拷一份: moonlight 的指针出了 on_video_frame 就失效)
    void queue_input(const std::uint8_t* data, std::size_t size) {
        if (in_queue_.size() >= kMaxInputQueue) {
            // 上层喂得比解码快太多 —— 丢最旧的并记账 (丢新的更糟: 可能丢掉关键帧)
            in_queue_.pop_front();
            ++dropped_input_;
            trace("输入队列满, 丢掉最旧的 AU");
        }
        in_queue_.emplace_back(data, data + size);
    }

    /// 尽力把输入队列里的包喂给 MPP。
    /// 队列满 (MPP_ERR_BUFFER_FULL) 时退避重试, 但最多等 max_wait_ms —— 剩下的
    /// 留到下一次 decode() 再喂。**绝不丢包**。
    void pump_input(int max_wait_ms) {
        int waited = 0;
        while (!in_queue_.empty()) {
            std::vector<std::uint8_t>& au = in_queue_.front();
            mpp_packet_set_data(data_packet_, au.data());
            mpp_packet_set_size(data_packet_, au.size());
            mpp_packet_set_pos(data_packet_, au.data());
            mpp_packet_set_length(data_packet_, au.size());

            const MPP_RET ret = mpi_->decode_put_packet(ctx_, data_packet_);
            if (ret == MPP_OK) {
                // 收下了。但数据还得再留几轮: MPP 可能到下一轮才读完这块内存。
                inflight_.push_back(std::move(au));
                in_queue_.pop_front();
                while (inflight_.size() > kMaxInflightInputs) {
                    inflight_.pop_front();
                }
                waited = 0;
                continue;
            }
            if (ret != MPP_ERR_BUFFER_FULL) {
                error_ = "decode_put_packet 失败: " + mpp_err_text(ret);
                status_ = DecodeStatus::kDecodeError;
                trace(error_);
                in_queue_.clear();
                return;
            }

            // 输入队列满: 先把解好的帧收走给解码器腾地方, 睡 1ms 再试
            if (waited >= max_wait_ms) {
                trace("输入队列满, 剩下的 " + std::to_string(in_queue_.size()) +
                      " 个包下次再喂");
                return;
            }
            collect_frames(kEmptyPollMaxNoWait);
            usleep(1000);
            ++waited;
        }
    }

    /// 送 EOS。
    ///
    /// ⚠ **EOS 必须跟着最后一段码流一起送, 不能送一个空包。**
    /// 原因: Annex-B 基本流里, 解析器要知道"这个 NAL 到此结束", 靠的是**下一个
    /// 起始码**。最后一个访问单元后面什么都没有, 只送一个空 EOS 包的话, 解析器
    /// 无法判定它已经结束 —— 表现就是最后一帧永远出不来 (H.265 夹具恰好少 1 帧,
    /// 而官方 mpi_dec_test 把整段流当一个带 EOS 的包送出去, 所以能解出全 6 帧)。
    /// 所以这里把最后喂进去的那段数据再挂到 EOS 包上 (EOS 标志在建包时就设过了)。
    bool send_eos() {
        if (!inflight_.empty()) {
            const std::vector<std::uint8_t>& last = inflight_.back();
            mpp_packet_set_data(eos_packet_, const_cast<std::uint8_t*>(last.data()));
            mpp_packet_set_size(eos_packet_, last.size());
            mpp_packet_set_pos(eos_packet_, const_cast<std::uint8_t*>(last.data()));
            mpp_packet_set_length(eos_packet_, last.size());
            trace("EOS 跟着最后 " + std::to_string(last.size()) + " 字节码流一起送");
        } else {
            mpp_packet_set_data(eos_packet_, nullptr);
            mpp_packet_set_size(eos_packet_, 0);
            mpp_packet_set_pos(eos_packet_, nullptr);
            mpp_packet_set_length(eos_packet_, 0);
            trace("EOS 空包送 (还没有喂过数据)");
        }
        return put_with_retry(eos_packet_, "EOS 包");
    }

    /// 把 packet 塞进解码器; 队列满时退避重试**同一个包**, 绝不丢。 (EOS 包用)
    bool put_with_retry(MppPacket packet, const char* what) {
        for (int attempt = 0; attempt < kPutRetryMax; ++attempt) {
            const MPP_RET ret = mpi_->decode_put_packet(ctx_, packet);
            if (ret == MPP_OK) {
                if (attempt > 0) {
                    trace(std::string(what) + ": 重试 " + std::to_string(attempt) +
                          " 次后收下");
                }
                return true;
            }
            if (ret != MPP_ERR_BUFFER_FULL) {
                error_ = std::string("decode_put_packet(") + what + ") 失败: " +
                         mpp_err_text(ret);
                status_ = DecodeStatus::kDecodeError;
                trace(error_);
                return false;
            }

            // 输入队列满: 先把已经解出来的帧收走 (给解码器腾地方), 睡 1ms 再重试。
            // 见文件头第 3 条: 这里不能丢包走人, 丢一个 AU 就是丢一帧。
            collect_frames(kEmptyPollMaxNoWait);
            usleep(1000);
        }

        error_ = std::string("decode_put_packet(") + what + ") 重试 " +
                 std::to_string(kPutRetryMax) + " 次仍失败 (输入队列一直是满的)";
        status_ = DecodeStatus::kNeedMoreInput;
        trace(error_);
        return false;
    }

    /// 把 MPP 已经解好的帧全取出来, 收拢进 pending_
    ///
    /// @param max_empty_polls "暂时没有帧"时最多空轮询几次 (每次睡 1ms)。
    ///        硬件解码是异步的, 问一次就放弃基本等于永远拿不到帧。
    void collect_frames(int max_empty_polls) {
        int empty_polls = 0;
        int produced = 0;

        while (produced < kMaxFramesPerCall) {
            MppFrame frame = nullptr;
            MPP_RET ret = MPP_OK;

            // MPP_ERR_TIMEOUT 是"解码器一时没准备好", 官方样例也是睡 1ms 重试
            for (int attempt = 0; attempt < kGetRetryMax; ++attempt) {
                ret = mpi_->decode_get_frame(ctx_, &frame);
                if (ret != MPP_ERR_TIMEOUT) {
                    break;
                }
                usleep(1000);
            }

            // ret==MPP_OK && frame==nullptr 就是"帧还没好"
            if (ret != MPP_OK || frame == nullptr) {
                if (empty_polls >= max_empty_polls) {
                    trace("get_frame -> " + mpp_err_text(ret) + " (无帧, 已等 " +
                          std::to_string(empty_polls) + "ms, 先返回)");
                    break;
                }
                ++empty_polls;
                usleep(1000);
                continue;
            }
            empty_polls = 0;

            if (mpp_frame_get_info_change(frame) != 0) {
                handle_info_change(frame);
                mpp_frame_deinit(&frame);
                continue;  // 不是图像, 不计入 produced
            }

            // EOS 标记帧 / 空帧: MPP 在流结束时会给一个 width=height=0、没有
            // buffer 的"帧", 它只是个结束标记, 不是图像。当成图像会得到
            // "MPP 帧没有 buffer" 的假错误, 还会把帧数算错。
            if (mpp_frame_get_eos(frame) != 0 || mpp_frame_get_buffer(frame) == nullptr) {
                trace("EOS/空帧 (不是图像), 跳过");
                mpp_frame_deinit(&frame);
                continue;
            }

            const RK_U32 errinfo = mpp_frame_get_errinfo(frame);
            const RK_U32 discard = mpp_frame_get_discard(frame);

            // errinfo: 真的解坏了 -> 丢掉
            if (errinfo != 0) {
                ++errinfo_seen_;
                error_ = "MPP 报了坏帧 (errinfo=" + std::to_string(errinfo) +
                         ", discard=" + std::to_string(discard) + ")";
                trace(error_);
                mpp_frame_deinit(&frame);
                continue;
            }

            // discard: **不是错误**, 只表示"这一帧不拿去做显示用"(见文件头第 5 条)。
            //   官方样例对 discard 也只写日志, 帧照样输出。
            if (discard != 0) {
                ++discard_seen_;
                trace("帧带 discard 标志 (errinfo=0, 内容仍然有效, 照常使用)");
            }

            trace("帧: " + std::to_string(mpp_frame_get_width(frame)) + "x" +
                  std::to_string(mpp_frame_get_height(frame)) + " hor_stride=" +
                  std::to_string(mpp_frame_get_hor_stride(frame)) + " ver_stride=" +
                  std::to_string(mpp_frame_get_ver_stride(frame)) + " fmt=" +
                  std::to_string(static_cast<int>(mpp_frame_get_fmt(frame))));

            const bool ok = convert_frame(frame);
            mpp_frame_deinit(&frame);  // 帧池很小, 立刻还回去

            if (!ok) {
                // 格式不支持之类: 后面的帧也会是同样问题, 不再继续收
                return;
            }
            ++produced;
        }
    }

    /// info_change 帧: 把"帧缓冲用哪个组"这件事回完 (见文件头第 2 条)
    void handle_info_change(MppFrame frame) {
        const std::size_t buf_size = mpp_frame_get_buf_size(frame);
        const RK_U32 info_w = mpp_frame_get_width(frame);
        const RK_U32 info_h = mpp_frame_get_height(frame);
        const int info_fmt = static_cast<int>(mpp_frame_get_fmt(frame));

        std::string note;
        if (frm_group_ == nullptr) {
            const MPP_RET gr =
                mpp_buffer_group_get_internal(&frm_group_, MPP_BUFFER_TYPE_ION);
            if (gr != MPP_OK || frm_group_ == nullptr) {
                note = "buffer group 创建失败: " + mpp_err_text(gr);
                frm_group_ = nullptr;
            } else {
                const MPP_RET lr = mpp_buffer_group_limit_config(
                    frm_group_, buf_size, kFrameBufferCount);
                if (lr != MPP_OK) {
                    note = "limit_config 失败: " + mpp_err_text(lr);
                }
            }
        }
        if (frm_group_ != nullptr) {
            const MPP_RET sr = mpi_->control(ctx_, MPP_DEC_SET_EXT_BUF_GROUP, frm_group_);
            if (sr != MPP_OK) {
                note = "SET_EXT_BUF_GROUP 失败: " + mpp_err_text(sr);
            }
        }

        const MPP_RET ack = mpi_->control(ctx_, MPP_DEC_SET_INFO_CHANGE_READY, nullptr);
        trace("info_change -> " + std::to_string(info_w) + "x" + std::to_string(info_h) +
              " fmt=" + std::to_string(info_fmt) + " buf_size=" +
              std::to_string(buf_size) + " group=" + (frm_group_ ? "有" : "无") +
              " ack=" + mpp_err_text(ack) + (note.empty() ? "" : ("  [" + note + "]")));
    }

    /// 把一帧 MPP 输出收拢成 I420 放进 pending_
    bool convert_frame(MppFrame frame) {
        const MppFrameFormat fmt = mpp_frame_get_fmt(frame);
        const FmtPlan plan = classify_mpp_format(fmt);
        if (!plan.supported) {
            error_ = std::string("MPP 输出像素格式不支持 (只支持 8bit YUV420): ") +
                     plan.label + " (fmt=" + std::to_string(static_cast<int>(fmt)) + ")";
            status_ = DecodeStatus::kUnsupportedFormat;
            return false;
        }

        MppBuffer buffer = mpp_frame_get_buffer(frame);
        if (buffer == nullptr) {
            error_ = "MPP 帧没有 buffer";
            status_ = DecodeStatus::kDecodeError;
            return false;
        }
        const std::uint8_t* base =
            static_cast<const std::uint8_t*>(mpp_buffer_get_ptr(buffer));
        if (base == nullptr) {
            error_ = "mpp_buffer_get_ptr 返回空 (缓冲没映射到用户态?)";
            status_ = DecodeStatus::kDecodeError;
            return false;
        }

        // 可见尺寸用 MPP 报的, 不用 init 时猜的 (分辨率可能变过)
        int vw = static_cast<int>(mpp_frame_get_width(frame));
        int vh = static_cast<int>(mpp_frame_get_height(frame));
        if (vw <= 0 || vh <= 0) {
            vw = width_;
            vh = height_;
        }
        vw &= ~1;
        vh &= ~1;
        if (vw <= 0 || vh <= 0) {
            error_ = "MPP 报了非法尺寸";
            status_ = DecodeStatus::kDecodeError;
            return false;
        }

        // stride 通常比宽大 (16/32/64 对齐), 收拢由 collapse_yuv420_to_i420 负责
        const int y_stride = static_cast<int>(mpp_frame_get_hor_stride(frame));
        const int v_stride = static_cast<int>(mpp_frame_get_ver_stride(frame));
        if (y_stride < vw) {
            error_ = "MPP 报的 hor_stride(" + std::to_string(y_stride) + ") 比宽(" +
                     std::to_string(vw) + ")还小";
            status_ = DecodeStatus::kDecodeError;
            return false;
        }

        QueuedFrame queued;
        queued.width = vw;
        queued.height = vh;
        queued.i420.resize(i420_frame_bytes(vw, vh));

        if (plan.layout == YuvLayout::kSemiPlanar) {
            // Y 平面之后紧跟一块交织的 UV
            const std::uint8_t* uv = base + static_cast<std::size_t>(y_stride) * v_stride;
            collapse_yuv420_to_i420(base, uv, nullptr, y_stride, y_stride, plan.layout,
                                    plan.uv_swapped, vw, vh, queued.i420.data());
        } else {
            // I420: Y / U / V 三块平面, 每块的 stride 是 hor_stride (色度是 /2)
            const std::uint8_t* u = base + static_cast<std::size_t>(y_stride) * v_stride;
            const std::uint8_t* v =
                u + static_cast<std::size_t>(y_stride / 2) * (v_stride / 2);
            collapse_yuv420_to_i420(base, u, v, y_stride, y_stride / 2, plan.layout,
                                    plan.uv_swapped, vw, vh, queued.i420.data());
        }

        out_w_ = vw;
        out_h_ = vh;
        ++frames_;
        status_ = DecodeStatus::kOk;

        if (pending_.size() >= kMaxPendingFrames) {
            // 上层很久没来取帧了 —— 丢最旧的, 别让内存无限涨
            pending_.pop_front();
            ++dropped_;
        }
        pending_.push_back(std::move(queued));
        return true;
    }

    bool pop_pending(std::uint8_t** yuv_out, int* w, int* h) {
        current_ = std::move(pending_.front());
        pending_.pop_front();

        if (yuv_out != nullptr) {
            *yuv_out = current_.i420.data();
        }
        if (w != nullptr) {
            *w = current_.width;
        }
        if (h != nullptr) {
            *h = current_.height;
        }
        status_ = DecodeStatus::kOk;
        return true;
    }

    MppCtx ctx_ = nullptr;
    MppApi* mpi_ = nullptr;
    /// 装数据的包 (复用); 见文件头第 1 条, 不能每帧新建
    MppPacket data_packet_ = nullptr;
    /// 装 EOS 的包 (复用; 建好就带上 EOS 标志)
    MppPacket eos_packet_ = nullptr;
    /// EOS 是否已经送过 (反复送会让 MPP 重复吐最后一帧)
    bool eos_sent_ = false;
    /// info_change 时建好的帧缓冲组 (half-internal 模式: MPP 分配, 应用给组)
    MppBufferGroup frm_group_ = nullptr;
    MppCodingType coding_ = MPP_VIDEO_CodingHEVC;

    int width_ = 0;
    int height_ = 0;
    int out_w_ = 0;
    int out_h_ = 0;
    std::size_t frames_ = 0;
    std::size_t dropped_ = 0;
    /// 因为输入队列满而丢掉的访问单元数 (正常情况下应该是 0)
    std::size_t dropped_input_ = 0;
    std::size_t errinfo_seen_ = 0;
    std::size_t discard_seen_ = 0;
    const char* name_ = nullptr;
    std::string error_;
    DecodeStatus status_ = DecodeStatus::kNotInitialized;

    /// 还没喂进 MPP 的访问单元
    std::deque<std::vector<std::uint8_t>> in_queue_;
    /// 已经喂进去、但还要多留几轮的缓冲 (MPP 可能还在读)
    std::deque<std::vector<std::uint8_t>> inflight_;
    /// 解好但还没交出去的帧
    std::deque<QueuedFrame> pending_;
    /// 正在借给上层的那一帧 (下一次 decode/flush 才会被覆盖)
    QueuedFrame current_;
};

#endif  // AGENT_HAVE_MPP

std::unique_ptr<VideoDecoderBackend> make_mpp_backend() {
#ifdef AGENT_HAVE_MPP
    return std::unique_ptr<VideoDecoderBackend>(new MppBackend());
#else
    return nullptr;
#endif
}

}  // namespace detail
}  // namespace agent
