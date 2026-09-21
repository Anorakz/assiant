// ============================================================================
//  gui/tests/test_icons.cpp — 图标守卫
//
//  为什么要有它：`ui::iconNames()`（icons.cpp 的 kNames）与 resources/icons/*.svg、
//  icons.qrc 是**三份必须同步**的清单，而在 S5 之前 iconNames() **全仓没人用** ——
//  icons.cpp 里那句"测试会核对数量"是假的。后果很隐蔽：加了 .svg 却忘了登记
//  kNames/icons.qrc，界面上只是**静默少一个图标**，不会有任何报错。
//
//  这里把它变成可执行约束：
//    · kNames 里的每个名字都必须能取到非空 QIcon（= qrc 里登记了）
//    · kNames 与源码目录里的 *.svg 清单必须**逐个对上**（= 两边都不漏）
//    · 名字不重复；不认识的名字给空 QIcon 而不是乱给一个
// ============================================================================
#include <QColor>
#include <QDir>
#include <QIcon>
#include <QImage>
#include <QSet>
#include <QStringList>
#include <QtTest/QtTest>

#include "ui/icons.h"

class TestIcons : public QObject {
    Q_OBJECT

private slots:
    void everyNameHasAResource()
    {
        const QStringList names = ui::iconNames();
        QVERIFY2(names.size() >= 23, qPrintable(QString::number(names.size())));

        QStringList broken;
        for (const QString& name : names) {
            const QIcon icon = ui::icon(name);
            if (icon.isNull() || icon.pixmap(24, 24).isNull()) {
                broken << name;
            }
        }
        QVERIFY2(broken.isEmpty(),
                 qPrintable(QStringLiteral("这些名字取不到图标（qrc 里漏登记？）：%1")
                                .arg(broken.join(QStringLiteral(", ")))));
    }

    void nameListMatchesSourceSvgs()
    {
        QDir dir(QStringLiteral(ICONS_SOURCE_DIR));
        QVERIFY2(dir.exists(), qPrintable(dir.absolutePath()));

        QStringList files = dir.entryList(QStringList() << QStringLiteral("*.svg"),
                                          QDir::Files, QDir::Name);
        for (QString& name : files) {
            name.chop(4);                       // 去掉 ".svg"
        }
        QStringList names = ui::iconNames();
        files.sort();
        names.sort();
        QCOMPARE(names, files);                 // 差集会被直接打印出来
    }

    void namesAreUnique()
    {
        const QStringList names = ui::iconNames();
        QSet<QString> unique;
        for (const QString& name : names) {
            unique.insert(name);
        }
        QCOMPARE(unique.size(), names.size());
    }

    void unknownNameIsAnEmptyIcon()
    {
        QVERIFY(ui::icon(QStringLiteral("no-such-icon")).isNull());
    }

    void scheduleIconIsRegistered()
    {
        // S5 的第 23 个图标：日程区用
        QVERIFY(ui::iconNames().contains(QStringLiteral("schedule")));
        QVERIFY(!ui::icon(QStringLiteral("schedule")).isNull());
    }

    void tintedIconUsesTheRequestedColor()
    {
        const QColor want(255, 0, 0);
        const QImage image = ui::tintedIcon(QStringLiteral("home"), want)
                                 .pixmap(32, 32)
                                 .toImage();
        QVERIFY(!image.isNull());

        bool found = false;
        for (int y = 0; y < image.height() && !found; ++y) {
            for (int x = 0; x < image.width(); ++x) {
                const QColor color = image.pixelColor(x, y);
                if (color.alpha() == 0) {
                    continue;
                }
                // 原始 SVG 是 #E6E6E6；染色后该变成红系（边缘抗锯齿会有混合，别卡太死）
                QVERIFY2(color.red() > 180 && color.green() < 100,
                         qPrintable(QStringLiteral("像素 (%1,%2) = %3")
                                        .arg(x).arg(y).arg(color.name())));
                found = true;
                break;
            }
        }
        QVERIFY2(found, "整个图标都是透明的？");
    }
};

QTEST_MAIN(TestIcons)
#include "test_icons.moc"
