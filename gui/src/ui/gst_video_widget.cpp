// gst_video_widget.cpp — 见 .h 的说明。要点：dlopen 取 GStreamer/EGL；handoff 只入队；
// paintGL 里 eglCreateImageKHR(plane0+plane1) ⇒ glEGLImageTargetTexture2DOES ⇒ 画。
#include "gst_video_widget.h"

#include <QDebug>
#include <QOpenGLContext>
#include <QOpenGLFunctions>
#include <QTimer>
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
    int (*bus_pop_filtered)(void*, int, unsigned) = nullptr;
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
    t.is_dmabuf_memory = (decltype(t.is_dmabuf_memory))S(t.hGst, "gst_is_dmabuf_memory");
    t.dmabuf_get_fd = (decltype(t.dmabuf_get_fd))S(t.hGst, "gst_dmabuf_memory_get_fd");
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
    t.glEGLImageTargetTexture2DOES =
        (decltype(t.glEGLImageTargetTexture2DOES))t.eglGetProcAddress("glEGLImageTargetTexture2DOES");
    if (!t.eglCreateImageKHR || !t.glEGLImageTargetTexture2DOES) {
        qWarning() << "[gstvideo] 缺 EGL dmabuf 扩展 ⇒ 回退"; return nullptr;
    }
    t.init(nullptr, nullptr);
    return &t;
}

// ---------------------------------------------------------------- 生命周期
GstVideoWidget::GstVideoWidget(QWidget* parent) : QOpenGLWidget(parent) {
    setAttribute(Qt::WA_OpaquePaintEvent);
    setAutoFillBackground(false);
    qInfo() << "[gstvideo] 控件建立（目标：零拷贝 dmabuf ⇒ Qt EGL）";
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
    self->queue_.enqueue(g->buffer_ref(buffer));
    while (self->queue_.size() > 3) g->buffer_unref(self->queue_.dequeue());  // 满了丢最旧
    QMetaObject::invokeMethod(self, "update", Qt::QueuedConnection);         // 触发重绘
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
    pumpBus();
    QOpenGLFunctions* gl = QOpenGLContext::currentContext()->functions();
    Gst* g = gst();

    if (!g || queue_.isEmpty()) { gl->glClearColor(0, 0, 0, 1); gl->glClear(GL_COLOR_BUFFER_BIT); return; }

    void* buf = queue_.dequeue();
    void* mem = g->buffer_peek_memory(buf, 0);
    if (!mem || !g->is_dmabuf_memory(mem)) {
        g->buffer_unref(buf);
        gl->glClearColor(0, 0, 0, 1); gl->glClear(GL_COLOR_BUFFER_BIT); return;
    }
    const int fd = g->dmabuf_get_fd(mem);
    // 尺寸/stride：NV12 且本板实测 stride==width、plane1 offset==stride*height
    const int W = width(), H = height();
    const int stride = W, uvOff = stride * H;
    void* dpy = g->eglGetCurrentDisplay();
    const int attrs[] = {
        EGL_WIDTH_, W, EGL_HEIGHT_, H,
        EGL_LINUX_DRM_FOURCC_EXT_, static_cast<int>(FOURCC_NV12_),
        EGL_DMA_BUF_PLANE0_FD_EXT_, fd, EGL_DMA_BUF_PLANE0_OFFSET_EXT_, 0, EGL_DMA_BUF_PLANE0_PITCH_EXT_, stride,
        EGL_DMA_BUF_PLANE1_FD_EXT_, fd, EGL_DMA_BUF_PLANE1_OFFSET_EXT_, uvOff, EGL_DMA_BUF_PLANE1_PITCH_EXT_, stride,
        EGL_NONE_
    };
    void* img = g->eglCreateImageKHR(dpy, nullptr, EGL_LINUX_DMA_BUF_EXT_, nullptr, attrs);
    if (!img) {
        static bool warned = false;
        if (!warned) { warned = true; qWarning() << "[gstvideo] eglCreateImageKHR 失败（plane1/尺寸）"; }
        g->buffer_unref(buf);
        gl->glClearColor(0.1f, 0, 0, 1); gl->glClear(GL_COLOR_BUFFER_BIT); return;
    }
    GLuint tex = 0;
    gl->glGenTextures(1, &tex);
    gl->glBindTexture(GL_TEXTURE_EXTERNAL_OES_, tex);
    gl->glTexParameteri(GL_TEXTURE_EXTERNAL_OES_, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    gl->glTexParameteri(GL_TEXTURE_EXTERNAL_OES_, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    g->glEGLImageTargetTexture2DOES(GL_TEXTURE_EXTERNAL_OES_, img);
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
    gl->glBindTexture(GL_TEXTURE_EXTERNAL_OES_, tex);
    gl->glUniform1i(gl->glGetUniformLocation(prog, "tex"), 0);
    const GLint ap = gl->glGetAttribLocation(prog, "p");
    gl->glBindBuffer(GL_ARRAY_BUFFER, vbo);
    gl->glEnableVertexAttribArray(ap);
    gl->glVertexAttribPointer(ap, 2, GL_FLOAT, GL_FALSE, 0, nullptr);
    gl->glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
    gl->glDisableVertexAttribArray(ap);
    gl->glDeleteTextures(1, &tex);
    if (framesShown_ == 1 || framesShown_ % 60 == 0)
        qInfo() << "[gstvideo] 帧" << framesShown_ << "零拷贝成功" << zeroCopyFrames_;
}
