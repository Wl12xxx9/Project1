#!/bin/bash
set -e
# ====================== 全局硬件引脚配置 ======================
# USB STM32F407 DFU
USB_RST_GPIO=143
USB_BOOT_GPIO=141

# CAN通道两路MCU
CAN0_RST_GPIO=40    # GPIO1_B0_D RST0
CAN0_BOOT_GPIO=36   # GPIO1_A4_D BT0
CAN1_RST_GPIO=42    # GPIO1_B2_D RST1
CAN1_BOOT_GPIO=37   # GPIO1_B1_D BT1

# 串口设备节点（按需修改）
UART_DEV0="/dev/ttyUSB0"
UART_DEV1="/dev/ttyUSB1"

# CAN总线接口
CAN_IF="can0"

# 固件路径
FIRMWARE_USB="./out/klipper.bin"
FIRMWARE_UART="./out/klipper.bin"
FIRMWARE_CAN="$HOME/klipper/out/klipper.bin"

# 日志文件
LOG_FILE="/data/log/mcu_upgrade.log"

# ====================== 通用工具函数 ======================
# 日志打印
log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] $1" | tee -a $LOG_FILE
}

# 导出GPIO并设为输出
gpio_export_out() {
    local gpio=$1
    if [ ! -d "/sys/class/gpio/gpio$gpio" ];then
        echo $gpio > /sys/class/gpio/export
        sleep 0.02
    fi
    echo out > /sys/class/gpio/gpio$gpio/direction
}

# 释放GPIO
gpio_unexport() {
    local gpio=$1
    if [ -d "/sys/class/gpio/gpio$gpio" ];then
        echo $gpio > /sys/class/gpio/unexport
    fi
}

# 批量释放所有占用GPIO
gpio_clean_all() {
    log "释放全部GPIO资源"
    gpio_unexport $USB_RST_GPIO
    gpio_unexport $USB_BOOT_GPIO
    gpio_unexport $CAN0_RST_GPIO
    gpio_unexport $CAN0_BOOT_GPIO
    gpio_unexport $CAN1_RST_GPIO
    gpio_unexport $CAN1_BOOT_GPIO
}

# ====================== 方案A：USB DFU自动烧录（时序完全对齐你的优化版） ======================
upgrade_usb_mcu() {
    log "========== 开始升级USB STM32F407 DFU =========="
    if [ ! -f "$FIRMWARE_USB" ];then
        log "错误：USB固件 $FIRMWARE_USB 不存在"
        return 1
    fi

    # 导出GPIO
    gpio_export_out $USB_RST_GPIO
    gpio_export_out $USB_BOOT_GPIO

    # 初始状态：BOOT低、RST高（设备正常运行态）
    echo 0 > /sys/class/gpio/gpio$USB_BOOT_GPIO/value
    echo 1 > /sys/class/gpio/gpio$USB_RST_GPIO/value
    sleep 0.05

    # 1. 拉高BOOT0，准备DFU启动条件
    echo 1 > /sys/class/gpio/gpio$USB_BOOT_GPIO/value
    sleep 0.1

    # 2. 拉低RST复位MCU
    echo 0 > /sys/class/gpio/gpio$USB_RST_GPIO/value
    sleep 0.1

    # 3. 释放RST，维持BOOT高等待芯片采样BOOT引脚
    echo 1 > /sys/class/gpio/gpio$USB_RST_GPIO/value
    sleep 0.2

    # 4. BOOT拉低，DFU枚举完成，等待USB识别
    echo 0 > /sys/class/gpio/gpio$USB_BOOT_GPIO/value
    sleep 0.1

    # 执行DFU烧录
    log "执行dfu-util烧录..."
    dfu-util -a 0 -s 0x08000000:leave -D "$FIRMWARE_USB"
    if [ $? -eq 0 ];then
        log "USB MCU烧录成功"
    else
        log "USB MCU烧录失败！"
        gpio_clean_all
        exit 2
    fi

    # 复位MCU回到正常运行模式
    echo 0 > /sys/class/gpio/gpio$USB_RST_GPIO/value
    sleep 0.1
    echo 1 > /sys/class/gpio/gpio$USB_RST_GPIO/value
    log "USB MCU复位完成，进入工作模式"
    log "========== USB MCU升级完成 ==========\n"
}

# ====================== 方案B：串口STM32烧录 ======================
upgrade_uart_mcu() {
    local dev=$1
    local name=$2
    log "========== 开始升级串口$name MCU 设备:$dev =========="
    if [ ! -f "$FIRMWARE_UART" ];then
        log "错误：串口固件 $FIRMWARE_UART 不存在"
        return 1
    fi
    if [ ! -c "$dev" ];then
        log "警告：串口设备 $dev 不存在，跳过"
        return 0
    fi

    log "执行stm32flash烧录 $dev"
    stm32flash -w "$FIRMWARE_UART" -v -g 0x08000000 "$dev"
    if [ $? -eq 0 ];then
        log "串口$name MCU烧录成功"
    else
        log "串口$name MCU烧录失败！"
        exit 3
    fi
    log "========== 串口$name MCU升级完成 ==========\n"
}

# ====================== 方案C：CAN总线MCU烧录 ======================
# 参数：rst引脚 boot引脚 设备uuid 编号
upgrade_can_mcu() {
    local rst_gpio=$1
    local boot_gpio=$2
    local mcu_uuid=$3
    local mcu_no=$4

    log "========== 开始升级CAN$mcu_no MCU UUID:$mcu_uuid =========="
    if [ ! -f "$FIRMWARE_CAN" ];then
        log "错误：CAN固件 $FIRMWARE_CAN 不存在"
        return 1
    fi

    # 导出GPIO
    gpio_export_out $rst_gpio
    gpio_export_out $boot_gpio

    # CAN进入烧录时序：RST+BOOT同时拉高保持300ms
    echo 1 > /sys/class/gpio/gpio$rst_gpio/value
    echo 1 > /sys/class/gpio/gpio$boot_gpio/value
    sleep 0.3

    # 释放RST
    echo 0 > /sys/class/gpio/gpio$rst_gpio/value
    sleep 0.2

    # 释放BOOT
    echo 0 > /sys/class/gpio/gpio$boot_gpio/value
    sleep 0.1

    # CAN烧录指令
    log "执行CAN烧录工具，目标UUID:$mcu_uuid"
    python3 flashtool.py -i $CAN_IF -f "$FIRMWARE_CAN" -u $mcu_uuid
    if [ $? -eq 0 ];then
        log "CAN$mcu_no MCU烧录成功"
    else
        log "CAN$mcu_no MCU烧录失败！"
        gpio_clean_all
        exit 4
    fi

    # 复位CAN MCU恢复运行
    echo 1 > /sys/class/gpio/gpio$rst_gpio/value
    sleep 0.1
    echo 0 > /sys/class/gpio/gpio$rst_gpio/value
    sleep 0.1
    echo 1 > /sys/class/gpio/gpio$rst_gpio/value

    log "========== CAN$mcu_no MCU升级完成 ==========\n"
}

# ====================== 主逻辑 & 入参帮助 ======================
show_help() {
    echo "使用方式："
    echo "  $0 all         批量升级全部MCU(USB+串口0/1+CAN0/CAN1)"
    echo "  $0 usb         仅升级USB DFU MCU"
    echo "  $0 uart0       仅升级串口0 MCU"
    echo "  $0 uart1       仅升级串口1 MCU"
    echo "  $0 can0 <UUID> 仅升级CAN0 MCU，必须传入设备UUID"
    echo "  $0 can1 <UUID> 仅升级CAN1 MCU，必须传入设备UUID"
    echo ""
    echo "示例："
    echo "  $0 can0 123456789ABCDEF"
    echo "  $0 all"
}

main() {
    mkdir -p /data/log
    case $1 in
        all)
            log "==================== 整机全部MCU批量升级开始 ===================="
            upgrade_usb_mcu
            upgrade_uart_mcu $UART_DEV0 "0"
            upgrade_uart_mcu $UART_DEV1 "1"
            # 注意：批量模式需手动修改下方两个UUID为你设备实际CAN UUID
            upgrade_can_mcu $CAN0_RST_GPIO $CAN0_BOOT_GPIO "CAN0_UUID_REPLACE" "0"
            upgrade_can_mcu $CAN1_RST_GPIO $CAN1_BOOT_GPIO "CAN1_UUID_REPLACE" "1"
            log "==================== 全部MCU升级流程执行完毕 ===================="
            gpio_clean_all
            log "GPIO资源全部释放"
            ;;
        usb)
            upgrade_usb_mcu
            gpio_clean_all
            ;;
        uart0)
            upgrade_uart_mcu $UART_DEV0 "0"
            ;;
        uart1)
            upgrade_uart_mcu $UART_DEV1 "1"
            ;;
        can0)
            if [ -z "$2" ];then
                log "错误：执行can0必须传入MCU UUID！"
                show_help
                exit 1
            fi
            upgrade_can_mcu $CAN0_RST_GPIO $CAN0_BOOT_GPIO "$2" "0"
            gpio_clean_all
            ;;
        can1)
            if [ -z "$2" ];then
                log "错误：执行can1必须传入MCU UUID！"
                show_help
                exit 1
            fi
            upgrade_can_mcu $CAN1_RST_GPIO $CAN1_BOOT_GPIO "$2" "1"
            gpio_clean_all
            ;;
        *)
            show_help
            exit 0
            ;;
    esac
    log "升级脚本执行正常退出"
}

# 启动主程序
main "$@"