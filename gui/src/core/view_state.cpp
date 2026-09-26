// ============================================================================
//  gui/src/core/view_state.cpp — 协议 → 显示状态
// ============================================================================
#include "core/view_state.h"

#include <QJsonValue>

namespace core {

const QStringList& ViewState::modes()
{
    static const QStringList kModes = {QStringLiteral("SLEEP"), QStringLiteral("IDLE"),
                                       QStringLiteral("STUDY"), QStringLiteral("GAME")};
    return kModes;
}

QStringList ViewState::modeSwitchChoices(const QString& currentMode)
{
    // T12-3: **都能点** —— 不只是"退出当前模式"。
    //
    // 为什么改: 状态机本来就要求"任何切换都经过 IDLE"（`GAME -> IDLE -> SLEEP`），
    // Agent 那边现在会**逐跳走**（每一跳都释放那个模式里的东西: 视频/队列/SigLIP…）。
    // 界面以前把跨模式的目标全灰掉，于是"从游戏点睡眠"根本发不出去 —— 用户只能绕
    // "先退出、再睡眠"两步，而 Agent 明明能一步做完。
    //
    //   IDLE（或还没收到/非法） -> 进入某个模式的三个入口（不变）
    //   STUDY / GAME            -> 退出 + 睡眠 + 另一个活跃模式（由 Agent 走路径）
    //   SLEEP                   -> 只给"退出当前模式"（睡眠屏不提供直接跳进游戏/学习）
    if (currentMode.isEmpty() || !isValidMode(currentMode)
        || currentMode == QLatin1String("IDLE")) {
        return {QStringLiteral("SLEEP"), QStringLiteral("STUDY"), QStringLiteral("GAME")};
    }
    if (currentMode == QLatin1String("SLEEP")) {
        return {QStringLiteral("IDLE")};
    }
    if (currentMode == QLatin1String("STUDY")) {
        return {QStringLiteral("IDLE"), QStringLiteral("SLEEP"), QStringLiteral("GAME")};
    }
    return {QStringLiteral("IDLE"), QStringLiteral("SLEEP"), QStringLiteral("STUDY")};
}

bool ViewState::isValidMode(const QString& mode)
{
    return modes().contains(mode);
}

void ViewState::reset()
{
    *this = ViewState();
}

bool ViewState::applyMessage(const QString& topic, const QJsonObject& data,
                             const QString& rawLine)
{
    lastTopic_ = topic;
    lastRawLine_ = rawLine;

    bool changed = false;

    if (topic == QLatin1String("status")) {
        // mode：必须是协议约定的四个全大写值之一
        const QJsonValue modeValue = data.value(QStringLiteral("mode"));
        if (modeValue.isString() && isValidMode(modeValue.toString())) {
            mode_ = modeValue.toString();
            hasStatus_ = true;
            changed = true;
        } else {
            ++droppedCount_;
        }
        const QJsonValue connectedValue = data.value(QStringLiteral("connected"));
        if (connectedValue.isBool()) {
            connected_ = connectedValue.toBool();
            hasStatus_ = true;
            changed = true;
        } else {
            ++droppedCount_;
        }
        return changed;
    }

    if (topic == QLatin1String("llm")) {
        const QJsonValue textValue = data.value(QStringLiteral("text"));
        if (textValue.isString()) {
            llmText_ = textValue.toString();
            hasLlm_ = true;
            changed = true;
        } else {
            ++droppedCount_;
        }
        return changed;
    }

    if (topic == QLatin1String("wallpaper")) {
        const QJsonValue pathValue = data.value(QStringLiteral("path"));
        if (pathValue.isString()) {
            wallpaperPath_ = pathValue.toString();
            hasWallpaper_ = true;
            changed = true;
        } else {
            ++droppedCount_;
        }
        const QJsonValue indexValue = data.value(QStringLiteral("index"));
        if (indexValue.isDouble()) {
            wallpaperIndex_ = static_cast<int>(indexValue.toDouble());
            hasWallpaper_ = true;
            changed = true;
        } else {
            ++droppedCount_;
        }
        return changed;
    }

    if (topic == QLatin1String("music")) {
        const QJsonValue titleValue = data.value(QStringLiteral("title"));
        if (titleValue.isString()) {
            musicTitle_ = titleValue.toString();
            hasMusic_ = true;
            changed = true;
        } else {
            ++droppedCount_;
        }
        const QJsonValue playingValue = data.value(QStringLiteral("playing"));
        if (playingValue.isBool()) {
            musicPlaying_ = playingValue.toBool();
            hasMusic_ = true;
            changed = true;
        } else {
            ++droppedCount_;
        }
        return changed;
    }

    // T11-7: `bilibili` 是**已知** topic，只是它的负载是数组（`queue[]` + `current`），
    //   由视频区/封面区自己消化（MainWindow -> VideoPanel / BilibiliCover）。
    //   ⚠ 别把它算进"忽略的 topic 数"：设置页那个计数器会让人以为界面没认它（其实认了）。
    if (topic == QLatin1String("bilibili")) {
        return false;
    }

    // 未知 topic：按协议"忽略"，不是错误
    ++ignoredTopicCount_;
    return false;
}

} // namespace core
