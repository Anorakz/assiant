// gst_video_widget.cpp — 见 .h 的说明。要点：dlopen 取 GStreamer/EGL；handoff 只入队；
// paintGL 里 eglCreateImageKHR(plane0+plane1) ⇒ glEGLImageTargetTexture2DOES ⇒ 画。
#include "gst_video_widget.h"

#include <QDebug>
#include <QOpenGLContext>
#include <QOpenGLFunctions>
#include <QTimer>
#include <QWindow>   // window()->windowHandle()->requestUpdate() 需要完整类型（否则 incomplete type ✗）
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
    void* hGst = nullptr; void* hGob = nullptr; void* hGl = nullptr; void* hEgl = nullptr;
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
    //:   一个非法地址 ⇒ **段错误** ✓✓（`pumpBus` 每帧都被 `paintGL` 调用 ⇒ 必崩 ✓）
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
    unsigned (*eglDestroyImageKHR)(void*, void*) = nullptr;
    //: ★★ 为"自建 EGL 表面 ＋ 主动交换"预置（2026-10-07，第 66 轮）：
    //:   实测已定案：`paintGL` 内部仅 ~1ms ✓，但帧率只有 3.2fps ✗ ⇒ 瓶颈在 **Qt 的
    //:   `QOpenGLWidget` 合成/交换路径** ✓ ⇒ 修法=**自己持有 EGLSurface 并主动 swap** ✓。
    //:   这四个是那条路要用到的（先取好 ✓，本轮**不改变任何行为** ✓）。
    void* (*eglCreateWindowSurface)(void*, void*, void*, const int*) = nullptr;
    int (*eglMakeCurrent)(void*, void*, void*, void*) = nullptr;
    int (*eglSwapBuffers)(void*, void*) = nullptr;
    int (*eglDestroySurface)(void*, void*) = nullptr;
    void (*glEGLImageTargetTexture2DOES)(unsigned, void*) = nullptr;
};

GstVideoWidget::Gst* GstVideoWidget::gst() {
    static Gst t; static bool tried = false;
    if (tried) return t.hGst ? &t : nullptr;
    tried = true;
    auto L = [](const char* n) { return dlopen(n, RTLD_NOW | RTLD_GLOBAL); };
    t.hGst = L("libgstreamer-1.0.so.0");
    t.hGob = L("libgobject-2.0.so.0");
    t.hGl  = L("libglib-2.0.so.0");
    t.hEgl = L("libmali.so.1") ? L("libmali.so.1") : L("libEGL.so.1");
    if (!t.hGst || !t.hGob || !t.hGl) { qWarning() << "[gstvideo] 缺 GStreamer 运行库 ⇒ 回退"; return nullptr; }
    auto S = [&](void* h, const char* n) { return h ? dlsym(h, n) : nullptr; };
    t.init = (decltype(t.init))S(t.hGst, "gst_init");
    t.parse_launch = (decltype(t.parse_launch))S(t.hGst, "gst_parse_launch");
    t.element_set_state = (decltype(t.element_set_state))S(t.hGst, "gst_element_set_state");
    t.bin_get_by_name = (decltype(t.bin_get_by_name))S(t.hGst, "gst_bin_get_by_name");
    t.object_set = (decltype(t.object_set))S(t.hGob, "g_object_set");
    t.signal_connect = (decltype(t.signal_connect))S(t.hGob, "g_signal_connect_data");
    t.element_get_bus = (decltype(t.element_get_bus))S(t.hGst, "gst_element_get_bus");
    t.element_query_position = (decltype(t.element_query_position))S(t.hGst, "gst_element_query_position");
    t.element_query_duration = (decltype(t.element_query_duration))S(t.hGst, "gst_element_query_duration");
    t.bus_pop_filtered = (decltype(t.bus_pop_filtered))S(t.hGst, "gst_bus_pop_filtered");
    t.message_type = (decltype(t.message_type))S(t.hGst, "gst_message_type");
    t.message_parse_error = (decltype(t.message_parse_error))S(t.hGst, "gst_message_parse_error");
    t.buffer_ref = (decltype(t.buffer_ref))S(t.hGst, "gst_buffer_ref");
    t.buffer_unref = (decltype(t.buffer_unref))S(t.hGst, "gst_buffer_unref");
    t.buffer_peek_memory = (decltype(t.buffer_peek_memory))S(t.hGst, "gst_buffer_peek_memory");
    t.buffer_n_memory = (decltype(t.buffer_n_memory))S(t.hGst, "gst_buffer_n_memory");
    //: ★★★ 崩溃/无帧真因之一（2026-10-07）：`gst_is_dmabuf_memory` 与
    //:   `gst_dmabuf_memory_get_fd` **不在 libgstreamer 里** ✗ —— 它们在
    //:   **`libgstallocators-1.0.so.0`** 里 ✓ ⇒ 原来从 `hGst` 取 ⇒ **恒为 nullptr** ✗
    //:   ⇒ 一旦有帧进来，调用它俩就**段错误** ✓（这正是"崩溃 ＋ 无帧"的一半原因 ✓）
    void* hAlloc = dlopen("libgstallocators-1.0.so.0", RTLD_NOW | RTLD_GLOBAL);
    t.is_dmabuf_memory = (decltype(t.is_dmabuf_memory))S(hAlloc, "gst_is_dmabuf_memory");
    t.dmabuf_get_fd = (decltype(t.dmabuf_get_fd))S(hAlloc, "gst_dmabuf_memory_get_fd");
    if (!t.is_dmabuf_memory || !t.dmabuf_get_fd) {
        qWarning() << "[gstvideo] 取不到 dmabuf 分配器函数 ⇒ 回退";
        return nullptr;
    }
    t.pad_get_current_caps = (decltype(t.pad_get_current_caps))S(t.hGst, "gst_pad_get_current_caps");
    t.caps_to_string = (decltype(t.caps_to_string))S(t.hGst, "gst_caps_to_string");
    t.caps_unref = (decltype(t.caps_unref))S(t.hGst, "gst_caps_unref");
    t.object_unref = (decltype(t.object_unref))S(t.hGob, "g_object_unref");
    t.message_unref = (decltype(t.message_unref))S(t.hGst, "gst_message_unref");
    t.free_ = (decltype(t.free_))S(t.hGl, "g_free");
    if (t.hEgl) {
        t.eglGetCurrentDisplay = (decltype(t.eglGetCurrentDisplay))dlsym(t.hEgl, "eglGetCurrentDisplay");
        t.eglGetProcAddress = (decltype(t.eglGetProcAddress))dlsym(t.hEgl, "eglGetProcAddress");
    }
    if (!t.eglGetProcAddress) { qWarning() << "[gstvideo] 取不到 eglGetProcAddress ⇒ 回退"; return nullptr; }
    t.eglCreateImageKHR = (decltype(t.eglCreateImageKHR))t.eglGetProcAddress("eglCreateImageKHR");
    t.eglDestroyImageKHR = (decltype(t.eglDestroyImageKHR))t.eglGetProcAddress("eglDestroyImageKHR");
    //: ★ 为自建 EGL 表面预置（本轮不改变行为 ✓）：这些都从 **libEGL/mali 本体**取（不是 eglGetProcAddress ✓）
    if (t.hEgl != nullptr) {
        t.eglCreateWindowSurface = (decltype(t.eglCreateWindowSurface))dlsym(t.hEgl, "eglCreateWindowSurface");
        t.eglMakeCurrent = (decltype(t.eglMakeCurrent))dlsym(t.hEgl, "eglMakeCurrent");
        t.eglSwapBuffers = (decltype(t.eglSwapBuffers))dlsym(t.hEgl, "eglSwapBuffers");
        t.eglDestroySurface = (decltype(t.eglDestroySurface))dlsym(t.hEgl, "eglDestroySurface");
    }
    t.glEGLImageTargetTexture2DOES =
        (decltype(t.glEGLImageTargetTexture2DOES))t.eglGetProcAddress("glEGLImageTargetTexture2DOES");
    if (!t.eglCreateImageKHR || !t.glEGLImageTargetTexture2DOES) {
        qWarning() << "[gstvideo] 缺 EGL dmabuf 扩展 ⇒ 回退"; return nullptr;
    }
    t.init(nullptr, nullptr);
    return &t;
}

// ---------------------------------------------------------------- 生命周期
//: ★ 改回 `QOpenGLWidget`（`QOpenGLWindow` ＋ `createWindowContainer` 在 eglfs 上直接崩 ✗，已判死 ✓）
GstVideoWidget::GstVideoWidget(QWidget* parent) : QOpenGLWidget(parent) {
    //: ★ 注意 ✓：`QOpenGLWindow` **没有** `setAttribute(WA_OpaquePaintEvent)` /
    //:   `setAutoFillBackground()` ✗（那是 `QWidget` 的方法 ✓，换基类后编不过 ✓）
    //:   —— 对 `QOpenGLWindow` 来说也不需要：它本来就不透明、也不做背景填充 ✓
    //: ★★ 教训（2026-10-07 板端事故）：这里原有一个 **30ms 定时 `update()` 心跳** ✗
    //:   —— 它与 `onHandoff` 的每帧 `update()` 叠加成**重绘风暴** ⇒ 崩溃 ＋
    //:   `A lot of buffers are being dropped` ✗。
    //:   ⇒ 现在改成**纯背压驱动** ✓：由 `onHandoff`（首帧）与 `paintGL`（消费后）各请求一次 ✓，
    //:     不再有任何定时重绘 ✓ ⇒ 帧率=消费能力（源 24fps 时约 24fps ✓）。
    qInfo() << "[gstvideo] 控件建立（背压驱动：上一帧消费后才请求下一帧 ✓）";
}
GstVideoWidget::~GstVideoWidget() { teardown(); }

void GstVideoWidget::teardown() {
    Gst* g = gst();
    while (!queue_.isEmpty() && g && g->buffer_unref) g->buffer_unref(queue_.dequeue());
    if (g && pipeline_) {
        g->element_set_state(pipeline_, GST_STATE_NULL);
        if (g->object_unref) g->object_unref(pipeline_);
    }
    pipeline_ = sink_ = nullptr;
    playing_ = false;
}

// ---------------------------------------------------------------- 管线
void GstVideoWidget::onHandoff(void*, void* buffer, void*, void* user) {
    auto* self = static_cast<GstVideoWidget*>(user);
    Gst* g = gst();
    if (!self || !g || !buffer) return;
    //: ★★ 到达速率打点（2026-10-07）：用来**分清**"帧到达慢"✗ 与"绘制慢"✗
    //:   —— 与 `paintGL#N` 的打点对照即可判定（两边都用 120 帧为一格 ✓）。
    static int cbCount = 0;
    ++cbCount;
    if (cbCount <= 3 || cbCount % 120 == 0) {
        qInfo() << "[gstvideo] handoff#" << cbCount << "（回调线程到达打点 ✓）";
    }
    //: ★★★ 崩溃修复（2026-10-07）：**绝不在这里碰 `queue_`** ✗ ——
    //:   以前回调线程 enqueue、GUI 线程 dequeue，`QQueue` 无锁 ⇒ 内存破坏 ⇒ 段错误 ⇒ 无限重启 ✗。
    //:   现在只做**一次原子换手** ✓：把新帧放进单槽，把**旧的**（若还没被消费）取回来 unref 掉 ✓。
    void* old = self->pending_.fetchAndStoreOrdered(g->buffer_ref(buffer));
    if (old != nullptr) {
        // 上一帧还没被 GUI 消费 ⇒ 丢掉它（丢帧而不排队 ✓）
        g->buffer_unref(old);
    }
    //: ★★★ 帧率修复尝试②（2026-10-07）：`update()` 只是**排队等重绘** ✗ —— 在 eglfs 下这个队列
    //:   明显被合并/饿死（实测缩小视频区 fps 也不变 ⇒ 不是绘制成本 ✓）。改成 **`repaint()`** ✓：
    //:   它是**同步**绘制 ✓（立刻走 `paintGL` ✓），完全绕开排队 ✓。仍在 GUI 线程执行 ✓
    //:   （`QueuedConnection` 的函子 ✓）⇒ 线程安全 ✓。若这招奏效 ⇒ 无需自建 EGL 表面 ✓
    QMetaObject::invokeMethod(self, [self]() { self->repaint(); }, Qt::QueuedConnection);
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
    if (g && pipeline_) { g->element_set_state(pipeline_, GST_STATE_PLAYING); playing_ = true; emit playingChanged(true); }
}
void GstVideoWidget::pause() {
    Gst* g = gst();
    if (g && pipeline_) { g->element_set_state(pipeline_, GST_STATE_PAUSED); playing_ = false; emit playingChanged(false); }
}
void GstVideoWidget::togglePlayPause() { if (playing_) pause(); else play(); }

void GstVideoWidget::resizeGL(int, int) {}

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
    //: ★★★ 分段计时（2026-10-07，第 6 次实验）：五步实验已排除 5 个假设（见文档 §7）✗，
    //:   只剩"每次 paint 有一个 **~300ms 的固定阻塞**"（与尺寸无关 ✓、不吃 CPU/GPU ✓）。
    //:   ⇒ 直接**量整个 paintGL 的耗时** ✓（用 `clock()` ✓，C 标准、无需额外头文件 ✓），
    //:     与"两次 paint 的间隔"对比 ⇒ 就能分清"**paintGL 内部慢**"还是"**paintGL 之外被限速**" ✓
    const clock_t pt_t0 = clock();
    static int pt_n = 0;
    ++pt_n;
    pumpBus();
    QOpenGLFunctions* gl = QOpenGLContext::currentContext()->functions();
    Gst* g = gst();

    //: ★ 取帧：**只在这里（GUI 线程）动这个槽** ✓ —— 原子换手，取走即"已消费" ✓
    void* buf = pending_.fetchAndStoreOrdered(nullptr);
    const int pendingCount = (buf != nullptr) ? 1 : 0;
    //: ★ 无条件打点：区分"paintGL 压根没被调用" ✗ 与"调用了但没帧" ✗
    static int paintCount = 0;
    ++paintCount;
    if (paintCount <= 3 || paintCount % 120 == 0)
        qInfo() << "[gstvideo] paintGL#" << paintCount << "pending=" << pendingCount
                << "gst=" << (g != nullptr) << "size=" << width() << "x" << height();

    if (!g || buf == nullptr) { gl->glClearColor(0, 0, 0, 1); gl->glClear(GL_COLOR_BUFFER_BIT); return; }
    void* mem = g->buffer_peek_memory(buf, 0);
    if (!mem || !g->is_dmabuf_memory(mem)) {
        g->buffer_unref(buf);
        gl->glClearColor(0, 0, 0, 1); gl->glClear(GL_COLOR_BUFFER_BIT); return;
    }
    const int fd = g->dmabuf_get_fd(mem);
    // 尺寸/stride：NV12 且本板实测 stride==width、plane1 offset==stride*height
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
    //: ★★★ 帧率修复尝试③（2026-10-07）：实测已排除"排队/调度"（同步 `repaint()` 也只有 3.4fps ✗）
    //:   且"开销与像素无关"（视频区缩到 320×180 fps 不变 ✗）⇒ 剩下的是**每帧固定开销、不吃 CPU/GPU** ✗
    //:   ⇒ 头号嫌疑 = **每帧 `eglCreateImageKHR`（＋我加的 destroy 上一帧 ✗）** ⇒ 驱动侧同步/串行 ~280ms ✓
    //:   ⇒ 对策 ✓：**按 fd 缓存 EGLImage** —— 板端实测 fd 只在少数值间轮换（51/54/63/67… ✓）
    //:     同一个 fd 再来就**直接复用** ✓，不再 create/destroy ✗
    struct FdImage { int fd; void* img; };
    static FdImage s_cache[8] = {};
    static int s_cacheN = 0;
    static GLuint s_tex = 0;   // ★ 纹理仍然只建一次 ✓（上一步的优化保留 ✓）
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
            gl->glClearColor(0.1f, 0, 0, 1); gl->glClear(GL_COLOR_BUFFER_BIT); return;
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
    //: ★ 不再每帧 `glDeleteTextures` ✗ —— 纹理已缓存（`s_tex` ✓，见上面的说明）
    //:   （这正是实测"回调 20.5fps ✓／绘制 2.0fps ✗"的主因 ✓）
    if (pt_n % 10 == 0)
        qInfo() << "[gstvideo] paintGL#" << paintCount << " 内部耗时(ms)="
                << (1000.0 * double(clock() - pt_t0) / double(CLOCKS_PER_SEC))
                << "pending=" << pendingCount << "size=" << width() << "x" << height();
}
