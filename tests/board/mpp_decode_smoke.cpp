// ============================================================================
//  tests/board/mpp_decode_smoke.cpp — 板端离线解码冒烟 (独立可执行文件)
//
//  为什么要有它
//  ---------------------------------------------------------------------------
//  宿主机测不了"真解出一帧": 没有 MPP, 也没有硬解设备。而 decoder_mpp.cpp 恰恰
//  是这次改动里最容易出错的一块 (stride 收拢、UV 平面顺序、一次包吐多帧)。
//  所以做一个**离线**冒烟: 拿一段已知内容的码流喂进去, 逐帧核对解出来的 YUV。
//
//  它只依赖测试夹具, 不需要 Sunshine / 网络 / moonlight —— 所以在板上随时能跑,
//  也不会因为"主机没开"而失败。
//
//  夹具 (tests/data/, 由 scripts/gen-decoder-testdata.py 生成)
//  ---------------------------------------------------------------------------
//      纯色帧 6 张: 红 绿 蓝 红 绿 蓝 (limited-range BT.601 的标准值)
//      每个码流旁边有一个 .expect, 第一行 "width height frames", 之后每行 "y u v"
//
//      color_1280x720_8bit.h265   生产分辨率
//      color_1280x720_8bit.h264   同画面 H.264 (验按协商格式初始化)
//      color_1272x720_8bit.h265   宽度**不对齐**: MPP 的 hor_stride 会是 1280,
//                                 专门用来验"收拢 stride"那段
//
//  三个检查都很"硬"
//  ---------------------------------------------------------------------------
//   1) 必须**真的**在用 MPP 硬解 —— 显式 kMpp 初始化, 并且检查 is_hardware()。
//      不允许悄悄退到软解然后"测试通过"。
//   2) 每个平面内部必须**所有像素相等** (纯色画面) —— 行 stride 少收/多收会把
//      padding 的垃圾读进来, 立刻表现为"平面里出现第二种值"。
//   3) 每帧的 Y/U/V 必须和 .expect 对得上 (±TOL) —— U/V 搞反、帧序错、尺寸错
//      都逃不掉。
//
//  跑法 (交叉编译后推到板端):
//      ./mpp_decode_smoke <码流路径>          # 自动读 <路径>.expect
//      ./mpp_decode_smoke tests/data/color_1280x720_8bit.h265
//
//  退出码: 0 = 全过; 1 = 有检查没过; 2 = 用法/文件问题
// ============================================================================

#include "decoder.h"

#include <algorithm>
#include <array>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <iterator>
#include <string>
#include <vector>

using agent::Decoder;
using agent::DecoderBackend;
using agent::VideoCodec;

namespace {

/// 允许的像素偏差 (有损编码对纯色块的 DC 重建通常精确, 留一点余量)
constexpr int kTol = 2;

/// 期望值: 逐帧的 (y, u, v)
struct Expect {
    int width = 0;
    int height = 0;
    std::vector<std::array<int, 3>> frames;
};

bool read_file(const std::string& path, std::vector<std::uint8_t>* out) {
    std::ifstream in(path, std::ios::binary);
    if (!in) {
        return false;
    }
    out->assign((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
    return true;
}

bool load_expect(const std::string& path, Expect* exp) {
    std::ifstream in(path);
    if (!in) {
        return false;
    }
    int frames = 0;
    if (!(in >> exp->width >> exp->height >> frames)) {
        return false;
    }
    for (int i = 0; i < frames; ++i) {
        std::array<int, 3> yuv{};
        if (!(in >> yuv[0] >> yuv[1] >> yuv[2])) {
            return false;
        }
        exp->frames.push_back(yuv);
    }
    return true;
}

// ---------------------------------------------------------------------------
//  Annex-B 拆访问单元 (AU)
//
//  moonlight 的 submitDecodeUnit 一次给的就是**一个 AU**, 所以这里也按 AU 喂,
//  跟生产路径一致 (而不是把整段流塞进一个包)。
//
//  规则: 每个 VCL NAL 开启一个新 AU, 它前面的 VPS/SPS/PPS/SEI 归到同一个 AU。
//  这对"一帧一个 slice"的流是正确的 (x264/x265 默认就是如此), 多 slice 的流
//  不适用 —— 夹具是单 slice 的纯色帧, 够用。
// ---------------------------------------------------------------------------
struct Nal {
    std::size_t begin = 0;
    std::size_t end = 0;
    int type = 0;
};

/// 找起始码 00 00 01。返回的是 00 00 01 里第一个 00 的下标;
/// 头字节固定在 +3 (4 字节起始码 00 00 00 01 会被 00 00 01 匹配到后半段, +3 仍然对)。
std::vector<Nal> find_nals(const std::vector<std::uint8_t>& buf, VideoCodec codec) {
    std::vector<std::size_t> starts;
    for (std::size_t i = 0; i + 3 <= buf.size(); ++i) {
        if (buf[i] == 0x00 && buf[i + 1] == 0x00 && buf[i + 2] == 0x01) {
            starts.push_back(i);
            i += 2;  // 跳过这个起始码, 避免把 00 00 01 01 之类重复计数
        }
    }

    std::vector<Nal> nals;
    for (std::size_t k = 0; k < starts.size(); ++k) {
        Nal nal;
        nal.begin = starts[k];
        nal.end = (k + 1 < starts.size()) ? starts[k + 1] : buf.size();
        const std::uint8_t header = buf[starts[k] + 3];
        // H.264: 低 5 位是 nal_unit_type; H.265: 高 1 位是 forbidden, 接着 6 位是 type
        nal.type = (codec == VideoCodec::kH264) ? (header & 0x1F) : ((header >> 1) & 0x3F);
        nals.push_back(nal);
    }
    return nals;
}

bool is_vcl(int type, VideoCodec codec) {
    if (codec == VideoCodec::kH264) {
        return type == 1 || type == 5;  // 非 IDR 片 / IDR 片
    }
    return type >= 0 && type <= 31;  // HEVC 的 VCL 是 0..31
}

std::vector<std::vector<std::uint8_t>> split_access_units(const std::vector<std::uint8_t>& buf,
                                                          VideoCodec codec) {
    const std::vector<Nal> nals = find_nals(buf, codec);

    std::vector<std::vector<std::uint8_t>> units;
    std::size_t au_begin = nals.empty() ? 0 : nals.front().begin;
    std::size_t au_end = au_begin;
    bool au_has_vcl = false;

    for (const Nal& nal : nals) {
        const bool vcl = is_vcl(nal.type, codec);
        if (vcl && au_has_vcl) {
            // 上一个 AU 到这就结束了
            units.emplace_back(buf.begin() + au_begin, buf.begin() + au_end);
            au_begin = nal.begin;
            au_has_vcl = false;
        }
        au_has_vcl = au_has_vcl || vcl;
        au_end = nal.end;
    }
    if (au_end > au_begin) {
        units.emplace_back(buf.begin() + au_begin, buf.begin() + au_end);
    }
    return units;
}

// ---------------------------------------------------------------------------
//  一帧的核对结果
// ---------------------------------------------------------------------------
struct FrameCheck {
    int width = 0;
    int height = 0;
    std::array<int, 3> yuv{};
    bool uniform = false;
};

/// 量一帧: 尺寸 + 每个平面是不是所有像素都相等 (纯色画面必须相等)
FrameCheck measure(const std::uint8_t* i420, int w, int h) {
    FrameCheck fc;
    fc.width = w;
    fc.height = h;

    const std::size_t ys = static_cast<std::size_t>(w) * h;
    const std::size_t cs = ys / 4;
    const std::uint8_t* planes[3] = {i420, i420 + ys, i420 + ys + cs};
    const std::size_t sizes[3] = {ys, cs, cs};

    fc.uniform = true;
    for (int p = 0; p < 3; ++p) {
        fc.yuv[p] = planes[p][0];
        for (std::size_t i = 1; i < sizes[p]; ++i) {
            if (std::abs(static_cast<int>(planes[p][i]) - fc.yuv[p]) > kTol) {
                fc.uniform = false;
                break;
            }
        }
    }
    return fc;
}

}  // namespace

int main(int argc, char** argv) {
    // 输出不缓冲: 这个程序跑在硬解上, 出了段错误时缓冲里的报告会全丢,
    // 那可就没法定位了。慢一点无所谓。
    std::setvbuf(stdout, nullptr, _IONBF, 0);

    // -v: 逐个 AU 打印状态, 排障用 (平时不需要)
    bool verbose = false;
    std::vector<std::string> args;
    for (int i = 1; i < argc; ++i) {
        if (std::string(argv[i]) == "-v") {
            verbose = true;
        } else {
            args.push_back(argv[i]);
        }
    }
    if (args.empty()) {
        std::fprintf(stderr,
                     "用法: %s [-v] <码流路径> [.expect 路径]\n"
                     "  例: %s tests/data/color_1280x720_8bit.h265\n",
                     argv[0], argv[0]);
        return 2;
    }

    const std::string stream_path = args[0];
    const std::string expect_path = (args.size() >= 2) ? args[1] : (stream_path + ".expect");

    // 编码格式按文件名后缀判断 (夹具命名就是这么定的)
    const VideoCodec codec =
        (stream_path.find(".h264") != std::string::npos) ? VideoCodec::kH264 : VideoCodec::kH265;

    std::vector<std::uint8_t> stream;
    if (!read_file(stream_path, &stream) || stream.empty()) {
        std::fprintf(stderr, "读不到码流: %s\n", stream_path.c_str());
        return 2;
    }
    Expect exp;
    if (!load_expect(expect_path, &exp) || exp.frames.empty()) {
        std::fprintf(stderr, "读不到期望值: %s\n", expect_path.c_str());
        return 2;
    }

    std::printf("=== MPP 解码冒烟: %s ===\n", stream_path.c_str());
    std::printf("  码流 %zu 字节, 编码 %s, 期望 %dx%d / %zu 帧\n", stream.size(),
                codec == VideoCodec::kH264 ? "H.264" : "H.265", exp.width, exp.height,
                exp.frames.size());

    // ---- 初始化: **显式 kMpp**, 不允许悄悄退软解 ----
    Decoder decoder;
    if (!decoder.init(exp.width, exp.height, codec, DecoderBackend::kMpp)) {
        std::printf("  FAIL: init(kMpp) 失败: %s\n", decoder.last_error());
        return 1;
    }
    std::printf("  解码后端: %s (hardware=%s)\n", decoder.active_decoder_name(),
                decoder.is_hardware() ? "yes" : "no");
    if (!decoder.is_hardware() || decoder.active_backend() != DecoderBackend::kMpp) {
        std::printf("  FAIL: 要求 kMpp 硬解, 实际 backend=%d hardware=%d\n",
                    static_cast<int>(decoder.active_backend()),
                    static_cast<int>(decoder.is_hardware()));
        return 1;
    }

    // ---- 拆 AU 并逐帧喂 ----
    const std::vector<std::vector<std::uint8_t>> units = split_access_units(stream, codec);
    std::printf("  拆出 %zu 个访问单元\n", units.size());
    if (units.size() != exp.frames.size()) {
        std::printf("  FAIL: 期望 %zu 个 AU, 实际 %zu\n", exp.frames.size(), units.size());
        return 1;
    }

    std::vector<FrameCheck> got;
    std::uint8_t* yuv = nullptr;
    int vw = 0;
    int vh = 0;

    for (std::size_t i = 0; i < units.size(); ++i) {
        // 硬解有流水线延迟: 前几个 AU 拿不到帧是正常的, 所以返回 false 不算错,
        // 真正的判断放在最后"总共解出几帧"。
        const bool produced =
            decoder.decode(units[i].data(), units[i].size(), &yuv, &vw, &vh);
        if (produced) {
            // 借出的指针下一次 decode 就失效 —— 必须**当场**量完
            got.push_back(measure(yuv, vw, vh));
        }
        if (verbose) {
            std::printf("    AU %zu: %zu 字节 -> %s  status=%d%s%s\n", i, units[i].size(),
                        produced ? "有帧" : "无帧", static_cast<int>(decoder.status()),
                        decoder.last_error()[0] ? "  err=" : "", decoder.last_error());
        }
    }

    // 冲刷流水线里剩下的帧 (送空包触发, 然后反复取)。
    // ⚠ 必须用 flush() 自己给出的新指针: 上一次 decode() 的指针这时已经失效了
    //   (缓冲被新的一帧覆盖)。用旧指针读就是段错误。
    int empty_rounds = 0;
    while (empty_rounds < 2) {
        const bool produced = decoder.flush(&yuv, &vw, &vh);
        if (produced) {
            got.push_back(measure(yuv, vw, vh));
            empty_rounds = 0;
        } else {
            ++empty_rounds;
        }
        if (verbose) {
            std::printf("    flush -> %s  status=%d%s%s\n", produced ? "有帧" : "无帧",
                        static_cast<int>(decoder.status()),
                        decoder.last_error()[0] ? "  err=" : "", decoder.last_error());
        }
    }

    std::printf("  解出 %zu 帧 (frames_decoded=%zu)\n", got.size(), decoder.frames_decoded());
    if (got.empty() && decoder.last_error()[0] != '\0') {
        std::printf("  解码器最后报错: %s\n", decoder.last_error());
    }

    // ---- 逐帧核对 ----
    int failures = 0;
    if (got.size() != exp.frames.size()) {
        std::printf("  FAIL: 期望 %zu 帧, 实际 %zu 帧\n", exp.frames.size(), got.size());
        ++failures;
    }

    const std::size_t n = std::min(got.size(), exp.frames.size());
    for (std::size_t i = 0; i < n; ++i) {
        const FrameCheck& fc = got[i];
        const std::array<int, 3>& want = exp.frames[i];
        bool ok = true;

        if (fc.width != exp.width || fc.height != exp.height) {
            std::printf("  FAIL f%zu: 尺寸 %dx%d, 期望 %dx%d\n", i, fc.width, fc.height,
                        exp.width, exp.height);
            ok = false;
        }
        if (!fc.uniform) {
            std::printf("  FAIL f%zu: 平面内有多种取值 —— stride 收拢错了\n", i);
            ok = false;
        }
        for (int p = 0; p < 3; ++p) {
            if (std::abs(fc.yuv[p] - want[p]) > kTol) {
                std::printf("  FAIL f%zu: %s=%d, 期望 %d\n", i, "YUV"[p], fc.yuv[p], want[p]);
                ok = false;
            }
        }

        if (!ok) {
            ++failures;
        } else {
            std::printf("  ok   f%zu  %dx%d  y=%3d u=%3d v=%3d\n", i, fc.width, fc.height,
                        fc.yuv[0], fc.yuv[1], fc.yuv[2]);
        }
    }

    if (failures != 0) {
        std::printf("=== FAIL (%d 项没过) ===\n", failures);
        return 1;
    }
    std::printf("=== PASS ===\n");
    return 0;
}
