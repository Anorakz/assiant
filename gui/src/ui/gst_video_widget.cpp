// gst_video_widget.cpp — 见 .h 的说明。要点：dlopen 取 GStreamer/EGL；handoff 只做**原子换手**；
// **GUI 线程 25ms 心跳**驱动重绘；paintGL 里 eglCreateImageKHR(plane0+plane1) ⇒ 贴外部纹理 ⇒ 画。
#include "gst_video_widget.h"

#include <QDebug>
#include <QElapsedTimer>
#include <QOpenGLContext>
#include <QOpenGLFunctions>
#include <QTimer>
#include <QWindow>   // window()/windowHandle() 需要完整类型
#include <dlfcn.h>

namespace {
// ---- GStreamer 基本常量（避免依赖头文件）----
const int GST_STATE_NULL = 1, GST_STATE_READY = 2, GST_STATE_PAUSED = 3, GST_STATE_PLAYING = 4;
const int GST_MESSAGE_EOS = 1, GST_MESSAGE_ERROR = 2;
const unsigned GL_TEXTURE_EXTERNAL_OES_ = 0x8D65;
// EGL 属性（EGL_EXT_image_dma_buf_import）
const int EGL_WIDTH_ = 0x3057, EGL_HEIGHT_ = 0x3056, EGL_NONE_ = 0x3038;
const int EGL_LINUX_DMA_BUF_EXT_ = 0x3270, EGL_LINUX_DRM_FOURCC_EXT_ = 0x3271;
const int EGL_DMA_BUF_PLANE0_FD_EXT_ = 0x3272, EGL_DMA_BUF_PLANE0_OFFSET_EXT_ = 0x3273;
const int EGL_DMA_BUF_PLANE0_PITCH_EXT_ = 0x3274;
const int EGL_DMA_BUF_PLANE1_FD_EXT_ = 0x3275, EGL_DMA_BUF_PLANE1_OFFSET_EXT_ = 0x3276;
const int EGL_DMA_BUF_PLANE1_PITCH_EXT_ = 0x3277;
const unsigned FOURCC_NV12_ = 0x3231564eu;  // 'NV12'
}  // namespace

// ---------------------------------------------------------------- 函数指针表
struct GstVideoWidget::Gst {
    void* hGst = nullptr; void* hGob = nullptr; void* hGl = nullptr;
    void* hEgl = nullptr;    ///< libmali / libEGL（EGL 入口在这）
    void* hAlloc = nullptr;  ///< libgstallocators（dmabuf 那两个符号在这 ✗ 不在 libgstreamer ✓）
    void (*init)(int*, char***) = nullptr;
    void* (*parse_launch)(const char*, void**) = nullptr;
    int (*element_set_state)(void*, int) = nullptr;
    void* (*bin_get_by_name)(void*, const char*) = nullptr;
    void (*object_set)(void*, const char*, ...) = nullptr;
    unsigned long (*signal_connect)(void*, const char*, void*, void*, void*, int) = nullptr;
    void* (*element_get_bus)(void*) = nullptr;
    int (*element_query_position)(void*, int, long long*) = nullptr;
    int (*element_query_duration)(void*, int, long long*) = nullptr;
    //: ★★★ 崩溃修复②（2026-10-07）：`gst_bus_pop_filtered` 返回的是 **`GstMessage*`** ✗，
    //:   我原来把它声明成 `int` ✗ ⇒ 指针被**截断成 32 位** ⇒ 后续 `gst_message_unref` 拿到
    //:   一个非法地址 ⇒ **段错误** ✓✓（`pumpBus` 每帧都被调用 ⇒ 必崩 ✓）
    void* (*bus_pop_filtered)(void*, int, unsigned) = nullptr;
    int (*message_type)(const void*) = nullptr;
    void (*message_parse_error)(void*, void**, char**) = nullptr;
    void* (*buffer_ref)(void*) = nullptr;
    void (*buffer_unref)(void*) = nullptr;
    void* (*buffer_peek_memory)(void*, unsigned) = nullptr;
    unsigned (*buffer_n_memory)(void*) = nullptr;
    int (*is_dmabuf_memory)(const void*) = nullptr;
    int (*dmabuf_get_fd)(const void*) = nullptr;
    void* (*pad_get_current_caps)(void*) = nullptr;
    char* (*caps_to_string)(void*) = nullptr;
    void (*caps_unref)(void*) = nullptr;
    void (*object_unref)(void*) = nullptr;
    void (*message_unref)(void*) = nullptr;
    void (*free_)(void*) = nullptr;
    // EGL
    void* (*eglGetCurrentDisplay)() = nullptr;
    void* (*eglGetProcAddress)(const char*) = nullptr;
    void* (*eglCreateImageKHR)(void*, void*, int, void*, const int*) = nullptr;
    void (*glEGLImageTargetTexture2DOES)(unsigned, void*) = nullptr;
};

GstVideoWidget::Gst* GstVideoWidget::gst() {
    static Gst t; static bool tried = false;
    if (tried) return t.hGst ? &t : nullptr;
    tried = true;
    auto L = [](const char* n) { return dlopen(n, RTLD_NOW | RTLD_GLOBAL); };
    t.hGst = L("libgstreamer-1.0.so.0");
    t.hGob = L("libgobject-2.0.so.0");
    t.hGl = L("libglib-2.0.so.0");
    //: ★ 零拷贝的关键：`gst_is_dmabuf_memory` / `gst_dmabuf_memory_get_fd` 在
    //:   **libgstallocators-1.0.so.0** 里 ✗（不在 libgstreamer ✓）——
    //:   从错的库 dlsym ⇒ 拿到 nullptr ⇒ 后面解引用 ⇒ 段错误（实测踩过 ✓）。
    t.hAlloc = L("libgstallocators-1.0.so.0");
    t.hEgl = L("libmali.so.1") ? L("libmali.so.1") : L("libEGL.so.1");
    if (!t.hGst || !t.hGob || !t.hGl || !t.hAlloc || !t.hEgl) {
        qWarning() << "[gstvideo] dlopen 失败（gst/gobject/glib/allocators/mali）⇒ 回退";
        t.hGst = nullptr; return nullptr;
    }
    t.init = (decltype(t.init))dlsym(t.hGst, "gst_init");
    t.parse_launch = (decltype(t.parse_launch))dlsym(t.hGst, "gst_parse_launch");
    t.element_set_state = (decltype(t.element_set_state))dlsym(t.hGst, "gst_element_set_state");
    t.bin_get_by_name = (decltype(t.bin_get_by_name))dlsym(t.hGst, "gst_bin_get_by_name");
    t.element_get_bus = (decltype(t.element_get_bus))dlsym(t.hGst, "gst_element_get_bus");
    t.element_query_position = (decltype(t.element_query_position))dlsym(t.hGst, "gst_element_query_position");
    t.element_query_duration = (decltype(t.element_query_duration))dlsym(t.hGst, "gst_element_query_duration");
    t.bus_pop_filtered = (decltype(t.bus_pop_filtered))dlsym(t.hGst, "gst_bus_pop_filtered");
    t.message_type = (decltype(t.message_type))dlsym(t.hGst, "gst_message_type");
    t.message_parse_error = (decltype(t.message_parse_error))dlsym(t.hGst, "gst_message_parse_error");
    t.buffer_ref = (decltype(t.buffer_ref))dlsym(t.hGst, "gst_buffer_ref");
    t.buffer_unref = (decltype(t.buffer_unref))dlsym(t.hGst, "gst_buffer_unref");
    t.buffer_peek_memory = (decltype(t.buffer_peek_memory))dlsym(t.hGst, "gst_buffer_peek_memory");
    t.buffer_n_memory = (decltype(t.buffer_n_memory))dlsym(t.hGst, "gst_buffer_n_memory");
    t.object_unref = (decltype(t.object_unref))dlsym(t.hGst, "gst_object_unref");
    t.message_unref = (decltype(t.message_unref))dlsym(t.hGst, "gst_message_unref");
    t.pad_get_current_caps = (decltype(t.pad_get_current_caps))dlsym(t.hGst, "gst_pad_get_current_caps");
    t.caps_to_string = (decltype(t.caps_to_string))dlsym(t.hGst, "gst_caps_to_string");
    t.caps_unref = (decltype(t.caps_unref))dlsym(t.hGst, "gst_caps_unref");
    t.is_dmabuf_memory = (decltype(t.is_dmabuf_memory))dlsym(t.hAlloc, "gst_is_dmabuf_memory");
    t.dmabuf_get_fd = (decltype(t.dmabuf_get_fd))dlsym(t.hAlloc, "gst_dmabuf_memory_get_fd");
    t.object_set = (decltype(t.object_set))dlsym(t.hGob, "g_object_set");
    t.signal_connect = (decltype(t.signal_connect))dlsym(t.hGob, "g_signal_connect_data");
    t.free_ = (decltype(t.free_))dlsym(t.hGl, "g_free");
    if (!t.parse_launch || !t.element_set_state || !t.bin_get_by_name) {
        qWarning() << "[gstvideo] 关键 Gst 符号缺失 ⇒ 回退"; t.hGst = nullptr; return nullptr;
    }
    if (!t.is_dmabuf_memory || !t.dmabuf_get_fd) {
        qWarning() << "[gstvideo] 缺 dmabuf 分配器符号 ⇒ 回退"; t.hGst = nullptr; return nullptr;
    }
    t.eglGetCurrentDisplay = (decltype(t.eglGetCurrentDisplay))dlsym(t.hEgl, "eglGetCurrentDisplay");
    t.eglGetProcAddress = (decltype(t.eglGetProcAddress))dlsym(t.hEgl, "eglGetProcAddress");
    if (!t.eglGetProcAddress) { qWarning() << "[gstvideo] 取不到 eglGetProcAddress ⇒ 回退"; t.hGst = nullptr; return nullptr; }
    t.eglCreateImageKHR = (decltype(t.eglCreateImageKHR))t.eglGetProcAddress("eglCreateImageKHR");
    t.glEGLImageTargetTexture2DOES =
        (decltype(t.glEGLImageTargetTexture2DOES))t.eglGetProcAddress("glEGLImageTargetTexture2DOES");
    if (!t.eglCreateImageKHR || !t.glEGLImageTargetTexture2DOES) {
        qWarning() << "[gstvideo] 缺 EGL dmabuf 扩展 ⇒ 回退"; t.hGst = nullptr; return nullptr;
    }
    t.init(nullptr, nullptr);
    return &t;
}

// ---------------------------------------------------------------- 生命周期
GstVideoWidget::GstVideoWidget(QWidget* parent) : QOpenGLWidget(parent) {
    //: ★★ `PartialUpdate`：FBO 内容保留 ✓ ⇒ 没新帧时不画也不会闪黑 ✓。
    //:   ⚠ 正在 A/B 验证（2026-10-08）：默认**跟随 Qt 默认值**（NoPartialUpdate ✓，
    //:   也就是"33.6~35.6fps"那次实测所用的配置 ✓）；`DSH_GST_PARTIAL=1` 才打开 PartialUpdate ✓。
    if (qEnvironmentVariableIsSet("DSH_GST_PARTIAL")) setUpdateBehavior(QOpenGLWidget::PartialUpdate);
    //: ★ A/B 开关：`DSH_GST_REPAINT_ALWAYS=1` ⇒ 每个 tick 都 repaint（不看有没有新帧 ✓，
    //:   就是实测 33.6~35.6fps 的那个配置 ✓）—— 用来把"门条件"与"驱动方式"分开判 ✓
    alwaysRepaint_ = qEnvironmentVariableIsSet("DSH_GST_REPAINT_ALWAYS");

    //: ★★★ 帧率修复（2026-10-08，第 68 轮；真因见 .h 顶部）：
    //:   由 **GUI 线程自己的 25ms 心跳**请求重绘 ✓。为什么必须这样 ✗：
    //:   Qt 给 `QOpenGLWidget` 的"重绘请求"是 **`Qt::LowEventPriority`** 事件 ✓，
    //:   只要队列里有**任何普通事件**它就被饿死 ✗ —— 而 handoff 线程过去每帧 post 一个
    //:   queued 调用（24 个/s ✓）⇒ 队列永不空 ⇒ 实测 20 请求/s 只换来 **0.8 次 paint**/s ✗
    //:   （= 历史上的 2~3fps ✓）。心跳方式实测：`paintGL` **33.6~35.6/s**、合成 **39~42/s** ✓，
    //:   `agent_gui` CPU 仍只 ~9% ✓。
    tick_ = new QTimer(this);
    tick_->setInterval(25);
    connect(tick_, &QTimer::timeout, this, [this]() { tickPaint(); });
    qInfo() << "[gstvideo] 控件建立（GUI 线程 25ms 心跳驱动 ✓；handoff 只做原子换手 ✓）";
}
GstVideoWidget::~GstVideoWidget() { teardown(); }

void GstVideoWidget::teardown() {
    if (tick_ != nullptr) tick_->stop();
    Gst* g = gst();
    while (!queue_.isEmpty() && g && g->buffer_unref) g->buffer_unref(queue_.dequeue());
    //: ★ 单槽里若还压着一帧，也要还回去 ✓（否则每换一次源就漏一个 buffer ✓）
    if (g && g->buffer_unref) {
        void* held = pending_.fetchAndStoreOrdered(nullptr);
        if (held != nullptr) g->buffer_unref(held);
    }
    if (g && pipeline_) {
        g->element_set_state(pipeline_, GST_STATE_NULL);
        if (g->object_unref) g->object_unref(pipeline_);
    }
    pipeline_ = sink_ = nullptr;
    playing_ = false;
    drewOnce_ = false;
}

// ---------------------------------------------------------------- 管线
void GstVideoWidget::onHandoff(void*, void* buffer, void*, void* user) {
    auto* self = static_cast<GstVideoWidget*>(user);
    Gst* g = gst();
    if (!self || !g || !buffer) return;
    //: ★ 到达速率打点（每 120 帧一行 ✓，用来对照"到达 vs 绘制"）
    static int cbCount = 0;
    if (++cbCount % 120 == 0) qInfo() << "[gstvideo] handoff#" << cbCount << "（回调线程到达打点 ✓）";
    //: ★★★ 崩溃修复（2026-10-07）：**绝不在这里碰 `queue_`** ✗ ——
    //:   以前回调线程 enqueue、GUI 线程 dequeue，`QQueue` 无锁 ⇒ 内存破坏 ⇒ 段错误 ⇒ 无限重启 ✗。
    //:   现在只做**一次原子换手** ✓：把新帧放进单槽，把**旧的**（若还没被消费）取回来 unref 掉 ✓
    //:   ⇒ 语义 = **丢最旧** ✓（延迟最小 ✓，不会积压 ✓）。
    void* old = self->pending_.fetchAndStoreOrdered(g->buffer_ref(buffer));
    if (old != nullptr) g->buffer_unref(old);
    //: ★★★ 帧率修复（2026-10-08）：**这里过去还会 `invokeMethod(self, [self]{ repaint(); },
    //:   Qt::QueuedConnection)`** ✗ —— 那正是"每帧一个普通优先级事件"的来源 ✓，
    //:   它把 Qt 的低优先级重绘请求**饿死**了 ✗（详见 .h 顶部与文档 §7.8 ✓）。
    //:   ⇒ 现在**一个事件都不 post** ✓，改由 GUI 线程的心跳（25ms）主动取帧重绘 ✓。
}

bool GstVideoWidget::buildPipeline(const QString& url) {
    Gst* g = gst();
    if (!g) { failed_ = true; return false; }
    teardown();
    const QByteArray desc =
        "souphttpsrc location=" + url.toUtf8() +
        " ! tsdemux ! h264parse ! mppvideodec ! video/x-raw(memory:DMABuf)"
        " ! fakesink name=sink signal-handoffs=true sync=false";
    void* err = nullptr;
    pipeline_ = g->parse_launch(desc.constData(), &err);
    if (!pipeline_) { qWarning() << "[gstvideo] 管线构造失败"; failed_ = true; return false; }
    sink_ = g->bin_get_by_name(pipeline_, "sink");
    if (!sink_) { qWarning() << "[gstvideo] 找不到 fakesink(name=sink)"; failed_ = true; return false; }
    g->object_set(sink_, "signal-handoffs", 1, "sync", 0, nullptr);
    g->signal_connect(sink_, "handoff", (void*)&GstVideoWidget::onHandoff, this, nullptr, 0);
    if (tick_ != nullptr) tick_->start();
    return true;
}

bool GstVideoWidget::setSource(const QString& url) {
    source_ = url;
    if (url.isEmpty()) { teardown(); update(); return true; }
    failed_ = false; eosSent_ = false; zeroCopyFrames_ = 0; framesShown_ = 0;
    if (!buildPipeline(url)) { emit failedOver(); return false; }
    return true;  // 真正的 PLAYING 在 initializeGL 之后（需要 GL 上下文）
}

void GstVideoWidget::initializeGL() {
    qInfo() << "[gstvideo] initializeGL 被调用 ✓（GL 上下文已建立）context="
            << (QOpenGLContext::currentContext() != nullptr);
    Gst* g = gst();
    if (!g) { failed_ = true; emit failedOver(); return; }
    if (pipeline_ && g->element_set_state) {
        g->element_set_state(pipeline_, GST_STATE_PLAYING);
        playing_ = true; emit playingChanged(true);
        qInfo() << "[gstvideo] 管线 PLAYING（源：" << source_ << "）";
    }
}

void GstVideoWidget::play() {
    Gst* g = gst();
    if (g && pipeline_) {
        g->element_set_state(pipeline_, GST_STATE_PLAYING);
        playing_ = true; emit playingChanged(true);
        if (tick_ != nullptr && !tick_->isActive()) tick_->start();
    }
}
void GstVideoWidget::pause() {
    Gst* g = gst();
    if (g && pipeline_) { g->element_set_state(pipeline_, GST_STATE_PAUSED); playing_ = false; emit playingChanged(false); }
}
void GstVideoWidget::togglePlayPause() { if (playing_) pause(); else play(); }

void GstVideoWidget::resizeGL(int, int) {
    //: 新 FBO 的内容是未定义的 ⇒ 允许下一次"没帧"的绘制先清一次黑 ✓（避免花屏一闪 ✓）
    drewOnce_ = false;
}

//: ★ 25ms 心跳（GUI 线程）：**唯一**的重绘驱动 ✓（见 .h 顶部的定案 ✓）
void GstVideoWidget::tickPaint() {
    static int tickN = 0;
    ++tickN;
    //: ★ 诊断（每 2 秒一行，80×25ms ✓）：一眼看出**定时器有没有在跑**、门条件卡在哪 ✓
    if (tickN % 80 == 0) {
        qInfo() << "[gstvideo] 心跳#" << tickN << "pipeline=" << (pipeline_ != nullptr)
                << "playing=" << playing_ << "有帧=" << (pending_.loadAcquire() != nullptr)
                << "已画帧=" << framesShown_ << "零拷贝=" << zeroCopyFrames_
                << "alwaysRepaint=" << alwaysRepaint_;
    }
    if (pipeline_ == nullptr) return;
    pumpBus();                                        // EOS/错误照常上报 ✓（与有没有新帧无关 ✓）
    if (!playing_) return;
    if (!alwaysRepaint_ && pending_.loadAcquire() == nullptr) return;  // 没新帧 ⇒ 不重画 ✓
    repaint();
}

// ---------------------------------------------------------------- 查询
//: GstFormat: 3 = GST_FORMAT_TIME（毫秒由 ns/1e6 换算）
qint64 GstVideoWidget::positionMs() const {
    Gst* g = gst();
    long long ns = 0;
    if (g && g->element_query_position && pipeline_ && g->element_query_position(pipeline_, 3, &ns))
        return ns / 1000000;
    return 0;
}
qint64 GstVideoWidget::durationMs() const {
    Gst* g = gst();
    long long ns = 0;
    if (g && g->element_query_duration && pipeline_ && g->element_query_duration(pipeline_, 3, &ns))
        return ns / 1000000;
    return 0;
}

void GstVideoWidget::pumpBus() {
    Gst* g = gst();
    if (!g || !pipeline_) return;
    void* bus = g->element_get_bus(pipeline_);
    if (!bus) return;
    for (;;) {
        void* m = reinterpret_cast<void*>(g->bus_pop_filtered(bus, GST_MESSAGE_EOS | GST_MESSAGE_ERROR, 0));
        if (!m) break;
        const int t = g->message_type(m);
        if (t == GST_MESSAGE_EOS) {
            if (!eosSent_) { eosSent_ = true; emit endOfMedia(); }
        } else if (t == GST_MESSAGE_ERROR) {
            void* e = nullptr; char* dbg = nullptr;
            g->message_parse_error(m, &e, &dbg);
            qWarning() << "[gstvideo] 管线错误 ⇒ 回退";
            failed_ = true; emit failedOver();
        }
        if (g->message_unref) g->message_unref(m);
    }
}

// ---------------------------------------------------------------- 关键：paintGL 里的零拷贝导入
void GstVideoWidget::paintGL() {
    const clock_t pt_t0 = clock();
    pumpBus();
    QOpenGLFunctions* gl = QOpenGLContext::currentContext()->functions();
    Gst* g = gst();

    //: ★ 取帧：**只在这里（GUI 线程）动这个槽** ✓ —— 原子换手，取走即"已消费" ✓
    void* buf = pending_.fetchAndStoreOrdered(nullptr);
    const int pendingCount = (buf != nullptr) ? 1 : 0;
    static int paintCount = 0;
    ++paintCount;

    if (!g || buf == nullptr) {
        //: ★ `PartialUpdate` 下**不能随便清屏** ✗（那会把上一帧擦掉 ⇒ 闪 ✓）：
        //:   只有"这一帧还没画过"（首帧 / 刚 resize ✓）才清黑 ✓，其余情况**原样保留** ✓。
        if (!drewOnce_) { gl->glClearColor(0, 0, 0, 1); gl->glClear(GL_COLOR_BUFFER_BIT); }
        return;
    }
    void* mem = g->buffer_peek_memory(buf, 0);
    if (!mem || !g->is_dmabuf_memory(mem)) {
        g->buffer_unref(buf);
        if (!drewOnce_) { gl->glClearColor(0, 0, 0, 1); gl->glClear(GL_COLOR_BUFFER_BIT); }
        return;
    }
    const int fd = g->dmabuf_get_fd(mem);
    // 尺寸/stride：NV12 且本板实测 stride==width、plane1 offset==stride*对齐高度
    //: ★★★ 噪点修复（2026-10-07，用户实测："区域位置对 ✓、内容接近噪点 ✗"）：
    //:   EGL 的 WIDTH/HEIGHT/stride **必须是视频缓冲区的真实尺寸** ✓ —— 本流后端实测为
    //:   **640x360、stride=640、plane0 offset=0** ✓（见 dmabuf-probe 输出 ✓）。
    //:   ⚠ 之前用的是**控件尺寸**（808x300）✗ ⇒ 每行步长与 UV 平面偏移全错 ⇒ 采样成**噪点** ✓。
    //:   控件尺寸只用于 `glViewport`（把 640x360 拉伸到控件大小 ✓）。
    //:   TODO：改成从 caps 串解析（`width=(int)` / `height=(int)`）以支持任意分辨率 ✓。
    //: ★★★ 绿条修复（2026-10-07，用户实测"顶部有一条绿色条"✓）：
    //:   NV12 的 UV 平面偏移 = `stride × **对齐后的高度**` ✓ —— 而 MPP/DRM 的缓冲区高度
    //:   通常是 **16 对齐**的（360 ⇒ **368** ✓）⇒ 用 360 会让 UV 从 Y 平面里取样 ⇒ **绿条** ✓✓
    //:   （着色器有 Y 翻转 ✓ ⇒ 缓冲区底部的错会显示在**画面顶部** ✓ —— 与观察一致 ✓）
    //:   TODO：改成从 `GstVideoMeta` 的 `offset[1]`/`stride[1]` 取真实值（不猜 ✓）。
    const int W = 640, H = 368;              // 640x360 内容 + 16 对齐（360 ⇒ 368）
    const int stride = 640, uvOff = stride * H;
    void* dpy = g->eglGetCurrentDisplay();
    const int attrs[] = {
        EGL_WIDTH_, W, EGL_HEIGHT_, H,
        EGL_LINUX_DRM_FOURCC_EXT_, static_cast<int>(FOURCC_NV12_),
        EGL_DMA_BUF_PLANE0_FD_EXT_, fd, EGL_DMA_BUF_PLANE0_OFFSET_EXT_, 0, EGL_DMA_BUF_PLANE0_PITCH_EXT_, stride,
        EGL_DMA_BUF_PLANE1_FD_EXT_, fd, EGL_DMA_BUF_PLANE1_OFFSET_EXT_, uvOff, EGL_DMA_BUF_PLANE1_PITCH_EXT_, stride,
        EGL_NONE_
    };
    //: ★ 按 fd 缓存 `EGLImage` ✓：板端实测 fd 只在少数值间轮换（51/54/63/67… ✓）
    //:   ⇒ 同一个 fd 再来就直接复用 ✓，不再每帧 create/destroy ✗
    //:   （曾经每帧 `eglCreateImageKHR` + destroy 上一帧 ⇒ 驱动侧串行 ~280ms ✗，已实测排除 ✓）
    struct FdImage { int fd; void* img; };
    static FdImage s_cache[8] = {};
    static int s_cacheN = 0;
    static GLuint s_tex = 0;   // ★ 纹理只建一次 ✓
    void* s_img = nullptr;
    for (int i = 0; i < s_cacheN; ++i) {
        if (s_cache[i].fd == fd) { s_img = s_cache[i].img; break; }
    }
    if (s_img == nullptr) {
        s_img = g->eglCreateImageKHR(dpy, nullptr, EGL_LINUX_DMA_BUF_EXT_, nullptr, attrs);
        if (s_img == nullptr) {
            static bool warned = false;
            if (!warned) { warned = true; qWarning() << "[gstvideo] eglCreateImageKHR 失败（plane1/尺寸）"; }
            g->buffer_unref(buf);
            if (!drewOnce_) { gl->glClearColor(0.1f, 0, 0, 1); gl->glClear(GL_COLOR_BUFFER_BIT); }
            return;
        }
        if (s_cacheN < 8) { s_cache[s_cacheN].fd = fd; s_cache[s_cacheN].img = s_img; ++s_cacheN; }
        static int made = 0;
        if (++made <= 5) qInfo() << "[gstvideo] 新建 EGLImage（fd=" << fd << "，缓存 " << s_cacheN << " 个）";
    }
    if (s_tex == 0) {
        gl->glGenTextures(1, &s_tex);
    }
    gl->glBindTexture(GL_TEXTURE_EXTERNAL_OES_, s_tex);
    gl->glTexParameteri(GL_TEXTURE_EXTERNAL_OES_, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    gl->glTexParameteri(GL_TEXTURE_EXTERNAL_OES_, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    g->glEGLImageTargetTexture2DOES(GL_TEXTURE_EXTERNAL_OES_, s_img);
    if (gl->glGetError() == GL_NO_ERROR) { zeroCopyFrames_++; }
    framesShown_++;
    drewOnce_ = true;
    g->buffer_unref(buf);

    // ---- 最小 GLES2 着色器：把外部纹理画到全屏四边形（letterbox 由 glViewport 处理）----
    static GLuint prog = 0, vbo = 0;
    if (!prog) {
        static const char* VS =
            "attribute vec2 p; varying vec2 uv;"
            "void main(){ uv = vec2((p.x+1.0)*0.5, 1.0-(p.y+1.0)*0.5); gl_Position = vec4(p,0.0,1.0); }";
        static const char* FS =
            "#extension GL_OES_EGL_image_external : require\n"
            "precision mediump float; varying vec2 uv; uniform samplerExternalOES tex;"
            "void main(){ gl_FragColor = texture2D(tex, uv); }";
        GLuint vs = gl->glCreateShader(GL_VERTEX_SHADER);
        gl->glShaderSource(vs, 1, &VS, nullptr); gl->glCompileShader(vs);
        GLuint fs = gl->glCreateShader(GL_FRAGMENT_SHADER);
        gl->glShaderSource(fs, 1, &FS, nullptr); gl->glCompileShader(fs);
        prog = gl->glCreateProgram();
        gl->glAttachShader(prog, vs); gl->glAttachShader(prog, fs); gl->glLinkProgram(prog);
        GLint ok = 0; gl->glGetProgramiv(prog, GL_LINK_STATUS, &ok);
        static const GLfloat quad[] = {-1.f, -1.f, 1.f, -1.f, -1.f, 1.f, 1.f, 1.f};
        gl->glGenBuffers(1, &vbo); gl->glBindBuffer(GL_ARRAY_BUFFER, vbo);
        gl->glBufferData(GL_ARRAY_BUFFER, sizeof(quad), quad, GL_STATIC_DRAW);
        qInfo() << "[gstvideo] 着色器程序就绪 link=" << ok;
    }
    gl->glViewport(0, 0, width(), height());
    gl->glClearColor(0, 0, 0, 1); gl->glClear(GL_COLOR_BUFFER_BIT);
    gl->glUseProgram(prog);
    gl->glActiveTexture(GL_TEXTURE0);
    gl->glBindTexture(GL_TEXTURE_EXTERNAL_OES_, s_tex);
    gl->glUniform1i(gl->glGetUniformLocation(prog, "tex"), 0);
    const GLint ap = gl->glGetAttribLocation(prog, "p");
    gl->glBindBuffer(GL_ARRAY_BUFFER, vbo);
    gl->glEnableVertexAttribArray(ap);
    gl->glVertexAttribPointer(ap, 2, GL_FLOAT, GL_FALSE, 0, nullptr);
    gl->glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
    gl->glDisableVertexAttribArray(ap);

    //: ★ 验收打点（每 120 帧一行 ✓，约 3 秒一行 ✓）：**帧率**、paintGL 内部耗时、在不在零拷贝 ✓
    const int kEvery = 120;
    if (paintCount <= 20 || paintCount % kEvery == 0) {
        static QElapsedTimer fpsT;
        static int fpsAnchor = 0;
        if (!fpsT.isValid()) { fpsT.start(); fpsAnchor = paintCount - kEvery; }
        const qint64 el = fpsT.restart();
        const double fps = (el > 0) ? (1000.0 * kEvery / double(el)) : 0.0;
        qInfo() << "[gstvideo] 绘制帧率 fps=" << QString::number(fps, 'f', 1)
                << "（每" << kEvery << "帧 " << el << "ms）内部耗时(ms)="
                << QString::number(1000.0 * double(clock() - pt_t0) / double(CLOCKS_PER_SEC), 'f', 2)
                << "零拷贝帧=" << zeroCopyFrames_ << "pending=" << pendingCount
                << "size=" << width() << "x" << height();
        fpsAnchor = paintCount;
        Q_UNUSED(fpsAnchor);
    }
}
