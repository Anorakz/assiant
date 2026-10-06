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
#include <QOpenGLWidget>
#include <QQueue>
#include <QString>

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
    /// ★ 收"帧到达"的低优先级事件（`QEvent::User` ⇒ 请求重绘 ✓，见 .cpp 的 onHandoff ✓）
    bool event(QEvent* e) override;

private:
    // ---- GStreamer 运行时（dlopen 出来的函数指针）----
    struct Gst;                     ///< 函数指针表（定义在 .cpp）
    static Gst* gst();              ///< 懒加载；失败返回 nullptr

    /// ★ 25ms 心跳：**GUI 线程**主动请求重绘（见上面"帧率定案"✓）。
    ///   没新帧时**什么都不做** ✓ —— `PartialUpdate` 会把上一帧留在 FBO 里 ✓。
    void tickPaint();

    void teardown();
    bool buildPipeline(const QString& url);
    void pumpBus();                 ///< 处理 EOS/ERROR（在 paintGL 里顺带调）
    static void onHandoff(void* fakesink, void* buffer, void* pad, void* user);

    QByteArray sourceBytes_;
    QString source_;
    void* pipeline_ = nullptr;      ///< GstElement*
    void* sink_ = nullptr;          ///< GstElement*
    QQueue<void*> queue_;           ///< ⚠ 已废弃：跨线程访问它曾是崩溃根因（见 pending_）
    /// ★★ 线程安全的"单槽"换手（2026-10-07 事故修复）：
    ///   GStreamer 的 handoff 线程只做 `fetchAndStoreOrdered`，GUI 线程在 paintGL 里也只用
    ///   `fetchAndStoreOrdered(nullptr)` 取走 —— **没有任何一方"读-改-写"共享容器** ⇒ 无竞争 ✓
    QAtomicPointer<void> pending_;
    QTimer* tick_ = nullptr;        ///< 25ms 重绘心跳（只有它在驱动 paintGL ✓）
    bool alwaysRepaint_ = false;    ///< A/B 诊断：`DSH_GST_REPAINT_ALWAYS=1` ⇒ 每个 tick 都重画
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
    int evtN_ = 0;                  ///< 收到的"帧到达"事件数（主驱动 ✓）
    //: ★ 诊断（第 73 轮）：**到达的"形状"** ✓ —— "平均 26/s" 完全可能掩盖"一阵一阵" ✗，
    //:   而"一阵一阵"正好能解释"心跳采样时槽是空的" ✓（bursty 到达 + 采样 = 大多采到空档 ✓）。
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
