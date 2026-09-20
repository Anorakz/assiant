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
    // 只有 IDLE（或还没收到 / 收到非法值）时才给"进入某个模式"的三个入口；
    // 处在其它模式时只给一个"退出"，避免出现"学习 → 游戏"这种要经状态机判断的直跳。
    if (currentMode.isEmpty() || !isValidMode(currentMode)
        || currentMode == QLatin1String("IDLE")) {
        return {QStringLiteral("SLEEP"), QStringLiteral("STUDY"), QStringLiteral("GAME")};
    }
    return {QStringLiteral("IDLE")};
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

    // 未知 topic：按协议"忽略"，不是错误
    ++ignoredTopicCount_;
    return false;
}

} // namespace core
