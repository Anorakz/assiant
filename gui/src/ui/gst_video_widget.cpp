// gst_video_widget.cpp — 见 .h 的说明。要点：dlopen 取 GStreamer/EGL；handoff 只做**原子换手**；
// **GUI 线程 25ms 心跳**驱动重绘；paintGL 里 eglCreateImageKHR(plane0+plane1) ⇒ 贴外部纹理 ⇒ 画。
#include "gst_video_widget.h"

#include <QCoreApplication>
#include <QDebug>
#include <QElapsedTimer>
#include <QEvent>
#include <QOpenGLContext>
#include <QOpenGLFunctions>
#include <QTimer>
#include <QWindow>   // window()/windowHandle() 需要完整类型
#include <cstdlib>   // atoi（解析 caps ✓）
#include <cstring>   // strstr（解析 caps ✓）
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

//: ★★ 诊断（2026-10-08 第 72 轮）：**"到底有几个 `GstVideoWidget`"** 是上一条矛盾（帧到达 26/s
//:   但单槽常空 ✓）的头号分叉 ✓。这两个计数器只在 GUI 线程动（构造/析构 ✓）⇒ 普通 int 就够 ✓。
int g_liveWidgets = 0;   ///< 当前存活的实例数（>1 就说明"帧写进了另一个实例" ✓）
int g_nextWidgetId = 0;  ///< 给每个实例编号（比裸 `this` 好认 ✓）

//: （"帧到达事件"那条路已在第 75 轮被实测判掉 ✗：事件投得出去、但每次 `repaint()` 只有 ~1/10
//:   变成 `paintGL` ✓ —— 主驱动因此回到 QTimer ✓，这里不再需要自定义事件类型 ✓）

//: ★★ 第 76 轮（用户实测"闪回旧帧" ＋ "GUI 短暂重启"⇒ 查到 3 次 SIGSEGV ✓）：
//:   fd 键的 EGLImage 缓存**必须跨流清掉** ✗ —— 换流后旧的 dmabuf 已经被关 ✓，
//:   而缓存里那条 EGLImage 还指着它 ⇒ 再 `glEGLImageTargetTexture2DOES` 绑上去就是
//:   **拿已释放的 dmabuf 当纹理** ⇒ 轻则闪出旧帧 ✓、重则驱动层直接 SIGSEGV ✓。
//:   ⇒ 缓存改成**文件作用域** ✓，`teardown()` 里统一 destroy + 清空 ✓。
struct FdImage { int fd; void* img; };
FdImage g_fdCache[8];
int g_fdCacheN = 0;
GLuint g_tex = 0;              ///< 外部纹理 ✓（**随 GL 上下文** ⇒ 上下文重建必须清零 ✗）
GLuint g_prog = 0, g_vbo = 0;  ///< 着色器/顶点缓冲 ✓（同上 ✗）
int g_capsW = 0, g_capsH = 0;  ///< 从 caps 解析出的**真实**视频尺寸 ✓（换流要重解析 ✓）

/// ★★ 第 77 轮（崩溃真因之一）：这些 GL 名字/vbo/纹理都是**每个 GL 上下文一份** ✓，
//:   而 `QOpenGLWidget` 的上下文会被 Qt **重建**（板端日志实测 `initializeGL` 出现过 4 次 ✓）——
//:   重建之后旧的 `GLuint` 全**失效** ✗，再绑上去就是驱动层 **SIGSEGV** ✓（实测偶发重启 ✓）。
//:   ⇒ 所以在 `initializeGL()` 里统一清零 + 允许下次重建 ✓；同时清掉 EGLImage 缓存 ✓
//:   （缓存里的 image 也属于旧上下文/旧 dmabuf ✓）。
void resetGlStateForNewContext() {
    g_tex = 0;
    g_prog = 0;
    g_vbo = 0;
    g_fdCacheN = 0;   // 旧的 EGLImage 随上下文作废 ✓（不 destroy：display 可能已失效 ✗，交给驱动回收 ✓）
}

/// 单调毫秒（诊断用 ✓；可被 handoff 线程调用 ⇒ C++11 的静态初始化是线程安全的 ✓）
qint64 monoMs() {
    static QElapsedTimer t = []() { QElapsedTimer e; e.start(); return e; }();
    return t.elapsed();
}
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
    /// ★ 第 76 轮：换流时要**销毁**旧的 EGLImage ✓（否则旧的 dmabuf 已关 ⇒ 死引用 ⇒ 闪旧帧/SIGSEGV ✗）
    unsigned (*eglDestroyImageKHR)(void*, void*) = nullptr;
    void (*glEGLImageTargetTexture2DOES)(unsigned, void*) = nullptr;
    /// ★ 第 80 轮：真实几何 —— 从 sink pad 的 caps 取宽高 ✓、从内存大小反推对齐高度 ✓
    void* (*element_get_static_pad)(void*, const char*) = nullptr;
    unsigned long (*memory_get_sizes)(void*, unsigned long*, unsigned long*) = nullptr;
    /// ★★★ 第 80 轮（换流偶发崩溃的**真凶** ✓）：`gst_element_set_state()` **是异步的** ✗，
    //:   必须用 `gst_element_get_state(..., timeout)` **等它真的停** ✓ 才能 unref ✗
    //:   （否则"已经在拆的流线程"稍后踩到已释放的管线 ⇒ 崩在几秒~几十秒后 ✓✓ 与实测吻合 ✓）
    int (*element_get_state)(void*, int*, int*, unsigned long long) = nullptr;
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
    t.eglDestroyImageKHR = (decltype(t.eglDestroyImageKHR))t.eglGetProcAddress("eglDestroyImageKHR");
    t.element_get_static_pad = (decltype(t.element_get_static_pad))dlsym(t.hGst, "gst_element_get_static_pad");
    t.element_get_state = (decltype(t.element_get_state))dlsym(t.hGst, "gst_element_get_state");
    t.memory_get_sizes = (decltype(t.memory_get_sizes))dlsym(t.hGst, "gst_memory_get_sizes");
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
    //: ★★★ `PartialUpdate` 现在是**必需且默认**的（第 75 轮实测定案 ✓）：
    //:   主驱动（25ms 心跳）**无条件** repaint ✓ ⇒ 有约四成的拍**没有新帧** ✓；
    //:   而 Qt 默认的 `NoPartialUpdate` 会在每次合成后**丢掉 FBO 内容** ✗（`invalidateFbo()` ✓）
    //:   ⇒ 那些"没新帧"的拍会把画面清黑 ⇒ **闪** ✗。
    //:   改成 `PartialUpdate` ⇒ FBO 内容保留 ✓ ⇒ 没新帧时 `paintGL` 直接返回 ✓，屏幕上是上一帧 ✓。
    setUpdateBehavior(QOpenGLWidget::PartialUpdate);

    //: ★★★ 帧率修复（2026-10-08，第 68 轮；真因见 .h 顶部）：
    //:   由 **GUI 线程自己的 25ms 心跳**请求重绘 ✓。为什么必须这样 ✗：
    //:   Qt 给 `QOpenGLWidget` 的"重绘请求"是 **`Qt::LowEventPriority`** 事件 ✓，
    //:   只要队列里有**任何普通事件**它就被饿死 ✗ —— 而 handoff 线程过去每帧 post 一个
    //:   queued 调用（24 个/s ✓）⇒ 队列永不空 ⇒ 实测 20 请求/s 只换来 **0.8 次 paint**/s ✗
    //:   （= 历史上的 2~3fps ✓）。心跳方式实测：`paintGL` **33.6~35.6/s**、合成 **39~42/s** ✓，
    //:   `agent_gui` CPU 仍只 ~9% ✓。
    tick_ = new QTimer(this);
    //: ★ 第 78 轮：心跳 10ms ✓（只当"节拍源" ✓）＋ `tickPaint` 里 33ms 的**画帧门** ✓
    //:   ⇒ 显示节拍稳定在 ~30fps ✓（板端实测：25ms 心跳 + 30ms 门会量化成 50ms = 20fps ✗，
    //:     把心跳缩到 10ms 才能真正拿到 33ms ✓）；少画的那部分不浪费 —— 有帧才走 GL ✓。
    tick_->setInterval(10);
    connect(tick_, &QTimer::timeout, this, [this]() { tickPaint(); });
    id_ = ++g_nextWidgetId;
    ++g_liveWidgets;
    qInfo() << "[gstvideo] 控件建立 id=" << id_ << "this=" << this
            << "存活实例=" << g_liveWidgets
            << "（GUI 线程 25ms 心跳驱动 ✓；handoff 只做原子换手 ✓）";
}
GstVideoWidget::~GstVideoWidget() {
    --g_liveWidgets;
    qInfo() << "[gstvideo] 控件析构 id=" << id_ << "this=" << this
            << "存活实例=" << g_liveWidgets
            << "收到 handoff=" << handoffN_ << "存槽=" << storeN_
            << "心跳=" << tickN_ << "paintGL=" << paintN_;
    teardown();
}

void GstVideoWidget::teardown() {
    //: ★★★ 第 80 轮：**先关收帧闸门** ✓（见 `acceptFrames_` 的说明 —— 这是"换流偶发 SIGSEGV"的头号嫌疑 ✓）
    acceptFrames_.store(false);
    if (tick_ != nullptr) tick_->stop();
    Gst* g = gst();
    //: ★ 第 77 轮：帧缓冲也要**排空并 unref** ✓（否则每换一次源就漏一串 buffer ✗）
    {
        QMutexLocker lock(&frameMutex_);
        while (!frameQ_.isEmpty()) {
            void* held = frameQ_.dequeue();
            if (held && g && g->buffer_unref) g->buffer_unref(held);
        }
    }
    //: ★ 诊断（第 69 轮）：`teardown()` 会**清空帧缓冲** ✓ ⇒ 如果它被反复调用（例如源地址每次都变 ✓），
    //:   就会表现为"帧到达很多、但缓冲总是空的" ✗ ⇒ 所以这里必须打点 ✓（带 `this=` ✓）。
    if (pipeline_ != nullptr)
        qInfo() << "[gstvideo] teardown this=" << this
                << "（清空帧缓冲；此前 handoff=" << handoffN_ << "paintGL=" << paintN_ << "✓）";
    if (g && pipeline_) {
        //: ★★ 第 78 轮（换流偶发 SIGSEGV ✗）：**先关掉 handoff** ✓ 再停管线 ——
        //:   否则"回调线程还在用 sink/buffer"与"GUI 线程拆管线"会撞上 ✗
        //:   （`set_state(NULL)` 会等流线程收工 ✓，但先把信号关了更稳妥 ✓）。
        if (sink_ && g->object_set) g->object_set(sink_, "signal-handoffs", 0, nullptr);
        g->element_set_state(pipeline_, GST_STATE_NULL);
        //: ★★★ 第 80 轮（**换流偶发崩溃的真凶** ✓）：`gst_element_set_state()` **是异步的** ✗
        //:   —— 它立刻返回（`GST_STATE_CHANGE_ASYNC` ✓），流线程还在拆 ✓；
        //:   我们却**马上 unref 管线** ✗ ⇒ 那些线程（多半阻塞在网络读里 ✓）稍后回来时
        //:   踩到**已释放的内存** ✓✓ ⇒ 崩在"几秒~几十秒之后" ✓✓✓（与实测"换流后 20~40 秒崩"吻合 ✓）。
        //:   ⇒ 必须 `gst_element_get_state(..., timeout)` **等它真的到 NULL** ✓ 再 unref ✓。
        if (g->element_get_state) {
            int st = 0, pend = 0;
            const int rc = g->element_get_state(pipeline_, &st, &pend, 500ull * 1000ull * 1000ull);  // 500ms ✓
            if (rc == 0 /* GST_STATE_CHANGE_FAILURE */ || pend != 1 /* 没到 NULL */)
                qWarning() << "[gstvideo] teardown：等 NULL 没等到（rc=" << rc << "state=" << st
                           << "pending=" << pend << "）⇒ 仍然继续 ✓";
        }
        //: ★ 修掉一个引用泄漏（第 72 轮顺手）：`sink_` 来自 `gst_bin_get_by_name` ⇒ **多一个引用** ✓，
        //:   以前只把指针置空、从不 unref ✗ ⇒ 每换一次源就漏一个 sink ✓（管线仍能正常销毁 ✓）。
        if (sink_ && g->object_unref) g->object_unref(sink_);
        if (bus_ && g->object_unref) g->object_unref(bus_);      // ★ 第 78 轮：bus 的引用还回去 ✓
        if (g->object_unref) g->object_unref(pipeline_);
    }
    pipeline_ = sink_ = nullptr;
    bus_ = nullptr;    // 引用已在上面还掉 ✓
    playing_ = false;
    drewOnce_ = false;
    //: ★★ 第 76 轮：**换流必须清 EGLImage 缓存** ✓（旧的 dmabuf 已关 ⇒ 缓存里的 EGLImage 是死引用 ✗
    //:   ⇒ 再绑上去就会"闪旧帧"甚至 SIGSEGV ✓ —— 板端实测崩过 3 次 ✓，用户也看到 GUI 短暂重启 ✓）
    {
        Gst* gg = gst();
        //: ⚠ 第 77 轮教训：这里**不要去调 `eglDestroyImageKHR`** ✗ ——
        //:   板端实测：退出/停服务那一刻销毁 EGLImage 会 **SIGSEGV** ✓（10:39:47 那次 SEGV
        //:   正好发生在 `systemctl stop` 的瞬间 ✓）——那时 EGL/GL 上下文可能已经在拆 ✗。
        //:   ⇒ 只**丢掉缓存引用**就够 ✓（image 由驱动在 display 销毁时回收 ✓，
        //:     每换一次源最多漏 8 个 ✓，可接受 ✓）；关键是**新流不会复用旧 image** ✓。
        Q_UNUSED(gg);
        if (g_fdCacheN > 0)
            qInfo() << "[gstvideo] 换流：丢弃 EGLImage 缓存" << g_fdCacheN << "个 ✓（不复用旧的 ✗）";
        g_fdCacheN = 0;
        g_capsW = g_capsH = 0;    // 尺寸要按新流重解析 ✓
    }
}

// ---------------------------------------------------------------- 管线
void GstVideoWidget::onHandoff(void*, void* buffer, void*, void* user) {
    auto* self = static_cast<GstVideoWidget*>(user);
    Gst* g = gst();
    if (!self || !g || !buffer) return;
    //: ★★★ 第 80 轮（换流偶发 SIGSEGV 的**头号嫌疑**）：换流时 `teardown()` 正在拆旧管线，
    //:   而 handoff 线程可能**还攥着旧管线的一帧** ✓ ⇒ 若把它放进缓冲 ✓，GUI 线程随后就会
    //:   拿它的 fd 去 `eglCreateImageKHR` ✗ —— 那条 dmabuf 已经随旧管线关掉了 ✗
    //:   ⇒ 驱动层拿一个**已关闭的 dmabuf** 当纹理 = SIGSEGV ✓✓（"换流前后崩一次"正好吻合 ✓）。
    //:   ⇒ 用 `acceptFrames_` 当**闸门** ✓：`teardown()` 一开始就关闸 ✓，`buildPipeline()` 建好再开 ✓。
    //:     `std::atomic<bool>` ⇒ 跨线程无锁且语义明确 ✓（上一次"无锁共享容器"的教训 ✗）。
    if (!self->acceptFrames_.load()) return;
    //: ★ 到达速率打点（每 120 帧一行 ✓，用来对照"到达 vs 绘制"）
    //:   ⚠ 计数**改成每实例**（原来是 `static` ✗ ⇒ 多实例时会把流量混起来 ✓），并打 `id=`/`self=` ✓
    ++self->handoffN_;    //: 回调被调用的次数 ✓
    //: ★ 到达"形状"打点（第 73 轮）：窗口内计数 ＋ 两次到达之间的最大间隔 ✓
    {
        const qint64 now = monoMs();
        if (self->lastArriveMs_ != 0) {
            const qint64 gap = now - self->lastArriveMs_;
            if (gap > self->maxGapMs_) self->maxGapMs_ = gap;
        }
        self->lastArriveMs_ = now;
        ++self->winArrivals_;
    }
    //: ★★ 第 77 轮（**用户要求**）：入**帧缓冲**，不再用单槽 ✗ ——
    //:   单槽"丢最旧"在到达一阵一阵时，同一波里只能留下最后一帧 ✗
    //:   ⇒ 画帧间隔忽长忽短 = 用户看到的"速度不稳" ✓。
    //:   小队列 + 丢最旧：波峰先存起来 ✓，GUI 线程按 25ms 拍子**按序**取 ✓ ⇒ 间隔稳定 ✓；
    //:   队列满则丢最旧（保延迟 ✓）并计数 ✓。
    //:   ⚠ 上一次崩溃的根因就是"跨线程无锁共享容器" ✗ ⇒ 这里**全程加锁** ✓。
    {
        QMutexLocker lock(&self->frameMutex_);
        //: ★★ 第 80 轮：**锁内二次确认** ✓ —— 闸门可能在"进函数时那次检查"之后被 `teardown()` 关上 ✓
        //:   （关闸与清缓冲都在同一把锁里 ✓）⇒ 这里再查一次，就不会把**旧管线的帧**放进新缓冲 ✗。
        if (!self->acceptFrames_.load()) return;   // 还没 ref 过 ⇒ 直接放手 ✓（由 GStreamer 回收 ✓）
        while (self->frameQ_.size() >= kFrameQMax) {
            void* dropped = self->frameQ_.dequeue();
            if (dropped && g->buffer_unref) g->buffer_unref(dropped);
            ++self->dropN_;
        }
        self->frameQ_.enqueue(g->buffer_ref(buffer));
    }
    ++self->storeN_;      //: ★ 真正"入队"的次数（与回调次数分开数 ⇒ 一眼看出"存没存进去"✓）
    if (self->storeN_ % 120 == 0) {
        int depth = 0;
        {
            QMutexLocker lock(&self->frameMutex_);
            depth = self->frameQ_.size();
        }
        qInfo() << "[gstvideo] handoff#" << self->handoffN_ << "存槽=" << self->storeN_
                << "id=" << self->id_ << "存活实例=" << g_liveWidgets
                << "缓冲深度=" << depth << "丢弃=" << self->dropN_
                << "（回调线程到达打点 ✓）";
    }
    //: ★★★ 第 75 轮板端实测（同一 65 秒窗口 ✓）——把"事件驱动"这条撤掉：
    //:   `事件=1889 == 回调=1889 == 存槽=1889` ✓ ⇒ 事件**确实**投出去、也**确实**被收到了 ✓，
    //:   但每次事件里的 `repaint()` **只有约 1/10 变成一次 `paintGL`** ✗
    //:   （事件 ~30/s ⇒ `paintGL` 只有 3.2/s ✗）；
    //:   而第 68 轮实测的"**QTimer 里调 repaint()**"是 **1:1**（40 拍 ⇒ 36 次出画 ✓✓）。
    //:   ⇒ 主驱动**回到 GUI 线程的 25ms 心跳** ✓（见 tickPaint ✓），这里**一个事件都不 post** ✓。
    //:   单槽 + 丢最旧照旧 ✓（它只负责"把最新一帧交给 GUI 线程" ✓）。
    //: ★★★ 帧率修复（2026-10-08）：这里过去还会 `invokeMethod(self, [self]{ repaint(); },
    //:   Qt::QueuedConnection)` ✗ —— 那正是"每帧一个普通优先级事件"的来源 ✓，
    //:   它把 Qt 的低优先级重绘请求**饿死**了 ✗（详见 .h 顶部与文档 §7.8 ✓）。
}

bool GstVideoWidget::buildPipeline(const QString& url) {
    Gst* g = gst();
    if (!g) { failed_ = true; return false; }
    teardown();
    //: ★★★ 第 75 轮板端实测定案（同一 65 秒窗口 ✓）——**这里从 `sync=false` 改成 `sync=true`**：
    //:   实测数据：`paintGL` 40 次/s ✓（定时器 1:1 ✓）、`回调/存槽` 20 次/s ✓（帧确实到 ✓），
    //:   但**只画出 2.8 帧/s** ✗、且 **40 拍里 37 拍单槽是空的** ✗、而每 120 帧的到达打点里
    //:   「**槽内旧帧 = true**」✓ ⇒ 解码器是**按块一口气吐**的（实测 `最大到达间隔 ≈600ms` ✓，
    //:   2 秒里来 40~70 帧 ✓）⇒ 同一波里的帧**在两次 25ms 拍子之间就被"丢最旧"扔掉了** ✗
    //:   ⇒ 每 25ms 最多只画 1 帧 ⇒ **忽快忽慢**（用户说的"一卡一卡" ✓）。
    //:   ⇒ 对策：让 sink **按实时时钟出帧**（`sync=true` ✓）⇒ 到达变成稳定的 ~24fps ✓
    //:   ⇒ 每 25ms 至多 1 帧 ⇒ **不再丢帧** ✓、`paintGL` 40 拍里该有 ~24 拍拿到帧 ✓（目标 ≥20 ✓）。
    //:   ⚠ 保留 `signal-handoffs=true` ✓（板上没有 `app` 插件 ⇒ 只能靠 handoff 拿 buffer ✓）。
    const QByteArray desc =
        "souphttpsrc location=" + url.toUtf8() +
        " ! tsdemux ! h264parse ! mppvideodec ! video/x-raw(memory:DMABuf)"
        " ! fakesink name=sink signal-handoffs=true sync=true";
    void* err = nullptr;
    pipeline_ = g->parse_launch(desc.constData(), &err);
    if (!pipeline_) { qWarning() << "[gstvideo] 管线构造失败"; failed_ = true; return false; }
    sink_ = g->bin_get_by_name(pipeline_, "sink");
    if (!sink_) { qWarning() << "[gstvideo] 找不到 fakesink(name=sink)"; failed_ = true; return false; }
    g->object_set(sink_, "signal-handoffs", 1, "sync", 1, nullptr);
    g->signal_connect(sink_, "handoff", (void*)&GstVideoWidget::onHandoff, this, nullptr, 0);
    //: ★ 第 78 轮：bus **只取一次** ✓（`gst_element_get_bus` 会多一个引用 ⇒ 必须还 ✓，
    //:   否则就是"每拍泄漏一个引用 ⇒ 几分钟崩一次"那个真因 ✗）
    if (g->element_get_bus) bus_ = g->element_get_bus(pipeline_);
    //: ★ 第 80 轮：管线建好、handoff 已接上 ⇒ **开闸** ✓（与 `teardown()` 里的关闸成对 ✓）
    acceptFrames_.store(true);
    if (tick_ != nullptr) tick_->start();
    return true;
}

bool GstVideoWidget::setSource(const QString& url) {
    source_ = url;
    qInfo() << "[gstvideo] setSource this=" << this << "url=" << (url.isEmpty() ? QStringLiteral("(清空)") : url);
    if (url.isEmpty()) { teardown(); update(); return true; }
    failed_ = false; eosSent_ = false; zeroCopyFrames_ = 0; framesShown_ = 0;
    if (!buildPipeline(url)) { emit failedOver(); return false; }
    return true;  // 真正的 PLAYING 在 initializeGL 之后（需要 GL 上下文）
}

void GstVideoWidget::initializeGL() {
    qInfo() << "[gstvideo] initializeGL 被调用 ✓（GL 上下文已建立/重建）context="
            << (QOpenGLContext::currentContext() != nullptr);
    //: ★★★ 第 77 轮崩溃修复：**上下文可能是重建的**（板端实测 `initializeGL` 出现 4 次 ✓）⇒
    //:   旧的纹理/程序/vbo/EGLImage 全部失效 ✗ ⇒ 一律清零、下次重建 ✓（否则偶发 SIGSEGV ✓）
    resetGlStateForNewContext();
    drewOnce_ = false;
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

//: ★★★ 25ms 心跳（GUI 线程）：**唯一**的重绘驱动 ✓ —— 这是第 75 轮实测选出来的：
//:   **QTimer 里调 `repaint()` 是 1:1**（40 拍 ⇒ 36 次 `paintGL` ✓✓ 第 68 轮实测 ✓），
//:   而"帧到达投事件 + 事件里 repaint()"只有 **1:10** ✗（第 75 轮实测 ✗）。
//:   ⇒ 所以**无条件** repaint ✓（**不看**槽里有没有帧 ✗ —— 到达是"一阵一阵"的
//:   （实测 `最大到达间隔 ≈1.25 秒` ✓），按"有没有帧"当门会把绝大多数拍直接枪掉 ✗）。
//:   没新帧时由 `paintGL` 自己"什么都不做" ✓（`PartialUpdate` 保住上一帧 ✓，不闪 ✓）。
void GstVideoWidget::tickPaint() {
    ++tickN_;
    //: ★ 诊断（每 2 秒一行，80×25ms ✓）：一眼看出**定时器有没有在跑**、门条件卡在哪 ✓
    //:   带上 `this=` ⇒ 多实例时不会看串 ✓
    if (tickN_ % 80 == 0) {
        int depth = 0;
        {
            QMutexLocker lock(&frameMutex_);
            depth = frameQ_.size();
        }
        qInfo() << "[gstvideo] 心跳#" << tickN_ << "id=" << id_ << "this=" << this
                << "存活实例=" << g_liveWidgets
                << "pipeline=" << (pipeline_ != nullptr)
                << "playing=" << playing_ << "缓冲深度=" << depth
                << "已画帧=" << framesShown_ << "零拷贝=" << zeroCopyFrames_
                << "paintGL=" << paintN_ << "回调=" << handoffN_ << "存槽=" << storeN_
                << "丢弃=" << dropN_
                << "早退(无帧/无内存/非dmabuf)=" << retNoBuf_ << "/" << retNoMem_ << "/" << retNotDmabuf_
                << "窗口到达=" << winArrivals_ << "最大到达间隔(ms)=" << maxGapMs_
                << "距上次到达(ms)=" << (lastArriveMs_ ? (monoMs() - lastArriveMs_) : -1);
        winArrivals_ = 0;    // 窗口滚动 ✓（handoff 线程同时在写 ⇒ 诊断用，允许良性竞争 ✓）
        maxGapMs_ = 0;
    }
    if (pipeline_ == nullptr) return;
    pumpBus();                                        // EOS/错误照常上报 ✓（与有没有新帧无关 ✓）
    if (!playing_) return;
    //: ★★ 第 78 轮（用户要求"稳定帧数" ✓）：**固定的画帧节拍** —— 每 ~30ms 才请求一次重绘 ✓
    //:   ⇒ 显示节拍 = 30fps 左右，**不跟着上游猛灌一起快跑** ✓（这就是"倍速播放"的根治 ✓：
    //:     上游按块灌时，多出来的帧在 `paintGL` 里被"只取最新一帧"丢掉 ✓，
    //:     显示速度由**我们**定 ✓，而不是由到达速度定 ✓）。
    {
        const qint64 nowMs = monoMs();
        if (lastPaintMs_ != 0 && nowMs - lastPaintMs_ < 33) return;   // 33ms ⇒ 最多 30fps ✓
        lastPaintMs_ = nowMs;
    }
    repaint();                                        // ★ 无条件 ✓（见上面的实测依据 ✓）
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
    //: ★★★ 第 78 轮崩溃真因（**引用泄漏**）：原来这里**每一拍都 `gst_element_get_bus()`**
    //:   却**从不 unref** ✗ ⇒ 40 拍/秒 × 几分钟 = 泄漏上万个 bus 引用 ✓
    //:   ⇒ 内存/对象压力越来越大 ⇒ **几分钟崩一次** ✓✓（与实测"每 4~6 分钟一次 SEGV"吻合 ✓）。
    //:   ⇒ 改成**建管线时取一次、teardown 时还回去** ✓（见 buildPipeline / teardown ✓）。
    void* bus = bus_;
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

    //: ★ 取帧：**只在这里（GUI 线程）动这个缓冲** ✓ —— 第 77/78 轮策略：
    //:   **把积压全丢掉、只取最新一帧** ✓ —— 这样
    //:     ① 画帧节拍由我们自己的定时器决定 ✓（稳定 ✓，不跟着到达快跑 ⇒ **不会"倍速"** ✓）
    //:     ② 延迟最小 ✓（旧帧不排队 ✓）
    //:     ③ 上游按块猛灌时，丢的是"我们没来得及显示的"，而不是把内容加速播完 ✓
    void* buf = nullptr;
    int qDepth = 0;
    if (acceptFrames_.load()) {          // ★ 第 80 轮：闸门关着时**不画**（不碰已作废的 dmabuf ✓）
        QMutexLocker lock(&frameMutex_);
        while (frameQ_.size() > 1) {
            void* dropped = frameQ_.dequeue();
            if (dropped && g && g->buffer_unref) g->buffer_unref(dropped);
            ++dropN_;
        }
        if (!frameQ_.isEmpty()) buf = frameQ_.dequeue();
        qDepth = frameQ_.size();
    }
    const int pendingCount = (buf != nullptr) ? 1 : 0;
    ++paintN_;

    if (!g || buf == nullptr) {
        //: ★ `PartialUpdate` 下**不能随便清屏** ✗（那会把上一帧擦掉 ⇒ 闪 ✓）：
        //:   只有"这一帧还没画过"（首帧 / 刚 resize ✓）才清黑 ✓，其余情况**原样保留** ✓。
        ++retNoBuf_;
        if (!drewOnce_) { gl->glClearColor(0, 0, 0, 1); gl->glClear(GL_COLOR_BUFFER_BIT); }
        return;
    }
    void* mem = g->buffer_peek_memory(buf, 0);
    if (!mem) {
        ++retNoMem_;
        g->buffer_unref(buf);
        if (!drewOnce_) { gl->glClearColor(0, 0, 0, 1); gl->glClear(GL_COLOR_BUFFER_BIT); }
        return;
    }
    if (!g->is_dmabuf_memory(mem)) {
        ++retNotDmabuf_;
        g->buffer_unref(buf);
        if (!drewOnce_) { gl->glClearColor(0, 0, 0, 1); gl->glClear(GL_COLOR_BUFFER_BIT); }
        return;
    }
    const int fd = g->dmabuf_get_fd(mem);
    //: ★★★ 第 76 轮：几何**不再硬编码**（用户实测"队列第二个视频有绿条" ⇒ 640x368 只对第一路成立 ✗）
    //:   ① 宽高从 **sink pad 的 caps** 解析 ✓（`width=(int)` / `height=(int)` ✓）
    //:   ② 步长：本板 MPP 实测 `stride == width` ✓
    //:   ③ **对齐高度从"内存大小"反推** ✓：NV12 单 dmabuf 的 size = stride × alignedH × 3/2 ✓
    //:      —— 这正好能自愈"不同分辨率/不同对齐"的绿条 ✗（360⇒368 ✓、720⇒720 ✓、1080⇒1088 … ✓）
    if (g_capsW <= 0 && g->element_get_static_pad && g->pad_get_current_caps && g->caps_to_string) {
        void* pad = g->element_get_static_pad(sink_, "sink");
        void* caps = pad ? g->pad_get_current_caps(pad) : nullptr;
        char* s = caps ? g->caps_to_string(caps) : nullptr;
        if (s) {
            //: ⚠ 第 76 轮踩过的坑（板端实测 40x60 ✗ ⇒ 画面直接黑 ✗）：
            //:   `"width=(int)"` 是 **11** 个字符 ✓，我一开始写了 +12 ✗ ⇒ 正好把首位数字吃掉
            //:   （640 ⇒ 40 ✓、360 ⇒ 60 ✓）⇒ 这里**必须用 strlen** ✓，别再手数 ✗。
            static const char* kW = "width=(int)";
            static const char* kH = "height=(int)";
            const char* pw = strstr(s, kW);
            const char* ph = strstr(s, kH);
            if (pw) g_capsW = atoi(pw + strlen(kW));
            if (ph) g_capsH = atoi(ph + strlen(kH));
            qInfo() << "[gstvideo] caps =" << s;
            qInfo() << "[gstvideo] caps 尺寸 =" << g_capsW << "x" << g_capsH;
            if (g->free_) g->free_(s);
        }
        if (caps && g->caps_unref) g->caps_unref(caps);
    }
    //: ★ 合理性兜底（第 76 轮）：解析失败/离谱时**不能拿垃圾值去建纹理** ✗（否则就是黑屏 ✗）
    if (g_capsW < 64 || g_capsW > 8192 || g_capsH < 64 || g_capsH > 8192) {
        if (g_capsW > 0) qWarning() << "[gstvideo] caps 尺寸不合理 ⇒ 回退 640x360：" << g_capsW << "x" << g_capsH;
        g_capsW = 640; g_capsH = 360;
    }
    const int W = g_capsW;       // 视频**可见**宽 ✓
    const int Hv = g_capsH;      // 视频**可见**高 ✓
    const int stride = W;
    int alignedH = Hv;
    if (g->memory_get_sizes) {
        const unsigned long sz = g->memory_get_sizes(mem, nullptr, nullptr);
        if (sz > 0) {
            const int a = int(sz * 2 / (3 * static_cast<unsigned long>(stride)));
            if (a >= Hv && a <= Hv + 64) alignedH = a;    // 合理范围才采纳 ✓（否则用可见高 ✓）
        }
    }
    if (alignedH & 1) ++alignedH;                 // NV12 的 UV 平面按偶数 ✓
    const int uvOff = stride * alignedH;
    void* dpy = g->eglGetCurrentDisplay();
    const int attrs[] = {
        EGL_WIDTH_, W, EGL_HEIGHT_, alignedH,
        EGL_LINUX_DRM_FOURCC_EXT_, static_cast<int>(FOURCC_NV12_),
        EGL_DMA_BUF_PLANE0_FD_EXT_, fd, EGL_DMA_BUF_PLANE0_OFFSET_EXT_, 0, EGL_DMA_BUF_PLANE0_PITCH_EXT_, stride,
        EGL_DMA_BUF_PLANE1_FD_EXT_, fd, EGL_DMA_BUF_PLANE1_OFFSET_EXT_, uvOff, EGL_DMA_BUF_PLANE1_PITCH_EXT_, stride,
        EGL_NONE_
    };
    //: ★ 按 fd 缓存 `EGLImage` ✓（同一条流内 ✓）：板端实测 fd 只在少数值间轮换（51/54/63/67… ✓）
    //:   ⇒ 同一个 fd 再来就直接复用 ✓，不再每帧 create/destroy ✗
    //:   ⚠ 缓存**跨流必须清** ✗（见 `teardown()` ✓ —— 否则就是"闪旧帧/SIGSEGV" ✓）
    void* s_img = nullptr;
    for (int i = 0; i < g_fdCacheN; ++i) {
        if (g_fdCache[i].fd == fd) { s_img = g_fdCache[i].img; break; }
    }
    if (s_img == nullptr) {
        s_img = g->eglCreateImageKHR(dpy, nullptr, EGL_LINUX_DMA_BUF_EXT_, nullptr, attrs);
        if (s_img == nullptr) {
            static bool warned = false;
            if (!warned) {
                warned = true;
                //: 失败时把**真实入参**打出来 ✓（否则只剩"失败"两个字，没法定位 ✗）
                qWarning() << "[gstvideo] eglCreateImageKHR 失败：W=" << W << "H=" << alignedH
                           << "stride=" << stride << "uvOff=" << uvOff << "fd=" << fd;
            }
            g->buffer_unref(buf);
            if (!drewOnce_) { gl->glClearColor(0.1f, 0, 0, 1); gl->glClear(GL_COLOR_BUFFER_BIT); }
            return;
        }
        if (g_fdCacheN < 8) { g_fdCache[g_fdCacheN].fd = fd; g_fdCache[g_fdCacheN].img = s_img; ++g_fdCacheN; }
        static int made = 0;
        if (++made <= 5) qInfo() << "[gstvideo] 新建 EGLImage（fd=" << fd << "，缓存 " << g_fdCacheN << " 个）";
    }
    if (g_tex == 0) {
        gl->glGenTextures(1, &g_tex);
    }
    gl->glBindTexture(GL_TEXTURE_EXTERNAL_OES_, g_tex);
    gl->glTexParameteri(GL_TEXTURE_EXTERNAL_OES_, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    gl->glTexParameteri(GL_TEXTURE_EXTERNAL_OES_, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    g->glEGLImageTargetTexture2DOES(GL_TEXTURE_EXTERNAL_OES_, s_img);
    if (gl->glGetError() == GL_NO_ERROR) { zeroCopyFrames_++; }
    framesShown_++;
    drewOnce_ = true;
    g->buffer_unref(buf);

    // ---- 最小 GLES2 着色器：外部纹理 ⇒ 四边形 ✓
    //:   ★ 第 76 轮加 `uvY`：缓冲区高度是**对齐后**的（alignedH ✓），可见内容只占上面 Hv 行 ✓
    //:   ⇒ uv.y 只映射到 `Hv/alignedH` ✓（配合 Y 翻转 ✓ ⇒ 对齐出来的填充行**永不被采样** ✓，
    //:     这也顺手把"最后几行脏数据"挡在外面 ✓）。
    //:   ⚠ 第 77 轮：`prog`/`vbo` 改成**文件作用域** ✓ ⇒ 上下文重建时会被清零 ✓（见 initializeGL ✓）
    if (!g_prog) {
        static const char* VS =
            "attribute vec2 p; varying vec2 uv; uniform float uvY;"
            "void main(){ uv = vec2((p.x+1.0)*0.5, (1.0-(p.y+1.0)*0.5)*uvY); gl_Position = vec4(p,0.0,1.0); }";
        static const char* FS =
            "#extension GL_OES_EGL_image_external : require\n"
            "precision mediump float; varying vec2 uv; uniform samplerExternalOES tex;"
            "void main(){ gl_FragColor = texture2D(tex, uv); }";
        GLuint vs = gl->glCreateShader(GL_VERTEX_SHADER);
        gl->glShaderSource(vs, 1, &VS, nullptr); gl->glCompileShader(vs);
        GLuint fs = gl->glCreateShader(GL_FRAGMENT_SHADER);
        gl->glShaderSource(fs, 1, &FS, nullptr); gl->glCompileShader(fs);
        g_prog = gl->glCreateProgram();
        gl->glAttachShader(g_prog, vs); gl->glAttachShader(g_prog, fs); gl->glLinkProgram(g_prog);
        GLint ok = 0; gl->glGetProgramiv(g_prog, GL_LINK_STATUS, &ok);
        static const GLfloat quad[] = {-1.f, -1.f, 1.f, -1.f, -1.f, 1.f, 1.f, 1.f};
        gl->glGenBuffers(1, &g_vbo); gl->glBindBuffer(GL_ARRAY_BUFFER, g_vbo);
        gl->glBufferData(GL_ARRAY_BUFFER, sizeof(quad), quad, GL_STATIC_DRAW);
        qInfo() << "[gstvideo] 着色器程序就绪 link=" << ok;
    }
    //: ★★ 不拉伸（第 76 轮，用户要求 ✓）：按视频宽高比取**最大内接矩形**、居中 ✓（上下/左右留黑 ✓）
    {
        const double aspect = double(W) / double(Hv);
        int vw = width();
        int vh = int(double(width()) / aspect);
        if (vh > height()) {
            vh = height();
            vw = int(double(height()) * aspect);
        }
        if (vw < 1) vw = 1;
        if (vh < 1) vh = 1;
        gl->glViewport((width() - vw) / 2, (height() - vh) / 2, vw, vh);
    }
    gl->glClearColor(0, 0, 0, 1); gl->glClear(GL_COLOR_BUFFER_BIT);
    gl->glUseProgram(g_prog);
    gl->glActiveTexture(GL_TEXTURE0);
    gl->glBindTexture(GL_TEXTURE_EXTERNAL_OES_, g_tex);
    gl->glUniform1i(gl->glGetUniformLocation(g_prog, "tex"), 0);
    gl->glUniform1f(gl->glGetUniformLocation(g_prog, "uvY"), float(Hv) / float(alignedH));
    const GLint ap = gl->glGetAttribLocation(g_prog, "p");
    gl->glBindBuffer(GL_ARRAY_BUFFER, g_vbo);
    gl->glEnableVertexAttribArray(ap);
    gl->glVertexAttribPointer(ap, 2, GL_FLOAT, GL_FALSE, 0, nullptr);
    gl->glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
    gl->glDisableVertexAttribArray(ap);

    //: ★★ 画帧间隔打点（**用户要求"检查画帧间隔是否正确"** ✓）：
    //:   每 120 次**真正绘制**打印平均/最大间隔 ✓ —— 稳定的话平均应 ≈ 1000/帧率 ✓。
    {
        const qint64 nowMs = monoMs();
        if (lastDrawMs_ != 0) {
            const qint64 gap = nowMs - lastDrawMs_;
            drawGapSumMs_ += gap;
            ++drawGapN_;
            if (gap > drawGapMaxMs_) drawGapMaxMs_ = gap;
        }
        lastDrawMs_ = nowMs;
    }

    //: ★ 验收打点：每 120 次绘制一行 ✓。
    //:   ⚠ 上一版这里有个算错的地方 ✗：额外加了"paintN_<=20 也打点"⇒ 前 20 帧会拿固定 120 当分子，
    //:   算出 `fps=5000.0` 这种假值 ✗。现在**只在 120 的整数倍打点** ✓ ⇒ 分子分母都是真实的 ✓。
    const int kEvery = 120;
    if (paintN_ <= 3)
        qInfo() << "[gstvideo] 首次绘制 id=" << id_ << "this=" << this << "paintGL=" << paintN_
                << "size=" << width() << "x" << height();
    if (paintN_ % kEvery == 0) {
        if (!fpsClk_.isValid()) fpsClk_.start();
        const qint64 el = fpsClk_.restart();
        const double fps = (el > 0) ? (1000.0 * kEvery / double(el)) : 0.0;
        const double avgGap = (drawGapN_ > 0) ? double(drawGapSumMs_) / double(drawGapN_) : 0.0;
        qInfo() << "[gstvideo] 绘制帧率 fps=" << QString::number(fps, 'f', 1)
                << "（每" << kEvery << "次绘制 " << el << "ms）id=" << id_ << "存活实例=" << g_liveWidgets
                << "内部耗时(ms)="
                << QString::number(1000.0 * double(clock() - pt_t0) / double(CLOCKS_PER_SEC), 'f', 2)
                << "零拷贝帧=" << zeroCopyFrames_ << "pending=" << pendingCount
                << "视频=" << W << "x" << Hv << "对齐高=" << alignedH
                << "平均画帧间隔(ms)=" << QString::number(avgGap, 'f', 1)
                << "最大画帧间隔(ms)=" << drawGapMaxMs_
                << "缓冲深度=" << qDepth << "丢弃=" << dropN_;
        drawGapSumMs_ = 0; drawGapMaxMs_ = 0; drawGapN_ = 0;
    }
}
