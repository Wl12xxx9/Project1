#!/bin/bash

# arguments: $RELEASE $LINUXFAMILY $BOARD $BUILD_DESKTOP
#
# This is the image customization script

# NOTE: It is copied to /tmp directory inside the image
# and executed there inside chroot environment
# so don't reference any files that are not already installed

# NOTE: If you want to transfer files between chroot and host
# userpatches/overlay directory on host is bind-mounted to /tmp/overlay in chroot
# The sd card's root path is accessible via $SDCARD variable.

RELEASE=$1
LINUXFAMILY=$2
BOARD=$3
BUILD_DESKTOP=$4

# ===================== 新增：USB自动挂载独立函数 =====================
SetupUsbAutoMount() {
	# 非交互模式安装依赖
	DEBIAN_FRONTEND=noninteractive apt-get install -y --no-upgrade --no-install-recommends exfatprogs

	# 写入 udev 自动挂载规则
	cat > /etc/udev/rules.d/99-usb-automount.rules << EOF
ACTION=="add", SUBSYSTEM=="block", SUBSYSTEMS=="usb", KERNEL=="sd[a-z][0-9]", 
RUN+="/bin/mkdir -p /media/usb-%k", 
RUN+="/usr/bin/systemd-mount --no-block --collect -o uid=0,gid=0,umask=000 /dev/%k /media/usb-%k"

ACTION=="remove", SUBSYSTEM=="block", SUBSYSTEMS=="usb", KERNEL=="sd[a-z][0-9]", 
RUN+="/usr/bin/systemd-umount --lazy /media/usb-%k"
EOF

	# 重载udev规则
	udevadm control --reload-rules
}
# ======================================================================


# ==================================================
# 镜像瘦身优化函数
# 适配硬件：RK3566 + AIC8800/RTL8821CS双WiFi+蓝牙 + USB网卡 + STM32 USB-CAN + UART + SD卡
# 业务依赖：U盘自动挂载 / MQTT / HTTPS OTA下载 / AB分区升级
# ==================================================
image_slim_optimize() {
    local GREEN='\033[32m'
    local YELLOW='\033[33m'
    local RESET='\033[0m'

    echo -e "${GREEN}[镜像瘦身] ==========================================${RESET}"
    echo -e "${GREEN}[镜像瘦身] 开始执行系统精简优化...${RESET}"
    echo -e "${GREEN}[镜像瘦身] ==========================================${RESET}"

    # --------------------------
    # 1. 基础系统配置
    # --------------------------
    echo -e "\n${YELLOW}[1/7] 配置基础系统（跳过向导、预置账号时区）${RESET}"
    
    rm -f /root/.not_logged_in_yet
    echo "[镜像瘦身] 已禁用首次登录向导"

    echo "root:123" | chpasswd
    echo "[镜像瘦身] 已预置root默认密码"

    # ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime
    # echo "Asia/Shanghai" > /etc/timezone

    # --------------------------
    # 2. APT体系全量清理
    # --------------------------
    echo -e "\n${YELLOW}[2/7] 清理APT缓存与索引${RESET}"
    
    apt clean -y > /dev/null 2>&1
    rm -rf /var/cache/apt/*.bin
    rm -rf /var/lib/apt/lists/*
    echo "[镜像瘦身] APT缓存/索引清理完成"

    # --------------------------
    # 3. /boot 分区冗余文件清理（可选，调试建议注释）
    # --------------------------
    echo -e "\n${YELLOW}[3/7] 清理/boot分区冗余文件${RESET}"
    # rm -f /boot/System.map-*
    # rm -f /boot/config-*

    # --------------------------
    # 4. /usr/share 冗余资源清理
    # --------------------------
    echo -e "\n${YELLOW}[4/7] 清理/usr/share冗余资源${RESET}"
    
    rm -rf /usr/share/doc /usr/share/man /usr/share/info /usr/share/common-licenses
    rm -rf /usr/share/perl*
    # 仅删除非英文，保留/usr/share/i18n（U盘中文挂载必备）
    if [ -d /usr/share/locale ]; then
        find /usr/share/locale -type d ! -name "en" ! -path "/usr/share/locale" -exec rm -rf {} + 2>/dev/null
    fi
    # rm -rf /usr/share/i18n  # 禁止删除，注释掉
    rm -rf /usr/share/X11 /usr/share/bash-completion /usr/share/tcltk /usr/share/zsh
    rm -rf /usr/share/figlet /usr/share/sounds /usr/share/icons /usr/share/pixmaps
    rm -rf /usr/share/groff /usr/share/consolefonts /usr/share/console-setup
    # rm -rf /usr/share/alsa 无音频可删，保留注释
    echo "[镜像瘦身] /usr/share清理完成"

    # --------------------------
    # 5. /usr/lib 系统库冗余清理【大量修复】
    # --------------------------
    echo -e "\n${YELLOW}[5/7] 清理/usr/lib系统库冗余${RESET}"
    
    rm -rf /usr/lib/linux-image-* /usr/lib/linux-u-boot-*
    rm -rf /usr/lib/armbian-install /usr/lib/armbian /usr/lib/nand-sata-install

    # rm -rf /usr/lib/aarch64-linux-gnu/perl*
    # rm -rf /usr/lib/aarch64-linux-gnu/gconv

    # rm -rf /usr/lib/aarch64-linux-gnu/rsyslog  # OTA日志保留，注释
    # rm -f /usr/lib/aarch64-linux-gnu/libapt-pkg.so*
    # rm -rf /usr/lib/apt

    rm -rf /usr/lib/man-db /usr/lib/valgrind /usr/lib/tcltk /usr/lib/groff
    if [ -d /usr/lib/locale ]; then
        find /usr/lib/locale -type d ! -name "en" ! -path "/usr/lib/locale" -exec rm -rf {} + 2>/dev/null
    fi

    # 危险库全部注释，禁止删除
    # rm -rf /usr/lib/sasl2  MQTT/TLS依赖
    # rm -rf /usr/lib/pam.d 登录权限依赖
    rm -rf /usr/lib/console-setup /usr/lib/mime /usr/lib/lsb
    rm -rf /usr/lib/pm-utils /usr/lib/sftp-server
    echo "[镜像瘦身] /usr/lib清理完成（SASL/PAM/rsyslog已保护）"

    # --------------------------
    # 6. 内核驱动模块精简【重点修复】
    # --------------------------
    echo -e "\n${YELLOW}[6/7] 精简内核驱动模块${RESET}"
    
    local KERNEL_VER=$(ls /usr/lib/modules/ 2>/dev/null | head -1)
    if [ -z "$KERNEL_VER" ]; then
        echo "[镜像瘦身] ⚠️  未找到内核模块目录，跳过驱动精简"
    else
        local MODULES_KERNEL="/usr/lib/modules/${KERNEL_VER}"
        echo "[镜像瘦身] 当前内核版本: ${KERNEL_VER}"

        # 删除驱动：移除staging整体删除，保留WiFi驱动目录
        if [ -d "${MODULES_KERNEL}/drivers" ]; then
            rm -rf ${MODULES_KERNEL}/drivers/{accel,atm,cxkl,dax,edac,iommu,mailbox,mux,nfc,nvme,pci,perf,pps,ptp,target,vhost,vfio,virt,virtio,w1,xen,gnss,cdrom}
            # media/video/gpu/AIC摄像头用到则注释，无摄像头可保留删除
            # rm -rf ${MODULES_KERNEL}/drivers/{gpu,video,media}
            echo "[镜像瘦身] 已删除虚拟化/无用驱动，保留staging无线目录"
        fi

        # 文件系统：保护ntfs3/fuse/overlayfs/pstore/binfmt_misc
        if [ -d "${MODULES_KERNEL}/fs" ]; then
            rm -rf ${MODULES_KERNEL}/fs/{9p,adfs,affs,befs,bfs,btrfs,cachefiles,ceph,coda,cramfs,dlm,f2fs,hfs,hfsplus,hpfs,iso9660,jffs2,jfs,lockd,minix,nfs,nfsd,nilfs2,ocfs2,omfs,orangeefs,qnx4,qnx6,quota,romfs,smb,udf,ufs,xfs,zonefs}
            echo "[镜像瘦身] 仅删除网络/老旧文件系统，保留ntfs3/fuse/overlayfs"
        fi

        depmod -a "${KERNEL_VER}" > /dev/null 2>&1
        echo "[镜像瘦身] 重建模块依赖"
    fi

    # --------------------------
    # 7. 硬件固件精简【小幅优化】
    # --------------------------
    echo -e "\n${YELLOW}[7/7] 精简硬件固件库${RESET}"
    
    local FIRMWARE_DIR="/usr/lib/firmware"
    if [ -d "$FIRMWARE_DIR" ]; then
        rm -rf ${FIRMWARE_DIR}/qcom
        rm -rf ${FIRMWARE_DIR}/{brcm,ath11k,ath10k,mediatek,intel,ap6212,ap6275p,ap6210,cypress,ti-connectivity,qca,xr819,ssv6051,ssv6x5x,rt2870}
        rm -rf ${FIRMWARE_DIR}/iwlwifi-*
        rm -rf ${FIRMWARE_DIR}/{vpu,video,meson,novatek,s5p-mfc-v8.fw,v4l-coda960-*}
        rm -rf ${FIRMWARE_DIR}/dvb-* ${FIRMWARE_DIR}/xc3028* ${FIRMWARE_DIR}/xc4000*
        # 注释RK DMA固件删除
        # rm -rf ${FIRMWARE_DIR}/{uwe5622,arm,imx,edid,cirrus,sdma,renesas_usb_fw.mem}
        rm -rf ${FIRMWARE_DIR}/README.md ${FIRMWARE_DIR}/nvram_*.txt ${FIRMWARE_DIR}/bt_configure_*.ini
        rm -f ${FIRMWARE_DIR}/wifi_2355b001_1ant.ini ${FIRMWARE_DIR}/wcnmodem.bin ${FIRMWARE_DIR}/mt76*
        echo "[镜像瘦身] 固件清理完成，保留RK DMA、RTL/AIC无线固件"
    fi

    echo -e "\n${GREEN}[镜像瘦身] ==========================================${RESET}"
    echo -e "${GREEN}[镜像瘦身] 系统精简优化执行完成！${RESET}"
    echo -e "${GREEN}[镜像瘦身] ==========================================${RESET}\n"
}


Main() {
	case $RELEASE in
		noble)
			# ====================== 新增这里 ======================
			# 1. 核心：删除首次开机标记，彻底禁用所有向导
			rm /root/.not_logged_in_yet

			# 2. 预置root密码（按需修改，跳过改密码弹窗）
			echo "root:123" | chpasswd

			# 3. 预置上海时区，跳过时区选择
			# ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime
			# echo "Asia/Shanghai" > /etc/timezone

			# 4. 预置中文UTF-8 locale，跳过语言选择
			# sed -i 's/# zh_CN.UTF-8 UTF-8/zh_CN.UTF-8 UTF-8/' /etc/locale.gen
			# locale-gen zh_CN.UTF-8
			# echo "LANG=zh_CN.UTF-8" > /etc/default/locale

			# 5. 禁用首次运行systemd服务（双重保险）
			# systemctl disable --now armbian-firstrun-config 2>/dev/null
			# ======================================================

			###########################################################################
			# 1. 禁用冗余 systemd 自启服务
			###########################################################################
			# systemctl disable NetworkManager-wait-online.service
			# systemctl daemon-reload
			# DISABLE_SERVICES=(
			# 	NetworkManager NetworkManager-dispatcher NetworkManager-wait-online
			# 	openvpn 
			# )

			# for svc in "${DISABLE_SERVICES[@]}"; do
			# 	systemctl disable "$svc.service"
			# done

			# 屏蔽网络等待服务，解决开机卡顿
			ln -sf /dev/null /etc/systemd/system/systemd-networkd-wait-online.service
			ln -sf /dev/null /etc/systemd/system/NetworkManager-wait-online.service
			# 屏蔽无网卡依赖的 vnstat 服务
			ln -sf /dev/null /etc/systemd/system/vnstat.service
			# 可选：屏蔽其他启动报错的冗余服务
			ln -sf /dev/null /etc/systemd/system/armbian-zram-config.service
			ln -sf /dev/null /etc/systemd/system/smartmontools.service

			###########################################################################
			# 5. 双网口静态IP（替代 NetworkManager）
			###########################################################################
			# cat > /etc/network/interfaces << EOF
			# auto eth0
			# iface eth0 inet static
			#     address 192.168.1.100
			#     netmask 255.255.255.0
			#     gateway 192.168.1.1
			#     dns-nameservers 223.5.5.5

			# auto eth1
			# iface eth1 inet static
			#     address 192.168.2.100
			#     netmask 255.255.255.0
			# EOF

			# ========== USB自动挂载 开始 ==========
			# SetupUsbAutoMount
			# ========== USB自动挂载 结束 ==========

			# ========== chroot 内编译 AIC8800 SDIO 驱动 ==========
			# 定义路径（替换为你本地真实路径）
			# export ARMBIAN_ROOT="/home/qiaoxi/project/armbian-build/build"
			# export KERNEL_PATH="${ARMBIAN_ROOT}/cache/sources/linux-kernel-worktree/6.18__rockchip64__arm64"
			# export AIC_DRIVER="${ARMBIAN_ROOT}/userpatches/overlay/opt/aic8800"

			# # 进入驱动目录编译
			# cd ${AIC_DRIVER}
			# make clean
			# # 标准交叉编译指令
			# make -C ${KERNEL_PATH} M=$(pwd) ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- modules
			# =====================================================

            # 调用镜像瘦身函数
            image_slim_optimize
			;;
		stretch)
			# your code here
			# InstallOpenMediaVault # uncomment to get an OMV 4 image
			;;
		buster)
			# your code here
			;;
		bullseye)
			# your code here
			;;
		bionic)
			# your code here
			;;
		focal)
			# your code here
			;;
	esac
} # Main

InstallOpenMediaVault() {
	# use this routine to create a Debian based fully functional OpenMediaVault
	# image (OMV 3 on Jessie, OMV 4 with Stretch). Use of mainline kernel highly
	# recommended!
	#
	# Please note that this variant changes Armbian default security 
	# policies since you end up with root password 'openmediavault' which
	# you have to change yourself later. SSH login as root has to be enabled
	# through OMV web UI first
	#
	# This routine is based on idea/code courtesy Benny Stark. For fixes,
	# discussion and feature requests please refer to
	# https://forum.armbian.com/index.php?/topic/2644-openmediavault-3x-customize-imagesh/

	echo root:openmediavault | chpasswd
	rm /root/.not_logged_in_yet
	. /etc/default/cpufrequtils
	export LANG=C LC_ALL="en_US.UTF-8"
	export DEBIAN_FRONTEND=noninteractive
	export APT_LISTCHANGES_FRONTEND=none

	case ${RELEASE} in
		jessie)
			OMV_Name="erasmus"
			OMV_EXTRAS_URL="https://github.com/OpenMediaVault-Plugin-Developers/packages/raw/master/openmediavault-omvextrasorg_latest_all3.deb"
			;;
		stretch)
			OMV_Name="arrakis"
			OMV_EXTRAS_URL="https://github.com/OpenMediaVault-Plugin-Developers/packages/raw/master/openmediavault-omvextrasorg_latest_all4.deb"
			;;
	esac

	# Add OMV source.list and Update System
	cat > /etc/apt/sources.list.d/openmediavault.list <<- EOF
	deb https://openmediavault.github.io/packages/ ${OMV_Name} main
	## Uncomment the following line to add software from the proposed repository.
	deb https://openmediavault.github.io/packages/ ${OMV_Name}-proposed main
	
	## This software is not part of OpenMediaVault, but is offered by third-party
	## developers as a service to OpenMediaVault users.
	# deb https://openmediavault.github.io/packages/ ${OMV_Name} partner
	EOF

	# Add OMV and OMV Plugin developer keys, add Cloudshell 2 repo for XU4
	if [ "${BOARD}" = "odroidxu4" ]; then
		add-apt-repository -y ppa:kyle1117/ppa
		sed -i 's/jessie/xenial/' /etc/apt/sources.list.d/kyle1117-ppa-jessie.list
	fi
	mount --bind /dev/null /proc/mdstat
	apt-get update
	apt-get --yes --force-yes --allow-unauthenticated install openmediavault-keyring
	apt-key adv --keyserver hkp://keyserver.ubuntu.com:80 --recv-keys 7AA630A1EDEE7D73
	apt-get update

	# install debconf-utils, postfix and OMV
	HOSTNAME="${BOARD}"
	debconf-set-selections <<< "postfix postfix/mailname string ${HOSTNAME}"
	debconf-set-selections <<< "postfix postfix/main_mailer_type string 'No configuration'"
	apt-get --yes --force-yes --allow-unauthenticated  --fix-missing --no-install-recommends \
		-o Dpkg::Options::="--force-confdef" -o Dpkg::Options::="--force-confold" install \
		debconf-utils postfix
	# move newaliases temporarely out of the way (see Ubuntu bug 1531299)
	cp -p /usr/bin/newaliases /usr/bin/newaliases.bak && ln -sf /bin/true /usr/bin/newaliases
	sed -i -e "s/^::1         localhost.*/::1         ${HOSTNAME} localhost ip6-localhost ip6-loopback/" \
		-e "s/^127.0.0.1   localhost.*/127.0.0.1   ${HOSTNAME} localhost/" /etc/hosts
	sed -i -e "s/^mydestination =.*/mydestination = ${HOSTNAME}, localhost.localdomain, localhost/" \
		-e "s/^myhostname =.*/myhostname = ${HOSTNAME}/" /etc/postfix/main.cf
	apt-get --yes --force-yes --allow-unauthenticated  --fix-missing --no-install-recommends \
		-o Dpkg::Options::="--force-confdef" -o Dpkg::Options::="--force-confold" install \
		openmediavault

	# install OMV extras, enable folder2ram and tweak some settings
	FILE=$(mktemp)
	curl "$OMV_EXTRAS_URL" -fLso "$FILE" && dpkg -i "$FILE"
	
	/usr/sbin/omv-update
	# Install flashmemory plugin and netatalk by default, use nice logo for the latter,
	# tweak some OMV settings
	. /usr/share/openmediavault/scripts/helper-functions
	apt-get -y -q install openmediavault-netatalk openmediavault-flashmemory
	AFP_Options="mimic model = Macmini"
	SMB_Options="min receivefile size = 16384\nwrite cache size = 524288\ngetwd cache = yes\nsocket options = TCP_NODELAY IPTOS_LOWDELAY"
	xmlstarlet ed -L -u "/config/services/afp/extraoptions" -v "$(echo -e "${AFP_Options}")" /etc/openmediavault/config.xml
	xmlstarlet ed -L -u "/config/services/smb/extraoptions" -v "$(echo -e "${SMB_Options}")" /etc/openmediavault/config.xml
	xmlstarlet ed -L -u "/config/services/flashmemory/enable" -v "1" /etc/openmediavault/config.xml
	xmlstarlet ed -L -u "/config/services/ssh/enable" -v "1" /etc/openmediavault/config.xml
	xmlstarlet ed -L -u "/config/services/ssh/permitrootlogin" -v "0" /etc/openmediavault/config.xml
	xmlstarlet ed -L -u "/config/system/time/ntp/enable" -v "1" /etc/openmediavault/config.xml
	xmlstarlet ed -L -u "/config/system/time/timezone" -v "UTC" /etc/openmediavault/config.xml
	xmlstarlet ed -L -u "/config/system/network/dns/hostname" -v "${HOSTNAME}" /etc/openmediavault/config.xml
	xmlstarlet ed -L -u "/config/system/monitoring/perfstats/enable" -v "0" /etc/openmediavault/config.xml
	echo -e "OMV_CPUFREQUTILS_GOVERNOR=${GOVERNOR}" >>/etc/default/openmediavault
	echo -e "OMV_CPUFREQUTILS_MINSPEED=${MIN_SPEED}" >>/etc/default/openmediavault
	echo -e "OMV_CPUFREQUTILS_MAXSPEED=${MAX_SPEED}" >>/etc/default/openmediavault
	for i in netatalk samba flashmemory ssh ntp timezone interfaces cpufrequtils monit collectd rrdcached ; do
		/usr/sbin/omv-mkconf $i
	done
	/sbin/folder2ram -enablesystemd || true
	sed -i 's|-j /var/lib/rrdcached/journal/ ||' /etc/init.d/rrdcached

	# Fix multiple sources entry on ARM with OMV4
	sed -i '/stretch-backports/d' /etc/apt/sources.list

	# rootfs resize to 7.3G max and adding omv-initsystem to firstrun -- q&d but shouldn't matter
	echo 15500000s >/root/.rootfs_resize
	sed -i '/systemctl\ disable\ armbian-firstrun/i \
	mv /usr/bin/newaliases.bak /usr/bin/newaliases \
	export DEBIAN_FRONTEND=noninteractive \
	sleep 3 \
	apt-get install -f -qq python-pip python-setuptools || exit 0 \
	pip install -U tzupdate \
	tzupdate \
	read TZ </etc/timezone \
	/usr/sbin/omv-initsystem \
	xmlstarlet ed -L -u "/config/system/time/timezone" -v "${TZ}" /etc/openmediavault/config.xml \
	/usr/sbin/omv-mkconf timezone \
	lsusb | egrep -q "0b95:1790|0b95:178a|0df6:0072" || sed -i "/ax88179_178a/d" /etc/modules' /usr/lib/armbian/armbian-firstrun
	sed -i '/systemctl\ disable\ armbian-firstrun/a \
	sleep 30 && sync && reboot' /usr/lib/armbian/armbian-firstrun

	# add USB3 Gigabit Ethernet support
	echo -e "r8152\nax88179_178a" >>/etc/modules

	# Special treatment for ODROID-XU4 (and later Amlogic S912, RK3399 and other big.LITTLE
	# based devices). Move all NAS daemons to the big cores. With ODROID-XU4 a lot
	# more tweaks are needed. CS2 repo added, CS1 workaround added, coherent_pool=1M
	# set: https://forum.odroid.com/viewtopic.php?f=146&t=26016&start=200#p197729
	# (latter not necessary any more since we fixed it upstream in Armbian)
	case ${BOARD} in
		odroidxu4)
			HMP_Fix='; taskset -c -p 4-7 $i '
			# Cloudshell stuff (fan, lcd, missing serials on 1st CS2 batch)
			echo "H4sIAKdXHVkCA7WQXWuDMBiFr+eveOe6FcbSrEIH3WihWx0rtVbUFQqCqAkYGhJn
			tF1x/vep+7oebDfh5DmHwJOzUxwzgeNIpRp9zWRegDPznya4VDlWTXXbpS58XJtD
			i7ICmFBFxDmgI6AXSLgsiUop54gnBC40rkoVA9rDG0SHHaBHPQx16GN3Zs/XqxBD
			leVMFNAz6n6zSWlEAIlhEw8p4xTyFtwBkdoJTVIJ+sz3Xa9iZEMFkXk9mQT6cGSQ
			QL+Cr8rJJSmTouuuRzfDtluarm1aLVHksgWmvanm5sbfOmY3JEztWu5tV9bCXn4S
			HB8RIzjoUbGvFvPw/tmr0UMr6bWSBupVrulY2xp9T1bruWnVga7DdAqYFgkuCd3j
			vORUDQgej9HPJxmDDv+3WxblBSuYFH8oiNpHz8XvPIkU9B3JVCJ/awIAAA==" \
			| tr -d '[:blank:]' | base64 --decode | gunzip -c >/usr/local/sbin/cloudshell2-support.sh
			chmod 755 /usr/local/sbin/cloudshell2-support.sh
			apt install -y i2c-tools odroid-cloudshell cloudshell2-fan
			sed -i '/systemctl\ disable\ armbian-firstrun/i \
			lsusb | grep -q -i "05e3:0735" && sed -i "/exit\ 0/i echo 20 > /sys/class/block/sda/queue/max_sectors_kb" /etc/rc.local \
			/usr/sbin/i2cdetect -y 1 | grep -q "60: 60" && /usr/local/sbin/cloudshell2-support.sh' /usr/lib/armbian/armbian-firstrun
			;;
		bananapim3)
			HMP_Fix='; taskset -c -p 4-7 $i '
			;;
		edge*|ficus|firefly-rk3399|nanopct4|nanopim4|nanopineo4|renegade-elite|roc-rk3399-pc|rockpro64|station-p1)
			HMP_Fix='; taskset -c -p 4-5 $i '
			;;
	esac
	echo "* * * * * root for i in \`pgrep \"ftpd|nfsiod|smbd|afpd|cnid\"\` ; do ionice -c1 -p \$i ${HMP_Fix}; done >/dev/null 2>&1" \
		>/etc/cron.d/make_nas_processes_faster
	chmod 600 /etc/cron.d/make_nas_processes_faster

	# add SATA port multiplier hint if appropriate
	[ "${LINUXFAMILY}" = "sunxi" ] && \
		echo -e "#\n# If you want to use a SATA PM add \"ahci_sunxi.enable_pmp=1\" to bootargs above" \
		>>/boot/boot.cmd

	# Filter out some log messages
	echo ':msg, contains, "do ionice -c1" ~' >/etc/rsyslog.d/omv-armbian.conf
	echo ':msg, contains, "action " ~' >>/etc/rsyslog.d/omv-armbian.conf
	echo ':msg, contains, "netsnmp_assert" ~' >>/etc/rsyslog.d/omv-armbian.conf
	echo ':msg, contains, "Failed to initiate sched scan" ~' >>/etc/rsyslog.d/omv-armbian.conf

	# Fix little python bug upstream Debian 9 obviously ignores
	if [ -f /usr/lib/python3.5/weakref.py ]; then
		GITREF="9cd7e17640a49635d1c1f8c2989578a8fc2c1de6"
		curl -fLo /usr/lib/python3.5/weakref.py \
			"https://raw.githubusercontent.com/python/cpython/${GITREF}/Lib/weakref.py"
	fi

	# clean up and force password change on first boot
	umount /proc/mdstat
	chage -d 0 root
} # InstallOpenMediaVault

UnattendedStorageBenchmark() {
	# Function to create Armbian images ready for unattended storage performance testing.
	# Useful to use the same OS image with a bunch of different SD cards or eMMC modules
	# to test for performance differences without wasting too much time.

	rm /root/.not_logged_in_yet

	apt-get -qq install time

	curl -fLso /usr/local/bin/sd-card-bench.sh "https://raw.githubusercontent.com/ThomasKaiser/sbc-bench/master/sd-card-bench.sh"
	chmod 755 /usr/local/bin/sd-card-bench.sh

	sed -i '/^exit\ 0$/i \
	/usr/local/bin/sd-card-bench.sh &' /etc/rc.local
} # UnattendedStorageBenchmark

InstallAdvancedDesktop()
{
	apt-get install -yy transmission libreoffice libreoffice-style-tango meld remmina thunderbird kazam avahi-daemon
	[[ -f /usr/share/doc/avahi-daemon/examples/sftp-ssh.service ]] && cp /usr/share/doc/avahi-daemon/examples/sftp-ssh.service /etc/avahi/services/
	[[ -f /usr/share/doc/avahi-daemon/examples/ssh.service ]] && cp /usr/share/doc/avahi-daemon/examples/ssh.service /etc/avahi/services/
	apt clean
} # InstallAdvancedDesktop

Main "$@"
