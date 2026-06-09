RK3566主控芯片外设适配
软件基于rk3566-nanopi-r3s-lts进行开发
适配项有：USB，UART,EMMC,DDR,无线模块等

V1:
基本功能调通
编译指令：
./compile.sh build BOARD=nanopi-r3s-lts BRANCH=current BUILD_DESKTOP=no BUILD_MINIMAL=no KERNEL_CONFIGURE=no RELEASE=noble GITHUB_MIRROR=ghproxy DOWNLOAD_MIRROR=china GHCR_MIRROR_ADDRESS=ghcr.libcuda.so GHCR_MIRROR=dockerproxy CLEAN_LEVEL="sources,make,debs,patch" DEBUG_PATCHING=yes ENABLE_EXTENSIONS="radxa-aic8800" KERNEL_GIT=shallow

Image: Armbian-unofficial_26.05.0-trunk_Nanopi-r3s-lts_noble_current_6.18.34-6.9.1.img


