// ============================================================================
//  gui/src/ui/gst_video_widget.h — dmabuf 零拷贝视频控件（QOpenGLWidget）
//
//  为什么存在（板端实测，见 docs/dmabuf-zero-copy-findings.md）：
//    · QtMultimedia 的 QVideoWidget 走 GStreamer 的 `vqueue`（map + YUV→RGB + 拷贝）
//      ⇒ 实测 `vqueue:src` 吃 0.82~0.94 核（占整个视频路径的 82%）。
//    · 把 mppvideodec 的 **dmabuf** 在 **Qt 自己的 EGL 上下文**里导入成纹理 ⇒ 零拷贝。
//      原型已在板上证实：210/210 帧 `✅ 零拷贝导入成纹理 OK`。
//    · ★ 关键（缺一个就失败）：① handoff 回调线程里必须 makeCurrent；
//      ② NV12 必须同时给 plane1，否则 EGL_BAD_PARAMETER(0x300c)；
//      ③ **NV12 的 UV 平面偏移必须用 16 对齐后的高度**（360 ⇒ 368），否则顶部一条绿条。
//    · ★ 不需要 GStreamer 的 GL（glupload/qmlglsink 那条路在本板 eglfs 下死路）。
//
//  ⚠ GStreamer 与 EGL-EXT 全部 **dlopen / 运行时解析**：这样 gui_widgets 的 20 个宿主侧
//     测试不会因为链接 GStreamer 而失败（宿主上没有 GStreamer）。
//     链接期只需要 -ldl。任何一步失败 ⇒ lastOpFailed() == true ⇒ 调用方回退到 QMediaPlayer。
//
//  ★★★ 帧率（2026-10-08 定案，第 68 轮实测 33.6~35.6fps）：
//    `paintGL` 一直只要 ~1ms ✓，slow 的是**"谁来触发绘制"** ✗ ——
//    Qt 给 render-to-texture 控件（= `QOpenGLWidget`）安排的"重绘请求"是一个
//    **`Qt::LowEventPriority` 事件**（`QWidgetRepaintManager::sendUpdateRequest` ⇒
//    `postEvent(..., Qt::LowEventPriority)`）⇒ **只要事件队列里还有任何普通事件，它就被饿死** ✗。
//    而 handoff 线程原本**每帧 post 一个 queued 调用**（24 个/s 的普通事件 ✓）⇒
//    队列永远不空 ⇒ 实测 20 请求/s 只换来 **0.8 次 paint**/s ✗（= 历史上的 2~3fps ✓）。
//    ⇒ 修法两条（都必需 ✓）：
//      ① handoff 里**只做原子换手**（单槽、丢最旧 ✓），**不再 post 任何事件** ✗；
//      ② 由 **GUI 线程自己的 25ms `QTimer`** 主动 `repaint()` ✓ ⇒ 两次 tick 之间队列是空的 ✓
//         ⇒ 低优先级的 UpdateRequest 立刻被处理 ✓ ⇒ 实测 `paintGL` **33.6~35.6/s**、
//         窗口合成 **39~42/s** ✓，且 `agent_gui` CPU 仍只 ~9% ✓。
//
//  职责边界：只负责"把一条本机 HTTP 流零拷贝显示出来 + 报位置/时长/在不在放"。
//    音频、队列、控制条、预览栏等一律仍由 VideoPanel 负责（保持既有语义）。
// ============================================================================
#pragma once

#include <QAtomicPointer>
#include <QByteArray>
#include <QElapsedTimer>
#include <QImage>
#include <QMutex>
#include <QOpenGLWidget>
#include <QQueue>
#include <QString>

#include <atomic>

class QTimer;

class GstVideoWidget : public QOpenGLWidget {
    Q_OBJECT

public:
    explicit GstVideoWidget(QWidget* parent = nullptr);
    ~GstVideoWidget() override;

    /// 设置流地址（http://127.0.0.1:8765/stream/...）。空串 = 停止并清空。
    /// @return false = 起不来（调用方应回退到 QMediaPlayer 路径）
    bool setSource(const QString& url);
    QString source() const { return source_; }

    void play();
    void pause();
    void togglePlayPause();
    bool isPlaying() const { return playing_; }

    qint64 positionMs() const;
    qint64 durationMs() const;

    /// 最近一次操作是否失败（失败 ⇒ 调用方回退）
    bool lastOpFailed() const { return failed_; }
    /// 是否已经成功出过至少一帧（验收/日志用）
    int framesShown() const { return framesShown_; }
    /// 零拷贝导入成功的帧数（验收用；=0 说明没走成 dmabuf 路径）
    int zeroCopyFrames() const { return zeroCopyFrames_; }

signals:
    void playingChanged(bool playing);
    /// 放完了（对应原 QMediaPlayer::EndOfMedia 那条路）
    void endOfMedia();
    /// 起不来/中途致命错（调用方据此回退）
    void failedOver();

protected:
    void initializeGL() override;
    void paintGL() override;
    void resizeGL(int w, int h) override;

private:
    // ---- GStreamer 运行时（dlopen 出来的函数指针）----
    struct Gst;                     ///< 函数指针表（定义在 .cpp）
    static Gst* gst();              ///< 懒加载；失败返回 nullptr

    /// ★ 25ms 心跳：**GUI 线程**的唯一重绘驱动（第 75 轮实测：QTimer 里 repaint 是 1:1 ✓）。
    ///   **无条件** repaint ✓；没新帧时 `paintGL` 直接返回 ✓（`PartialUpdate` 保住上一帧 ✓）。
    void tickPaint();

    void teardown();
    bool buildPipeline(const QString& url);
    void pumpBus();                 ///< 处理 EOS/ERROR（在 paintGL 里顺带调）
    static void onHandoff(void* fakesink, void* buffer, void* pad, void* user);

    QByteArray sourceBytes_;
    QString source_;
    void* pipeline_ = nullptr;      ///< GstElement*
    void* sink_ = nullptr;          ///< GstElement*
    void* bus_ = nullptr;           ///< GstBus（**建管线时取一次 ✓、teardown 还回去 ✓** ——
                                    ///< 以前每拍 `element_get_bus` 且从不 unref ⇒ 每拍泄漏 ⇒ 几分钟崩一次 ✗）
    //: ★★ 帧缓冲（第 77 轮，**用户要求** ✓）：单槽"丢最旧"在到达一阵一阵时会让同一波里只剩一帧 ✗
    //:   ⇒ 画帧间隔忽长忽短（用户看到的"速度不稳"✓）。换成**小队列**：
    //:     · handoff 线程：入队；满了丢**最旧**（保延迟 ✓）并计数 ✓
    //:     · GUI 线程：每个 25ms 拍子**按序**取一帧 ✓ ⇒ **画帧间隔稳定** ✓
    //:     · 队列本身也是**背压** ✓（存满就让解码器等 ✓）
    ///   `frameMutex_` 是跨线程访问它的唯一保护 ✓（上次崩溃就是"无锁共享容器"✗，别再犯 ✓）
    QMutex frameMutex_;
    QQueue<void*> frameQ_;
    /// ★★ 第 80 轮：**收帧闸门** ✓ —— `teardown()` 一进门就关 ✓，管线建好才开 ✓。
    ///   没有它，handoff 线程会把"旧管线的最后一帧"塞进缓冲 ✗，而那条 dmabuf 已经
    ///   随旧管线关掉 ⇒ GUI 线程拿它去建 EGLImage = **驱动层 SIGSEGV** ✓（换流崩溃的头号嫌疑 ✓）。
    std::atomic<bool> acceptFrames_{false};
    static const int kFrameQMax = 8;   ///< 缓冲深度（8 帧 ≈ 0.27 秒 @30fps ✓，够抹平波峰 ✓）
    //: ★ 画帧间隔打点（用户要求"检查画帧间隔是否正确" ✓）
    qint64 lastDrawMs_ = 0;
    qint64 drawGapSumMs_ = 0;
    qint64 drawGapMaxMs_ = 0;
    int drawGapN_ = 0;
    int dropN_ = 0;                 ///< 因"只取最新一帧"或缓冲满而丢掉的帧数 ✓（诊断）
    qint64 lastPaintMs_ = 0;        ///< 上一拍重绘的时刻 ✓（固定 ~30ms 画帧节拍 ✓，防"倍速" ✓）
    QTimer* tick_ = nullptr;        ///< 25ms 重绘心跳（**只有它**在驱动 paintGL ✓）
    //: ★★ 诊断（2026-10-08 第 69 轮）：计数器**必须是每实例的** ✗ ——
    //:   上一轮把它们写成 `static` ✓，而当时同时存在"帧到达 27/s"与"心跳里槽是空的"这对矛盾 ✓，
    //:   若进程里其实有**两个** `GstVideoWidget`（例如 GAME 模式重建过页面栈 ✓），
    //:   `static` 计数就会把两个实例的流量**混在一起** ⇒ 正好能掩盖这种矛盾 ✓。
    //:   ⇒ 现在每实例各数各的 ✓，并且每条日志都打 `this=` ✓ ⇒ 一次上板即可判定 ✓。
    int handoffN_ = 0;              ///< 本实例收到的 handoff 次数
    int storeN_ = 0;                ///< 本实例**真正写进单槽**的次数（与 handoffN_ 对照 ✓）
    int id_ = 0;                    ///< 实例编号（日志里比裸 `this` 好认 ✓）
    int tickN_ = 0;                 ///< 本实例的心跳次数
    int paintN_ = 0;                ///< 本实例的 paintGL 次数
    int retNoBuf_ = 0;              ///< 静默早退①：没有新帧（槽是空的）
    int retNoMem_ = 0;              ///< 静默早退②：peek_memory 为空
    int retNotDmabuf_ = 0;          ///< 静默早退③：不是 dmabuf 内存
    //: ★ 诊断（第 73 轮，**第 75 轮实测已证实**）：**到达的"形状"** ✓ —— "平均 26/s" 掩盖了
    //:   "一阵一阵" ✗：板端实测 2 秒里来 56 帧、但**最大到达间隔 ≈1.25 秒** ✓✓
    //:   ⇒ 这既解释了"心跳采样时槽常是空的"（采样落在空档 ✓），**也是"一卡一卡"的另一半原因** ✓。
    int winArrivals_ = 0;           ///< 本窗口（两次心跳之间）的到达数
    qint64 lastArriveMs_ = 0;       ///< 上一次到达的单调毫秒（诊断用，允许良性竞争 ✓）
    qint64 maxGapMs_ = 0;           ///< 本窗口内"两次到达之间"的最大间隔
    QElapsedTimer fpsClk_;          ///< 帧率打点用（**每实例一份** ✓，不能用 static ✗）
    bool playing_ = false;
    bool failed_ = false;
    bool eosSent_ = false;
    /// ★ `PartialUpdate` 下"这一帧还没画过"（首帧 / 刚 resize）才允许清黑 ✓（见 paintGL ✓）
    bool drewOnce_ = false;
    int framesShown_ = 0;
    int zeroCopyFrames_ = 0;
};
