// ============================================================================
//  gui/tests/test_failure_text.cpp — R5：播放失败的文案不许消失（T15-16 遗留③第 7 行）
//
//  为什么单独开一个文件 ✓（而不是塞进 `test_style_guard` ✗）
//  ---------------------------------------------------------------------------
//   我在 `test_style_guard.cpp` 上**连续三次栽在"锚点"上** ✗：
//   先假设它用 `QTEST_MAIN`（其实是 `QTEST_GUILESS_MAIN` ✓），再用正则找 main（位置又不对 ✗），
//   最后连"槽声明"的缩进都猜错（命中 0 ✗）。⇒ **不再猜别人的文件** ✓，
//   新开一个**完全由我控制**的文件 ✓（零锚点风险 ✓）。这也更符合"一个判据一个文件"的习惯 ✓。
//
//  为什么是**源码级**断言 ✗→✓
//  ---------------------------------------------------------------------------
//   宿主测试环境**没有 multimedia 后端** ✓（日志里一直有
//   `no service found for - "org.qt-project.qt.mediaplayer"` ✓）⇒ `QMediaPlayer::error`
//   **驱动不了** ✗ ⇒ 写 `setSource(坏文件)` + `QTRY_VERIFY` 只会**超时变红** ✗。
//   ⇒ 钉住"**错误分支存在** + **那句用户看得见的话还在**" ✓（与 `test_style_guard` 的 R1/R2 同路 ✓）。
//
//  ⚠ 它**不是**行为测试 ✗ —— 只保证"这句话没被顺手删掉" ✓。真正的行为要靠板端 ✓。
// ============================================================================
#include <QDir>
#include <QDirIterator>
#include <QFile>
#include <QFileInfo>
#include <QMap>
#include <QtTest/QtTest>

namespace {

/// `gui/src` 的绝对路径（从本文件的 `__FILE__` 推 ✓ —— 与 test_style_guard 同一写法 ✓）
QString sourceRoot()
{
    const QFileInfo me(QString::fromUtf8(__FILE__));
    return QDir(me.absolutePath()).absoluteFilePath(QStringLiteral("../src"));
}

/// 读 `gui/src` 下所有 .cpp/.h（相对路径 → 文本 ✓）
QMap<QString, QString> readSources()
{
    QMap<QString, QString> out;
    const QDir root(sourceRoot());
    QDirIterator it(root.absolutePath(),
                    {QStringLiteral("*.cpp"), QStringLiteral("*.h")},
                    QDir::Files, QDirIterator::Subdirectories);
    while (it.hasNext()) {
        const QString path = it.next();
        QFile file(path);
        if (file.open(QIODevice::ReadOnly)) {
            out.insert(root.relativeFilePath(path), QString::fromUtf8(file.readAll()));
        }
    }
    return out;
}

} // namespace

class TestFailureText : public QObject {
    Q_OBJECT

private slots:
    /// 反空转：源树得真的被读到 ✓（否则下面两条会"因为找不到文件而红"✗，看不出真原因 ✓）
    void sourceTreeIsFound()
    {
        const QMap<QString, QString> files = readSources();
        QVERIFY2(files.size() > 20, qPrintable(QStringLiteral("只读到 %1 个源文件 ✗（路径推导错了？）")
                                                   .arg(files.size())));
        QVERIFY2(files.contains(QStringLiteral("ui/video_panel.cpp")),
                 "没读到 ui/video_panel.cpp ✗");
    }

    /// R5（矩阵第 7 行）：**错误分支**要在 ✓，那句**用户看得见的话**也要在 ✓
    void playbackFailureTextIsStillThere()
    {
        const QMap<QString, QString> files = readSources();
        QVERIFY2(files.contains(QStringLiteral("ui/video_panel.cpp")), "找不到 video_panel.cpp ✗");
        const QString text = files.value(QStringLiteral("ui/video_panel.cpp"));

        QVERIFY2(text.contains(QStringLiteral("QMediaPlayer::Error")),
                 "video_panel 里找不到 QMediaPlayer::Error 的处理分支 ✗（失败就没提示了）");
        QVERIFY2(text.contains(QStringLiteral("播放失败")),
                 "「播放失败」的文案不见了 ✗ —— 黑屏却不说话，用户不知道发生了什么");
    }
};

QTEST_GUILESS_MAIN(TestFailureText)
#include "test_failure_text.moc"
