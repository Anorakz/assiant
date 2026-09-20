// ============================================================================
//  gui/tests/test_view_state.cpp — 协议 → 显示状态 单测
//
//  对齐 docs/ipc-protocol.md §3/§6：
//    · 未知 topic 忽略（只计数，不算错误）
//    · 已知 topic 字段缺失/类型错 → 该字段不动 + dropped 计数，其它合法字段照常应用
// ============================================================================
#include <QJsonObject>
#include <QtTest/QtTest>

#include "core/view_state.h"

using core::ViewState;

namespace {

QJsonObject makeStatus(const QString& mode, bool connected)
{
    return QJsonObject{{QStringLiteral("mode"), mode},
                       {QStringLiteral("connected"), connected}};
}

} // namespace

class TestViewState : public QObject {
    Q_OBJECT

private slots:
    void statusAppliesAndValidatesMode();
    void modeSwitchChoicesFollowCurrentMode();
    void llmWallpaperMusicApply();
    void unknownTopicIsIgnoredNotAnError();
    void missingFieldCountsAsDroppedButKeepsOthers();
    void resetClearsEverything();
};

void TestViewState::statusAppliesAndValidatesMode()
{
    ViewState state;
    QVERIFY(state.modes().contains(QStringLiteral("SLEEP")));
    QVERIFY(state.modes().contains(QStringLiteral("GAME")));

    QVERIFY(state.applyMessage(QStringLiteral("status"),
                               makeStatus(QStringLiteral("STUDY"), true)));
    QCOMPARE(state.mode(), QStringLiteral("STUDY"));
    QCOMPARE(state.connected(), true);
    QVERIFY(state.hasStatus());
    QCOMPARE(state.droppedCount(), 0);

    // 小写不是协议值（协议要求全大写）：mode 被丢弃，但同一条里的 connected 仍应生效
    QVERIFY(state.applyMessage(QStringLiteral("status"),
                               makeStatus(QStringLiteral("study"), true)));
    QCOMPARE(state.mode(), QStringLiteral("STUDY"));     // 保持上一次的合法值
    QCOMPARE(state.droppedCount(), 1);

    // 只有非法字段的消息：mode 非法 + connected 缺失 => 记 2 次，且没有任何字段被更新
    QVERIFY(!state.applyMessage(QStringLiteral("status"),
                                QJsonObject{{QStringLiteral("mode"), QStringLiteral("nope")}}));
    QCOMPARE(state.mode(), QStringLiteral("STUDY"));
    QCOMPARE(state.droppedCount(), 3);
}

void TestViewState::modeSwitchChoicesFollowCurrentMode()
{
    // 未知/IDLE → 三个入口
    QCOMPARE(ViewState::modeSwitchChoices(QString()),
             QStringList({QStringLiteral("SLEEP"), QStringLiteral("STUDY"),
                          QStringLiteral("GAME")}));
    QCOMPARE(ViewState::modeSwitchChoices(QStringLiteral("IDLE")),
             QStringList({QStringLiteral("SLEEP"), QStringLiteral("STUDY"),
                          QStringLiteral("GAME")}));
    // 其它模式 → 只有"退出"（不许出现"学习 → 游戏"这种要过状态机的直跳）
    QCOMPARE(ViewState::modeSwitchChoices(QStringLiteral("STUDY")),
             QStringList({QStringLiteral("IDLE")}));
    QCOMPARE(ViewState::modeSwitchChoices(QStringLiteral("GAME")),
             QStringList({QStringLiteral("IDLE")}));
    QCOMPARE(ViewState::modeSwitchChoices(QStringLiteral("SLEEP")),
             QStringList({QStringLiteral("IDLE")}));
    // 非法值按"未知"处理，仍给三个入口
    QCOMPARE(ViewState::modeSwitchChoices(QStringLiteral("nonsense")).size(), 3);
}

void TestViewState::llmWallpaperMusicApply()
{
    ViewState state;

    QVERIFY(state.applyMessage(QStringLiteral("llm"),
                               QJsonObject{{QStringLiteral("text"),
                                            QStringLiteral("已经切换到学习模式。")}}));
    QCOMPARE(state.llmText(), QStringLiteral("已经切换到学习模式。"));
    QVERIFY(state.hasLlm());

    QVERIFY(state.applyMessage(QStringLiteral("wallpaper"),
                               QJsonObject{{QStringLiteral("path"),
                                            QStringLiteral("/home/kickpi/wallpapers/04.jpg")},
                                           {QStringLiteral("index"), 3}}));
    QCOMPARE(state.wallpaperPath(), QStringLiteral("/home/kickpi/wallpapers/04.jpg"));
    QCOMPARE(state.wallpaperIndex(), 3);

    QVERIFY(state.applyMessage(QStringLiteral("music"),
                               QJsonObject{{QStringLiteral("title"), QStringLiteral("夜曲")},
                                           {QStringLiteral("playing"), true}}));
    QCOMPARE(state.musicTitle(), QStringLiteral("夜曲"));
    QCOMPARE(state.musicPlaying(), true);

    QCOMPARE(state.droppedCount(), 0);
    QCOMPARE(state.ignoredTopicCount(), 0);
}

void TestViewState::unknownTopicIsIgnoredNotAnError()
{
    ViewState state;
    QVERIFY(!state.applyMessage(QStringLiteral("brand_new_topic"),
                                QJsonObject{{QStringLiteral("x"), 1}}));
    QCOMPARE(state.ignoredTopicCount(), 1);
    QCOMPARE(state.droppedCount(), 0);          // 忽略 ≠ 错误
}

void TestViewState::missingFieldCountsAsDroppedButKeepsOthers()
{
    ViewState state;
    // status 只有 connected，没有合法 mode
    QVERIFY(state.applyMessage(QStringLiteral("status"),
                               QJsonObject{{QStringLiteral("connected"), true}}));
    QCOMPARE(state.connected(), true);
    QVERIFY(state.hasStatus());                 // connected 已生效
    QCOMPARE(state.droppedCount(), 1);          // mode 缺失记一次

    // 类型不对（mode 是数字）
    state.applyMessage(QStringLiteral("status"),
                       QJsonObject{{QStringLiteral("mode"), 7},
                                   {QStringLiteral("connected"), false}});
    QCOMPARE(state.connected(), false);
    QCOMPARE(state.droppedCount(), 2);

    // music 缺 playing
    state.applyMessage(QStringLiteral("music"),
                       QJsonObject{{QStringLiteral("title"), QStringLiteral("夜曲")}});
    QCOMPARE(state.musicTitle(), QStringLiteral("夜曲"));
    QCOMPARE(state.droppedCount(), 3);
}

void TestViewState::resetClearsEverything()
{
    ViewState state;
    state.applyMessage(QStringLiteral("status"), makeStatus(QStringLiteral("GAME"), true));
    state.applyMessage(QStringLiteral("llm"),
                       QJsonObject{{QStringLiteral("text"), QStringLiteral("hi")}});
    state.applyMessage(QStringLiteral("nothing"), QJsonObject());
    QVERIFY(state.hasStatus());
    QVERIFY(state.hasLlm());

    state.reset();
    QVERIFY(!state.hasStatus());
    QVERIFY(!state.hasLlm());
    QVERIFY(!state.hasWallpaper());
    QVERIFY(!state.hasMusic());
    QCOMPARE(state.mode(), QString());
    QCOMPARE(state.droppedCount(), 0);
    QCOMPARE(state.ignoredTopicCount(), 0);
}

QTEST_APPLESS_MAIN(TestViewState)
#include "test_view_state.moc"
