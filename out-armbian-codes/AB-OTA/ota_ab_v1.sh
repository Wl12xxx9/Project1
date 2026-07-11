#!/bin/sh
# NanoPi R3S 双分区自动切换OTA升级脚本
set -e

# ======================步骤1：基础变量定义======================
# 变量定义
echo "=====================【阶段1 初始化变量】====================="
PART_PREFIX=/dev/mmcblk0
SLOT_A_PART=p3
SLOT_B_PART=p4
SLOT_A_DEV=${PART_PREFIX}${SLOT_A_PART}
SLOT_B_DEV=${PART_PREFIX}${SLOT_B_PART}
SLOT_B_MNT=/mnt/slot_b
SLOT_A_MNT=/mnt/slot_a
OTA_STORE=/data/ota
OTA_PKG_NAME=nanopi-r3s-ota-v1.0.0.ota.tar.xz
OTA_PKG_PATH=${OTA_STORE}/${OTA_PKG_NAME}
# 改用/data分区存放解压临时文件，避开内存/tmp空间不足
VERIFY_DIR=/data/tmp_ota

echo "OTA完整路径：${OTA_PKG_PATH}"
echo "校验临时目录：${VERIFY_DIR}"

# ======================新增：自动检测当前运行分区======================
echo -e "\n=====================【阶段2 检测当前运行分区】===================="
get_current_slot() {
    # 获取根文件系统所在设备
    root_dev=$(mount | awk '/ \/ /{print $1}')
    if [ "$root_dev" = "$SLOT_A_DEV" ]; then
        echo "A"
    elif [ "$root_dev" = "$SLOT_B_DEV" ]; then
        echo "B"
    else
        echo "unknown"
        exit 1
    fi
}
CURRENT_SLOT=$(get_current_slot)
echo "当前运行分区：SLOT_${CURRENT_SLOT}"

# 根据当前分区自动设置待升级目标分区、挂载点、分区号
if [ "$CURRENT_SLOT" = "A" ]; then
    TARGET_SLOT="B"
    TARGET_DEV=$SLOT_B_DEV
    TARGET_MNT=$SLOT_B_MNT
    TARGET_PART_NUM=4
    SRC_BOOT_PART_NUM=3
elif [ "$CURRENT_SLOT" = "B" ]; then
    TARGET_SLOT="A"
    TARGET_DEV=$SLOT_A_DEV
    TARGET_MNT=$SLOT_A_MNT
    TARGET_PART_NUM=3
    SRC_BOOT_PART_NUM=4
fi
echo "本次待升级目标分区：SLOT_${TARGET_SLOT}"
echo "目标分区设备：${TARGET_DEV}，分区序号：${TARGET_PART_NUM}"
echo "当前启动分区序号：${SRC_BOOT_PART_NUM}"

# ======================步骤2：校验OTA包完整性（对应manifest里sha256）======================
echo -e "\n=====================【阶段3 OTA包完整性校验】===================="
# 1. 校验OTA包完整性
mkdir -p ${VERIFY_DIR}
tar -xJf ${OTA_PKG_PATH} -C ${VERIFY_DIR}
sync

EXPECT_SHA=$(grep rootfs_sha256 ${VERIFY_DIR}/manifest.txt | cut -d'=' -f2)
LOCAL_SHA=$(sha256sum ${VERIFY_DIR}/rootfs.tar.xz | awk '{print $1}')

if [ "${EXPECT_SHA}" != "${LOCAL_SHA}" ];then
    rm -rf ${OTA_PKG_PATH} ${VERIFY_DIR}
    echo "OTA校验失败，终止升级"
    exit 1
fi

echo "SHA256校验通过！"

# ======================步骤3：清空目标分区，解压新版rootfs（保留分区自有extlinux/fstab）======================
echo -e "\n=====================【阶段4 处理目标分区并写入系统】===================="
# 2. 挂载目标分区并备份专属extlinux/fstab
umount ${TARGET_MNT} 2>/dev/null || true
mkdir -p ${TARGET_MNT}
mount ${TARGET_DEV} ${TARGET_MNT}
sync
cp -r ${TARGET_MNT}/boot/extlinux /data/extlinux_bak
cp ${TARGET_MNT}/etc/fstab /data/fstab_bak

# 清空分区，不再把ota完整包写入目标分区，节省空间
rm -rf ${TARGET_MNT}/*
sync

# 直接从/data校验目录解压rootfs到目标分区，无中间压缩包占用
tar -xJf ${VERIFY_DIR}/rootfs.tar.xz -C ${TARGET_MNT}
sync

# 恢复你预配置的目标分区启动参数
mkdir -p ${TARGET_MNT}/boot/extlinux
cp -r /data/extlinux_bak/* ${TARGET_MNT}/boot/extlinux/
cp /data/fstab_bak ${TARGET_MNT}/etc/fstab

# 清理临时文件
rm -rf ${VERIFY_DIR} /data/extlinux_bak /data/fstab_bak
sync

echo "目标分区系统写入完成"

# ======================步骤4：切换启动标记（自动确认GPT容量警告）======================
echo -e "\n=====================【阶段5 切换分区boot启动标记】===================="
# 3. 关闭原分区boot标记，开启目标分区boot标记
echo "关闭原分区${SRC_BOOT_PART_NUM}的boot启动标识"
parted --fix -s ${PART_PREFIX} set ${SRC_BOOT_PART_NUM} boot off 2>/dev/null

echo "开启目标分区${TARGET_PART_NUM}的boot启动标识"
parted --fix -s ${PART_PREFIX} set ${TARGET_PART_NUM} boot on 2>/dev/null
udevadm trigger --subsystem-match=block
sync

# 校验标记是否生效
parted -s ${PART_PREFIX} print | grep -E "${SLOT_A_PART#p}|${SLOT_B_PART#p}"

# ======================步骤5：卸载分区、打印日志并重启进入新分区======================
echo -e "\n=====================【阶段6 收尾卸载与重启】===================="
umount ${TARGET_MNT}
sync

# 打印升级成功日志
echo -e "\n=====================【OTA升级全部流程执行完成】====================="
echo "当前运行分区：SLOT_${CURRENT_SLOT}"
echo "即将重启进入新版系统分区：SLOT_${TARGET_SLOT}"
echo "======================================================================"

# reboot