// ============================================================================
//  gui/src/services/onboard_ctl.h — 控制屏上软键盘 onboard（T6）
//
//  为什么不能直接用 QDBusConnection
//  ---------------------------------------------------------------------------
//  onboard 跑在**桌面用户**（kickpi, uid 1001）的会话总线上
//  （DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus），而本 GUI 以 root 跑；
//  per-user 的 dbus-daemon 不认别的 uid，root 直接连会 "The connection is closed"。
//  实测可行路径：`runuser -u kickpi -- env DBUS_SESSION_BUS_ADDRESS=... gdbus call ...`
//  —— 所以这里用 QProcess + gdbus，并在**同 uid 时**省掉 runuser。
//
//  接口（板端 gdbus introspect 实测）
//  ---------------------------------------------------------------------------
//      service   org.onboard.Onboard
//      path      /org/onboard/Onboard/Keyboard
//      iface     org.onboard.Onboard.Keyboard
//      methods   Show() / Hide() / ToggleVisible()
//      property  readonly b Visible
// ============================================================================
#pragma once

#include <QObject>
#include <QString>

class OnboardCtl : public QObject {
    Q_OBJECT

public:
    explicit OnboardCtl(QObject* parent = nullptr);

    /// 找到 onboard 进程、它的会话总线地址与所属用户。找不到返回 false（不抛）。
    bool probe(QString* detail = nullptr);

    bool available() const { return !busAddress_.isEmpty() && !ownerUser_.isEmpty(); }
    QString busAddress() const { return busAddress_; }
    QString ownerUser() const { return ownerUser_; }

    bool show(QString* error = nullptr);
    bool hide(QString* error = nullptr);

    /// 查询可见性。查询失败时返回 false 并把 *ok 置 false（区分"隐藏"与"查不到"）。
    bool isVisible(bool* ok = nullptr);

    /// 纯逻辑：哪种输入源需要软键盘（只有 keyboard 需要）——可直接单测。
    static bool wantsOnboard(const QString& inputSource);

    /// 纯逻辑：**现在**该不该把软键盘弹出来？——可直接单测。
    ///
    /// 政策（S10 起）：**只有输入框拿到焦点时才弹**。启动、切换输入源都不弹 ——
    /// 以前一开机键盘就盖住半个主区，用户根本没打算打字。输入框失焦就收起。
    /// `gui.onboard_auto = false` 时永远不弹（完全不碰 onboard）。
    static bool shouldShow(bool onboardAuto, const QString& inputSource, bool inputFocused);

private:
    bool callMethod(const QString& method, QString* error);
    QString runGdbus(const QStringList& args, bool* ok, QString* error) const;

    QString busAddress_;
    QString ownerUser_;
    QString ownerUid_;
    int ownerPid_ = -1;
};
