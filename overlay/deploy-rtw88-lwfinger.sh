#!/bin/bash
# ==============================================================
# lwfinger rtw88 增强版 SDIO 驱动部署脚本（Armbian chroot 内专用）
# 依赖：/tmp/overlay/ 下需预存 rtw88-master.zip
# ==============================================================
set -e

echo "[WIFI] 开始部署 rtw88 增强版 SDIO 驱动（lwfinger）"

## 1. 永久屏蔽内核原生rtw88整套驱动，避免模块冲突
cat > /etc/modprobe.d/blacklist-rtw88-native.conf << EOF
blacklist rtw88_core
blacklist rtw88_sdio
blacklist rtw88_8821c
blacklist rtw88_8821cs
install rtw88_core /bin/false
install rtw88_sdio /bin/false
install rtw88_8821c /bin/false
install rtw88_8821cs /bin/false
EOF

## 2. 内核启动参数黑名单，boot阶段彻底拦截原生驱动
BLACKLIST_STR="module_blacklist=rtw88_core,rtw88_sdio,rtw88_8821c,rtw88_8821cs"
if grep -q "^extraargs=" /boot/armbianEnv.txt; then
    # 匹配已有extraargs，先判断末尾是否有空格，安全追加参数
    sed -i "/^extraargs=/ {/ $/!s/$/ /; s/$/$BLACKLIST_STR/}" /boot/armbianEnv.txt
else
    # 不存在则新建一行，自带换行
    echo "extraargs=$BLACKLIST_STR" >> /boot/armbianEnv.txt
fi

## 3. 安装编译依赖 + 解压工具
apt update -y
apt install -y build-essential dkms bc unzip linux-headers-current-rockchip64 rfkill

# ========== 核心修复：获取目标镜像真实内核版本 ==========
# 从 /lib/modules 目录读取镜像内实际安装的 arm64 内核版本（chroot 内 uname -r 是主机内核，绝对不能用）
TARGET_KERNEL=$(ls /lib/modules/ | head -n 1)
echo "[DEBUG] 目标镜像内核版本: ${TARGET_KERNEL}"

# 校验内核头文件是否存在
if [ ! -d "/lib/modules/${TARGET_KERNEL}/build" ]; then
    echo "[ERROR] 内核 ${TARGET_KERNEL} 对应的头文件不存在，编译终止"
    exit 1
fi
# ========================================================

## 4. 离线解压驱动源码到标准DKMS目录
rm -rf /usr/src/rtw88-0.6
mkdir -p /usr/src
unzip -q /tmp/overlay/rtw88-master.zip -d /usr/src/
mv /usr/src/rtw88-master /usr/src/rtw88-0.6

## 新增：适配RK3566 SDIO时序，修改传输块与超时参数  (可以通过此方法修改压缩包中文件，但是我看了源码位置不对，这个方案暂且停止)
# SDIO_C="/usr/src/rtw88-0.6/sdio.c"
# # 替换超时，不管原有数字、前后空格、注释
# sed -i '/#define RTW_SDIO_RW_TIMEOUT/s/[0-9]\+/10000/' "$SDIO_C"
# # 替换块大小
# sed -i '/#define SDIO_BLOCK_SIZE/s/[0-9]\+/512/' "$SDIO_C"
# # 打印完整宏定义校验
# echo "[WIFI DEBUG] 修改后SDIO参数："
# grep -E "^#define (RTW_SDIO_RW_TIMEOUT|SDIO_BLOCK_SIZE)" "$SDIO_C"

## 5. DKMS 注册、编译、安装驱动模块（强制指定目标内核版本）
dkms add -m rtw88 -v 0.6
dkms build -m rtw88 -v 0.6 -k "${TARGET_KERNEL}"
dkms install -m rtw88 -v 0.6 -k "${TARGET_KERNEL}"

## 6. 安装驱动配套SDIO专用固件
make -C /usr/src/rtw88-0.6 install_fw

## 7. 配置开机自动加载SDIO驱动
echo "rtw88_8821cs" > /etc/modules-load.d/wifi-driver.conf

## 8. 更新initramfs，固化黑名单到启动镜像
update-initramfs -u -k "${TARGET_KERNEL}"

## 9. 清理DKMS源码，减小镜像占用
rm -rf /usr/src/rtw88-0.6
rm -f /tmp/overlay/rtw88-master.zip

echo "[WIFI] rtw88 增强版 SDIO 驱动部署完成"

