################################################################################
#
# libxcrypt
#
# ⚠ 这份是**我们仓库维护的版本**，由 image/install-into-sdk.sh 覆盖进厂商 SDK 的
#   buildroot/package/libxcrypt/libxcrypt.mk（注入时会打印 "~ 覆盖"，看得见）。
#
# 为什么必须覆盖（T15-2-9 实测，整机构建卡在这）
# ---------------------------------------------------------------------------
#   厂商快照里这份 .mk **只有 target 变体**：
#       $(eval $(autotools-package))
#   而同一棵树里 systemd 的 `HOST_SYSTEMD_DEPENDENCIES`（systemd.mk 第 ~902 行）
#   明确要 `host-libxcrypt`。buildroot 只有在包定义了 host 变体时才生成
#   `host-<pkg>` 这个 make 目标，于是整机构建直接死在：
#       make: *** No rule to make target 'host-libxcrypt',
#              needed by '.../host-systemd-254.9/.stamp_configured'.  Stop.
#   注意：**target 侧没问题**（.config 里 BR2_PACKAGE_LIBXCRYPT=y 是开着的，
#   源码 libxcrypt-4.4.36.tar.gz 也已下好），缺的只是 host 变体。
#   上游 buildroot 后来补上了这个变体（连 HOST_*_CONF_OPTS 一起），这里照抄上游写法，
#   版本与 SITE 保持厂商那份不变（4.4.36，github/{besser82}），只在下面加 HOST 两行 +
#   最后那行 host-autotools-package。这样 host-systemd 能配上，而 target 侧行为一点没变。
#
################################################################################

LIBXCRYPT_VERSION = 4.4.36
LIBXCRYPT_SITE = $(call github,besser82,libxcrypt,v$(LIBXCRYPT_VERSION))
LIBXCRYPT_LICENSE = LGPL-2.1+
LIBXCRYPT_LICENSE_FILES = LICENSING COPYING.LIB
LIBXCRYPT_INSTALL_STAGING = YES
LIBXCRYPT_AUTORECONF = YES

# Some warnings turn into errors with some sensitive compilers
LIBXCRYPT_CONF_OPTS = --disable-werror
HOST_LIBXCRYPT_CONF_OPTS = --disable-werror

# Disable obsolete and unsecure API
LIBXCRYPT_CONF_OPTS += --disable-obsolete_api
HOST_LIBXCRYPT_CONF_OPTS += --disable-obsolete_api

$(eval $(autotools-package))
$(eval $(host-autotools-package))
