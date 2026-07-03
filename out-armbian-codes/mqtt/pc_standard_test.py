import paho.mqtt.client as mqtt
import json
import time
import threading
import uuid
import logging
from logging.handlers import RotatingFileHandler
import os

# ==================================================
# ========== 🔧 需自行配置的参数区域 START ==========
# ==================================================
# MQTT连接配置（与设备端保持一致）
MQTT_HOST = "4ry508ao2807.vicp.fun"
MQTT_PORT = 34796
MQTT_USER = "admin"
MQTT_PWD = "123"
DEVICE_ID = "PRT_COREXY_20260702_001"  # 目标设备ID，与设备端一致
PROTOCOL_VERSION = "1.0.0"

# 双向TLS证书路径（PC端本地路径）
CA_CERT_PATH = r"E:\OpenSSL-Win64\bin\ca.crt"
CLIENT_CERT_PATH = r"E:\OpenSSL-Win64\bin\client_admin.crt"
CLIENT_KEY_PATH = r"E:\OpenSSL-Win64\bin\client_admin.key"

# 测试配置
CMD_TIMEOUT = 5  # 指令响应超时时间，单位秒
LOG_FILE = "./mqtt_test.log"
# ==================================================
# ========== 🔧 需自行配置的参数区域 END ==========
# ==================================================

# ========== 全局变量 ==========
response_map = {}  # request_id -> 响应结果，用于同步等待
response_cond = threading.Condition() 
client = None

# ========== 日志初始化 ==========
def init_logger():
    logger = logging.getLogger("mqtt_test")
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    
    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    return logger

logger = init_logger()

# ========== 工具函数 ==========
def gen_request_id():
    """生成全局唯一request_id"""
    return f"req_{uuid.uuid4().hex[:16]}"

def build_cmd_payload(cmd_type, data):
    """构造标准下行指令报文"""
    return {
        "request_id": gen_request_id(),
        "timestamp": int(time.time()*1000),
        "cmd_type": cmd_type,
        "version": PROTOCOL_VERSION,
        "data": data
    }

# ========== MQTT回调函数 ==========
def on_connect(client, userdata, flags, rc):
    if rc == 0:
        logger.info("✅ PC测试端 双向TLS连接成功")
        # 订阅所有上行Topic
        topics = [
            (f"device/{DEVICE_ID}/info", 0),
            (f"device/{DEVICE_ID}/status", 0),
            (f"device/{DEVICE_ID}/event", 1),
            (f"device/{DEVICE_ID}/response", 1)
        ]
        client.subscribe(topics)
        logger.info("所有上行Topic订阅完成，开始监听消息")
    else:
        logger.error(f"❌ 连接失败，错误码：{rc}")

def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode())
        topic = msg.topic

        # 设备基础信息（保留消息）
        if topic.endswith("/info"):
            logger.info("\n📥 【设备基础信息】收到保留消息")
            logger.info(f"  内容：{json.dumps(payload, indent=2, ensure_ascii=False)}")
        
        # 实时状态上报
        elif topic.endswith("/status"):
            print_state = payload["data"].get("print_state", "unknown")
            temp = payload["data"]["temperature"]["nozzle"]["current"]
            progress = payload["data"]["print_progress"]["progress_percent"]
            logger.debug(f"📥 【实时状态】状态:{print_state} | 喷嘴:{temp}℃ | 进度:{progress}%")
        
        # 事件告警
        elif topic.endswith("/event"):
            event_level = payload["data"]["event_level"]
            event_type = payload["data"]["event_type"]
            event_code = payload["data"]["event_code"]
            logger.info(f"\n⚠️ 【事件上报】级别:{event_level} | 类型:{event_type} | 编码:{event_code}")
            logger.info(f"  详情：{json.dumps(payload['data']['event_data'], ensure_ascii=False)}")
        
        # 指令响应
        elif topic.endswith("/response"):
            req_id = payload["data"]["request_id"]
            result = payload["data"]["result"]
            with response_cond:
                response_map[req_id] = payload
                response_cond.notify_all()  # 通知等待的线程
            logger.debug(f"📥 【指令响应】request_id:{req_id} | 结果:{result}")

    except Exception as e:
        logger.error(f"消息解析异常：{e}, 原始内容：{msg.payload[:200]}")

# ========== 同步指令发送（等待响应） ==========
def send_cmd_wait(topic_suffix, cmd_type, data):
    payload = build_cmd_payload(cmd_type, data)
    req_id = payload["request_id"]
    topic = f"device/{DEVICE_ID}/cmd/{topic_suffix}"

    with response_cond:
        response_map.pop(req_id, None)  # 清空历史响应

    client.publish(topic, json.dumps(payload, ensure_ascii=False), qos=1)
    logger.info(f"📤 发送指令：{cmd_type} | request_id:{req_id}")

    # 等待响应（通知-等待模式，无轮询）
    with response_cond:
        if not response_cond.wait_for(lambda: req_id in response_map, timeout=CMD_TIMEOUT):
            logger.error(f"⏱️ 指令超时：{cmd_type}，{CMD_TIMEOUT}s未收到响应")
            return False, None
        resp = response_map.pop(req_id)
        return True, resp

# ========== 各类指令封装（对应协议全量指令） ==========
class PrinterCommands:
    """所有下行指令封装，对应协议7大类指令"""

    # 1. 通用查询类
    @staticmethod
    def query_full_status():
        """查询全量状态"""
        return send_cmd_wait("system", "common_query", {"query_type": "full_status"})

    @staticmethod
    def query_version():
        """查询版本信息"""
        return send_cmd_wait("system", "common_query", {"query_type": "version"})

    @staticmethod
    def query_config():
        """查询硬件配置"""
        return send_cmd_wait("system", "common_query", {"query_type": "config"})

    # 2. 参数设置类
    @staticmethod
    def set_temp(nozzle=None, bed=None, fan=None):
        """设置温度/风扇"""
        data = {}
        if nozzle is not None: data["nozzle_target"] = nozzle
        if bed is not None: data["bed_target"] = bed
        if fan is not None: data["fan_speed"] = fan
        return send_cmd_wait("param", "param_set", data)

    @staticmethod
    def set_factor(speed=None, extrude=None):
        """设置速度/挤出倍率"""
        data = {}
        if speed is not None: data["speed_factor"] = speed
        if extrude is not None: data["extrude_factor"] = extrude
        return send_cmd_wait("param", "param_set", data)

    # 3. 运动控制类
    @staticmethod
    def home_all():
        """全轴归零"""
        return send_cmd_wait("motion", "motion_control", {"action": "home_all"})

    @staticmethod
    def jog(axis, distance, speed=10, relative=True):
        """点动移动"""
        return send_cmd_wait("motion", "motion_control", {
            "action": "jog", "axis": axis, "distance": distance, "speed": speed, "relative": relative
        })

    @staticmethod
    def motors_off():
        """电机断电"""
        return send_cmd_wait("motion", "motion_control", {"action": "motors_off"})

    # 4. 打印控制类
    @staticmethod
    def print_start(file_name, start_line=0):
        """开始打印"""
        return send_cmd_wait("print", "print_control", {
            "action": "start", "file_name": file_name, "start_line": start_line
        })

    @staticmethod
    def print_pause():
        """暂停打印"""
        return send_cmd_wait("print", "print_control", {"action": "pause"})

    @staticmethod
    def print_resume():
        """恢复打印"""
        return send_cmd_wait("print", "print_control", {"action": "resume"})

    @staticmethod
    def print_stop():
        """停止/取消打印"""
        return send_cmd_wait("print", "print_control", {"action": "stop"})

    # 5. 文件管理类
    @staticmethod
    def file_list(page=1, page_size=20):
        """获取文件列表"""
        return send_cmd_wait("file", "file_manage", {
            "action": "list", "page": page, "page_size": page_size
        })

    @staticmethod
    def file_delete(file_name):
        """删除文件"""
        return send_cmd_wait("file", "file_manage", {"action": "delete", "file_name": file_name})

    # 6. 系统配置类
    @staticmethod
    def pid_tune(target="extruder", target_temp=200):
        """PID自整定"""
        return send_cmd_wait("system", "system_config", {
            "action": "pid_tune", "target": target, "target_temp": target_temp
        })

    @staticmethod
    def bed_leveling():
        """自动调平"""
        return send_cmd_wait("system", "system_config", {"action": "bed_leveling"})

    @staticmethod
    def mcu_restart():
        """MCU重启"""
        return send_cmd_wait("system", "system_config", {"action": "mcu_restart"})

    # 7. AI摄像头控制类
    @staticmethod
    def ai_set_mode(enable=True, mode="first_layer_detect"):
        """设置AI检测模式"""
        return send_cmd_wait("ai", "ai_camera_control", {
            "action": "set_mode", "enable": enable, "mode": mode
        })

    @staticmethod
    def ai_capture():
        """AI手动抓拍"""
        return send_cmd_wait("ai", "ai_camera_control", {"action": "capture"})

# ========== 自动化测试用例 ==========
def auto_test_all():
    logger.info("="*60)
    logger.info("🚀 开始自动化全量功能测试")
    logger.info("="*60)

    test_cases = [
        ("查询全量状态", lambda: PrinterCommands.query_full_status()),
        ("查询版本信息", lambda: PrinterCommands.query_version()),
        ("查询硬件配置", lambda: PrinterCommands.query_config()),
        ("设置喷嘴温度200℃", lambda: PrinterCommands.set_temp(nozzle=200)),
        ("设置热床温度60℃", lambda: PrinterCommands.set_temp(bed=60)),
        ("设置风扇转速50%", lambda: PrinterCommands.set_temp(fan=50)),
        ("设置速度倍率120%", lambda: PrinterCommands.set_factor(speed=120)),
        ("设置挤出倍率110%", lambda: PrinterCommands.set_factor(extrude=110)),
        ("全轴归零", lambda: PrinterCommands.home_all()),
        ("电机断电", lambda: PrinterCommands.motors_off()),
        ("获取文件列表", lambda: PrinterCommands.file_list()),
        ("PID自整定（喷嘴200℃）", lambda: PrinterCommands.pid_tune(target="extruder", target_temp=200)),
        ("自动调平", lambda: PrinterCommands.bed_leveling()),
        ("AI摄像头模式设置", lambda: PrinterCommands.ai_set_mode(enable=True, mode="first_layer_detect")),
        ("AI手动抓拍", lambda: PrinterCommands.ai_capture()),
    ]

    pass_count = 0
    fail_count = 0

    for name, func in test_cases:
        logger.info(f"\n--- 测试项：{name} ---")
        try:
            success, resp = func()
            if success and resp["data"]["result"] == 0:
                logger.info(f"✅ 测试通过")
                pass_count += 1
            else:
                logger.error(f"❌ 测试失败，响应：{resp}")
                fail_count += 1
        except Exception as e:
            logger.error(f"❌ 测试异常：{e}")
            fail_count += 1
        time.sleep(0.5)

    logger.info("\n" + "="*60)
    logger.info(f"📊 测试结果：通过 {pass_count} 项，失败 {fail_count} 项")
    logger.info("="*60)

# ========== 控制台交互模式 ==========
def console_mode():
    """手动交互调试模式"""
    print("\n" + "="*60)
    print("📟 手动调试模式，输入指令编号执行")
    print("  1. 查询全量状态    2. 设置温度    3. 全轴归零")
    print("  4. 开始打印        5. 暂停打印    6. 恢复打印")
    print("  7. 停止打印        8. 获取文件列表 9. 运行全量自动测试")
    print("  0. 退出程序")
    print("="*60)

    while True:
        try:
            cmd = input("\n请输入指令编号：").strip()
            if cmd == "0":
                client.disconnect()
                logger.info("👋 程序退出")
                exit(0)
            elif cmd == "1":
                ok, resp = PrinterCommands.query_full_status()
                print(json.dumps(resp, indent=2, ensure_ascii=False) if ok else "超时")
            elif cmd == "2":
                t = float(input("输入喷嘴目标温度："))
                ok, resp = PrinterCommands.set_temp(nozzle=t)
                print("成功" if ok and resp["data"]["result"]==0 else "失败")
            elif cmd == "3":
                ok, resp = PrinterCommands.home_all()
                print("成功" if ok and resp["data"]["result"]==0 else "失败")
            elif cmd == "4":
                fname = input("输入打印文件名：")
                ok, resp = PrinterCommands.print_start(fname)
                print("成功" if ok and resp["data"]["result"]==0 else "失败")
            elif cmd == "5":
                ok, resp = PrinterCommands.print_pause()
                print("成功" if ok and resp["data"]["result"]==0 else "失败")
            elif cmd == "6":
                ok, resp = PrinterCommands.print_resume()
                print("成功" if ok and resp["data"]["result"]==0 else "失败")
            elif cmd == "7":
                ok, resp = PrinterCommands.print_stop()
                print("成功" if ok and resp["data"]["result"]==0 else "失败")
            elif cmd == "8":
                ok, resp = PrinterCommands.file_list()
                print(json.dumps(resp, indent=2, ensure_ascii=False) if ok else "超时")
            elif cmd == "9":
                auto_test_all()
            else:
                print("无效指令")
        except Exception as e:
            print(f"操作异常：{e}")

# ========== 主程序入口 ==========
if __name__ == "__main__":
    client = mqtt.Client(client_id="pc_test_admin_001", callback_api_version=mqtt.CallbackAPIVersion.VERSION1)
    client.username_pw_set(MQTT_USER, MQTT_PWD)

    # 双向TLS配置
    client.tls_set(
        ca_certs=CA_CERT_PATH,
        certfile=CLIENT_CERT_PATH,
        keyfile=CLIENT_KEY_PATH
    )

    client.on_connect = on_connect
    client.on_message = on_message

    # 连接Broker
    logger.info(f"正在连接MQTT Broker：{MQTT_HOST}:{MQTT_PORT}")
    connected = False
    while not connected:
        try:
            client.connect(MQTT_HOST, MQTT_PORT, 60)
            connected = True
        except Exception as e:
            logger.error(f"连接失败，3秒后重试：{e}")
            time.sleep(3)

    client.loop_start()
    time.sleep(1)  # 等待连接与订阅完成

    # 启动控制台交互
    console_mode()