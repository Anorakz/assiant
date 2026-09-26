// ============================================================================
//  gui/tests/test_view_state.cpp — 协议 → 显示状态 单测
//
//  对齐 docs/ipc-protocol.md §3/§6：
//    · 未知 topic 忽略（只计数，不算错误）
//    · 已知 topic 字段缺失/类型错 → 该字段不动 + dropped 计数，其它合法字段照常应用
// ============================================================================
#include <QJsonArray>
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
    void scheduleTopicIsStillIgnoredByTheGui();
    void bilibiliTopicIsKnownButNotStoredHere();
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
    // 其它模式 → 退出 + 睡眠 + 另一个活跃模式（T12-3：都能点, 跨模式由 Agent 走路径）
    QCOMPARE(ViewState::modeSwitchChoices(QStringLiteral("STUDY")),
             QStringList({QStringLiteral("IDLE"), QStringLiteral("SLEEP"),
                          QStringLiteral("GAME")}));
    QCOMPARE(ViewState::modeSwitchChoices(QStringLiteral("GAME")),
             QStringList({QStringLiteral("IDLE"), QStringLiteral("SLEEP"),
                          QStringLiteral("STUDY")}));
    // 睡眠屏例外: 只给"退出当前模式"（不提供直接跳进游戏/学习）
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

void TestViewState::scheduleTopicIsStillIgnoredByTheGui()
{
    // P 系列往协议里加了 topic "schedule"（日程触发事实，docs/ipc-protocol.md §3）。
    // GUI 这一侧**故意不认它**：界面暂时不显示"已触发"，靠 §3 的"未知 topic 忽略"
    // 保持向前兼容。这个用例把"不认"钉成**契约**而不是巧合 —— 哪天有人顺手加了
    // 一个不完整的 schedule 分支，这里会红，逼他先把行为想清楚。
    //
    // 两种线上形态都过一遍（kind="fired" 实时 / kind="state" 应答快照）。
    ViewState state;
    const QJsonObject fired{
        {QStringLiteral("kind"), QStringLiteral("fired")},
        {QStringLiteral("event"),
         QJsonObject{{QStringLiteral("title"), QStringLiteral("午休")},
                     {QStringLiteral("fired_at"), QStringLiteral("2026-09-22T13:00:03")}}}};
    const QJsonObject snapshot{
        {QStringLiteral("kind"), QStringLiteral("state")},
        {QStringLiteral("limit"), 50},
        {QStringLiteral("fired"), QJsonArray{fired.value(QStringLiteral("event"))}}};

    QVERIFY(!state.applyMessage(QStringLiteral("schedule"), fired));
    QVERIFY(!state.applyMessage(QStringLiteral("schedule"), snapshot));

    QCOMPARE(state.ignoredTopicCount(), 2);
    QCOMPARE(state.droppedCount(), 0);          // 忽略 ≠ 收到坏消息
    QVERIFY(!state.hasStatus());                // 也没顺手改别的东西
}

void TestViewState::bilibiliTopicIsKnownButNotStoredHere()
{
    // T11-7: `bilibili` 的负载是数组（queue[]），由视频区/封面区自己消化 ——
    // ViewState 不存它，但它**是已知 topic**：不能算进"忽略的 topic 数"
    //（设置页那个计数器一旦涨，会让人以为界面没认这条推送）。
    ViewState state;
    const QJsonObject payload{
        {QStringLiteral("queue"), QJsonArray{QJsonObject{{QStringLiteral("bvid"), QStringLiteral("BV1")}}}},
        {QStringLiteral("index"), 0},
        {QStringLiteral("stream"), QStringLiteral("/tmp/bilibili-BV1.ts")}};

    QVERIFY(!state.applyMessage(QStringLiteral("bilibili"), payload));
    QVERIFY(!state.applyMessage(QStringLiteral("bilibili"), QJsonObject()));

    QCOMPARE(state.ignoredTopicCount(), 0);     // 认它，只是不在这儿存
    QCOMPARE(state.droppedCount(), 0);          // 也不是坏消息
    QVERIFY(!state.hasStatus());
    QCOMPARE(state.lastTopic(), QStringLiteral("bilibili"));   // 诊断面板仍看得到最近一条
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
