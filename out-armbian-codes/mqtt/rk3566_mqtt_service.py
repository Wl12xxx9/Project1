import paho.mqtt.client as mqtt
import json
import time
import threading
import queue
import logging
from logging.handlers import RotatingFileHandler
import os
import ssl
from datetime import datetime
from collections import deque

# ==================================================
# ========== 🔧 需自行配置的参数区域 START ==========
# ==================================================

# 1. 设备基础信息（对应协议设备ID规则）
DEVICE_ID = "PRT_COREXY_20260702_001"  # 设备唯一标识
DEVICE_MODEL = "CoreXY Pro"
PROTOCOL_VERSION = "1.0.0"
MCU_FIRMWARE_VERSION = "1.0.0"
APP_VERSION = "1.0.0"
ARMBIAN_VERSION = "Armbian_24.05_RK3566"

# 2. MQTT连接配置
MQTT_BROKER = "4ry508ao2807.vicp.fun"
MQTT_PORT = 34796
MQTT_USER = "rk3566"
MQTT_PWD = "123"
KEEP_ALIVE = 60

# 3. 双向TLS证书路径（需与实际部署路径一致）
CA_CERT_PATH = "/root/mqtt-tls-test/ca.crt"
CLIENT_CERT_PATH = "/root/mqtt-tls-test/client_rk3566_001.crt"
CLIENT_KEY_PATH = "/root/mqtt-tls-test/client_rk3566_001.key"
CERT_EXPIRE_WARN_DAYS = 30  # 证书剩余天数小于该值触发预警

# 4. STM32串口配置（需根据实际硬件接线修改）
STM32_SERIAL_PORT = "/dev/ttyS0"
STM32_BAUDRATE = 115200
STM32_CMD_TIMEOUT = 2  # 指令响应超时时间，单位秒

# 5. 上报频率配置
STATUS_REPORT_INTERVAL = 1  # 实时状态上报间隔，单位秒
PROGRESS_REPORT_INTERVAL = 2  # 打印进度更新间隔，单位秒

# 6. 持久化文件路径
CONFIG_FILE = "/opt/printer/config.json"
BREAKPOINT_FILE = "/opt/printer/breakpoint.json"
LOG_FILE = "/var/log/printer/mqtt_service.log"

# 7. 系统参数
MAX_CACHE_EVENT = 100  # 断线时最大缓存事件数
LOG_MAX_SIZE = 10*1024*1024  # 单日志文件10MB
LOG_BACKUP_COUNT = 5  # 日志保留5份

# ==================================================
# ========== 🔧 需自行配置的参数区域 END ==========
# ==================================================


# ========== 全局常量定义 ==========
# 打印状态枚举
PRINT_STATE = {
    "IDLE": "idle",
    "HEATING": "heating",
    "PRINTING": "printing",
    "PAUSED": "paused",
    "COMPLETE": "complete",
    "ERROR": "error"
}

# 指令优先级：数字越小优先级越高
CMD_PRIORITY = {
    "EMERGENCY_STOP": 0,
    "PRINT_CONTROL": 1,
    "MOTION_CONTROL": 2,
    "PARAM_SET": 3,
    "NORMAL_QUERY": 4
}

# 统一错误码（对应协议错误码表）
ERROR_CODE = {
    "SUCCESS": 0,
    "PARAM_MISS": 1001,
    "PARAM_INVALID": 1002,
    "STATE_NOT_ALLOW": 2001,
    "PRINTING_FORBIDDEN": 2002,
    "DEVICE_BUSY": 2005,
    "FILE_NOT_EXIST": 3001,
    "MCU_TIMEOUT": 4002,
    "SYSTEM_ERROR": 5000
}

# 状态转移允许规则（状态前置校验依据）
STATE_TRANSITION_ALLOW = {
    PRINT_STATE["IDLE"]: ["heating", "printing", "home_all", "pid_tune", "bed_leveling"],
    PRINT_STATE["HEATING"]: ["printing", "idle", "error"],
    PRINT_STATE["PRINTING"]: ["paused", "stopped", "complete", "error"],
    PRINT_STATE["PAUSED"]: ["resume", "stopped", "error"],
    PRINT_STATE["COMPLETE"]: ["idle"],
    PRINT_STATE["ERROR"]: ["idle"]
}


# ========== 日志系统初始化 ==========
def init_logger():
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    logger = logging.getLogger("printer_mqtt")
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    
    # 文件日志（轮转）
    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=LOG_MAX_SIZE, backupCount=LOG_BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    
    # 控制台输出（systemd可捕获）
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    return logger

logger = init_logger()


# ========== STM32适配层（需根据你的串口协议补充实现） ==========
class STM32Adapter:
    """
    RK3566与STM32F407的异构通信适配层
    需根据你实际的串口协议格式，补充指令转换和数据解析逻辑
    """
    def __init__(self):
        self.serial = None
        self._init_serial()

    def _init_serial(self):
        """初始化串口，实际使用时替换为真实pyserial代码"""
        try:
            # import serial  # 实际使用时取消注释，安装pyserial
            # self.serial = serial.Serial(STM32_SERIAL_PORT, STM32_BAUDRATE, timeout=0.5)
            logger.info(f"STM32串口初始化完成：{STM32_SERIAL_PORT} @ {STM32_BAUDRATE}")
        except Exception as e:
            logger.error(f"STM32串口初始化失败：{e}")

    def send_command(self, cmd_type, params):
        """
        发送指令到STM32，等待响应
        :param cmd_type: 指令类型（如set_temp、home、move）
        :param params: 指令参数字典
        :return: (success: bool, result: dict, error_code: int)
        """
        # TODO: 🔧 需自行实现：将上层业务指令转换为串口协议帧，发送并等待响应
        # 示例逻辑：
        # 1. 组装串口帧：帧头 + 指令码 + 数据长度 + 参数 + CRC校验
        # 2. 串口发送
        # 3. 等待响应，超时返回MCU_TIMEOUT错误
        time.sleep(0.1)  # 模拟通信延迟
        logger.debug(f"转发指令到STM32：{cmd_type} {params}")
        return True, {}, ERROR_CODE["SUCCESS"]

    def get_realtime_data(self):
        """
        从STM32读取实时底层数据，聚合为上层状态格式
        :return: 状态数据字典
        """
        # TODO: 🔧 需自行实现：读取STM32上报的温度、坐标、电机状态，聚合成协议格式
        # 模拟数据，实际替换为串口读取解析
        return {
            "temperature": {
                "nozzle": {"current": 205.2, "target": 200, "heating": True},
                "bed": {"current": 60.1, "target": 60, "heating": False}
            },
            "motion": {
                "position": {"x": 100.0, "y": 100.0, "z": 50.0, "e": 0.0},
                "current_velocity": 50,
                "motors_enabled": True,
                "homed": {"x": True, "y": True, "z": True},
                "driver_status": {"x": "normal", "y": "normal", "z": "normal", "e": "normal"}
            }
        }


# ========== 状态管理层 ==========
class StatusManager:
    def __init__(self, stm32_adapter):
        self.stm32 = stm32_adapter
        self.status_lock = threading.Lock()
        
        # 全量状态缓存（内存缓存，对应协议status结构）
        self.status_cache = {
            "print_state": PRINT_STATE["IDLE"],
            "motion": {},
            "temperature": {},
            "fan": {"part_cooling_speed": 0, "nozzle_cooling_speed": 0, "nozzle_fan_auto": True},
            "print_progress": {
                "current_file": "",
                "print_duration": 0,
                "remain_time": 0,
                "progress_percent": 0,
                "current_layer": 0,
                "total_layers": 0,
                "filament_used_mm": 0
            },
            "ai_camera": {
                "enabled": False,
                "current_mode": None,
                "last_detect_result": "normal",
                "confidence": 0.0,
                "abnormal_type": None
            },
            "system_status": {
                "cpu_usage": 0,
                "mem_usage": 0,
                "soc_temp": 0,
                "storage_usage": 0
            },
            "error_code": 0
        }
        
        self._load_persistent_data()
        self._start_status_update_thread()

    def _load_persistent_data(self):
        """加载持久化配置和断点数据"""
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r") as f:
                    config = json.load(f)
                    logger.info("持久化配置加载完成")
            except Exception as e:
                logger.warning(f"配置文件加载失败：{e}")

    def _start_status_update_thread(self):
        """启动状态更新线程，定时从STM32拉取数据"""
        def update_loop():
            while True:
                try:
                    bottom_data = self.stm32.get_realtime_data()
                    with self.status_lock:
                        self.status_cache["motion"] = bottom_data["motion"]
                        self.status_cache["temperature"] = bottom_data["temperature"]
                        # TODO: 补充系统状态采集（CPU、内存、温度）
                except Exception as e:
                    logger.error(f"状态更新异常：{e}")
                time.sleep(STATUS_REPORT_INTERVAL)
        
        threading.Thread(target=update_loop, daemon=True).start()
        logger.info("状态更新线程启动")

    def get_full_status(self):
        """获取全量状态快照"""
        with self.status_lock:
            return json.loads(json.dumps(self.status_cache))

    def set_print_state(self, new_state):
        """切换打印状态，触发状态变更事件"""
        with self.status_lock:
            old_state = self.status_cache["print_state"]
            if old_state == new_state:
                return True
            self.status_cache["print_state"] = new_state
        
        logger.info(f"状态变更：{old_state} → {new_state}")
        return True

    def check_state_allowed(self, operation):
        """校验当前状态是否允许执行某操作"""
        with self.status_lock:
            current_state = self.status_cache["print_state"]
        return operation in STATE_TRANSITION_ALLOW.get(current_state, [])

    def save_breakpoint(self):
        """保存打印断点到磁盘（掉电续打用）"""
        try:
            os.makedirs(os.path.dirname(BREAKPOINT_FILE), exist_ok=True)
            with open(BREAKPOINT_FILE, "w") as f:
                json.dump({
                    "current_file": self.status_cache["print_progress"]["current_file"],
                    "progress_percent": self.status_cache["print_progress"]["progress_percent"],
                    "position": self.status_cache["motion"]["position"],
                    "temperature": self.status_cache["temperature"],
                    "timestamp": int(time.time()*1000)
                }, f, indent=2)
        except Exception as e:
            logger.error(f"断点保存失败：{e}")


# ========== MQTT核心层 ==========
class MQTTService:
    def __init__(self, status_manager, stm32_adapter):
        self.status_mgr = status_manager
        self.stm32 = stm32_adapter
        self.client = None
        self.connected = False
        self.reconnect_delay = 1  # 重连初始间隔，指数退避
        self.max_reconnect_delay = 60
        
        # 断线缓存队列（关键事件/响应，上线补发）
        self.offline_cache = deque(maxlen=MAX_CACHE_EVENT)
        self.cache_lock = threading.Lock()
        
        # 指令优先级队列
        self.cmd_queue = queue.PriorityQueue()
        
        self._init_client()
        self._start_cmd_process_thread()

    def _check_cert_validity(self):
        """检测客户端证书有效期，过期预警"""
        try:
            cert_dict = ssl._ssl._test_decode_cert(CLIENT_CERT_PATH)
            not_after_str = cert_dict["notAfter"]
            not_after = datetime.strptime(not_after_str, "%b %d %H:%M:%S %Y %Z")
            remain_days = (not_after - datetime.now()).days
            
            if remain_days <= 0:
                logger.error(f"客户端证书已过期！剩余天数：{remain_days}")
                return False
            elif remain_days <= CERT_EXPIRE_WARN_DAYS:
                logger.warning(f"证书即将过期，剩余{remain_days}天")
                self.publish_event("warn", "cert_expire_warn", 5001, {"remain_days": remain_days})
            else:
                logger.info(f"证书有效期正常，剩余{remain_days}天")
            return True
        except Exception as e:
            logger.error(f"证书有效期检测失败：{e}")
            return False

    def _init_client(self):
        """初始化MQTT客户端，配置TLS、遗嘱、回调"""
        self.client = mqtt.Client(client_id=DEVICE_ID, callback_api_version=mqtt.CallbackAPIVersion.VERSION1)
        self.client.username_pw_set(MQTT_USER, MQTT_PWD)
        
        # 双向TLS配置
        self.client.tls_set(
            ca_certs=CA_CERT_PATH,
            certfile=CLIENT_CERT_PATH,
            keyfile=CLIENT_KEY_PATH,
            tls_version=ssl.PROTOCOL_TLSv1_2
        )
        self.client.tls_insecure_set(False)  # 强制校验服务端域名
        
        # 遗嘱消息（设备异常离线自动上报）
        will_payload = self._build_event_msg(
            "error", "device_offline", 4007, {"reason": "connection_lost"}
        )
        self.client.will_set(
            topic=f"device/{DEVICE_ID}/event",
            payload=json.dumps(will_payload),
            qos=1,
            retain=False
        )
        
        # 绑定回调
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.on_publish = self._on_publish
        
        # 证书有效期检测
        self._check_cert_validity()

    def _build_common_header(self, msg_type):
        """构造通用上行消息头"""
        return {
            "common": {
                "device_id": DEVICE_ID,
                "timestamp": int(time.time()*1000),
                "msg_type": msg_type,
                "version": PROTOCOL_VERSION
            },
            "data": {},
            "ext": {}
        }

    def _build_event_msg(self, level, event_type, event_code, event_data):
        """构造事件消息"""
        msg = self._build_common_header("event")
        msg["data"] = {
            "event_id": f"evt_{int(time.time()*1000)}",
            "event_level": level,
            "event_type": event_type,
            "event_code": event_code,
            "event_data": event_data
        }
        return msg

    def _build_response_msg(self, request_id, cmd_type, result, error_code, msg, response_data=None):
        """构造指令响应消息"""
        resp = self._build_common_header("response")
        resp["data"] = {
            "request_id": request_id,
            "cmd_type": cmd_type,
            "result": result,
            "error_code": error_code,
            "msg": msg,
            "response_data": response_data if response_data else {}
        }
        return resp

    def _on_connect(self, client, userdata, flags, rc):
        """连接成功回调"""
        if rc == 0:
            self.connected = True
            self.reconnect_delay = 1  # 重置重连间隔
            logger.info("✅ MQTT双向TLS连接成功")
            
            # 订阅所有下行指令Topic
            cmd_topics = [
                (f"device/{DEVICE_ID}/cmd/print", 1),
                (f"device/{DEVICE_ID}/cmd/param", 1),
                (f"device/{DEVICE_ID}/cmd/motion", 1),
                (f"device/{DEVICE_ID}/cmd/file", 1),
                (f"device/{DEVICE_ID}/cmd/system", 1),
                (f"device/{DEVICE_ID}/cmd/ai", 1)
            ]
            client.subscribe(cmd_topics)
            logger.info("所有下行指令Topic订阅完成")
            
            # 1. 发布设备基础信息（保留消息）
            self._publish_device_info()
            # 2. 补发断线缓存的消息
            self._flush_offline_cache()
            # 3. 启动定时上报线程
            self._start_report_thread()
            
        else:
            logger.error(f"❌ MQTT连接失败，错误码：{rc}")

    def _on_disconnect(self, client, userdata, rc):
        """连接断开回调，指数退避重连"""
        self.connected = False
        logger.warning(f"MQTT连接断开，错误码：{rc}，{self.reconnect_delay}s后重连")
        
        while not self.connected:
            try:
                time.sleep(self.reconnect_delay)
                self.client.reconnect()
                # 指数退避：1s → 2s → 4s → ... → 最大60s
                self.reconnect_delay = min(self.reconnect_delay * 2, self.max_reconnect_delay)
            except Exception as e:
                logger.error(f"重连失败：{e}，{self.reconnect_delay}s后重试")

    def _on_message(self, client, userdata, msg):
        """下行消息接收回调"""
        try:
            payload = json.loads(msg.payload.decode())
            logger.debug(f"收到指令：{msg.topic} {payload}")
            
            # 基础字段校验
            required_fields = ["request_id", "cmd_type", "data"]
            for field in required_fields:
                if field not in payload:
                    self._send_error_response(
                        payload.get("request_id", ""), 
                        payload.get("cmd_type", ""),
                        ERROR_CODE["PARAM_MISS"],
                        f"缺少必填字段：{field}"
                    )
                    return
            
            # 根据Topic判断指令优先级，入队
            cmd_type = payload["cmd_type"]
            priority = self._get_cmd_priority(cmd_type)
            self.cmd_queue.put((priority, msg.topic, payload))
            
        except json.JSONDecodeError:
            logger.error("指令JSON格式错误")
        except Exception as e:
            logger.error(f"消息处理异常：{e}")

    def _get_cmd_priority(self, cmd_type):
        """获取指令优先级"""
        if cmd_type == "emergency_stop":
            return CMD_PRIORITY["EMERGENCY_STOP"]
        elif cmd_type == "print_control":
            return CMD_PRIORITY["PRINT_CONTROL"]
        elif cmd_type == "motion_control":
            return CMD_PRIORITY["MOTION_CONTROL"]
        elif cmd_type == "param_set":
            return CMD_PRIORITY["PARAM_SET"]
        else:
            return CMD_PRIORITY["NORMAL_QUERY"]

    def _start_cmd_process_thread(self):
        """指令处理线程：从优先级队列取指令执行"""
        def process_loop():
            while True:
                try:
                    priority, topic, payload = self.cmd_queue.get()
                    self._execute_command(topic, payload)
                except Exception as e:
                    logger.error(f"指令执行异常：{e}")
        
        threading.Thread(target=process_loop, daemon=True).start()
        logger.info("指令处理线程启动")

    def _execute_command(self, topic, payload):
        """执行具体指令，分发给STM32或本地处理"""
        request_id = payload["request_id"]
        cmd_type = payload["cmd_type"]
        data = payload["data"]
        
        # 1. 查询类指令：直接返回缓存，不访问STM32
        if cmd_type == "common_query":
            query_type = data.get("query_type", "")
            if query_type == "full_status":
                resp_data = self.status_mgr.get_full_status()
                self._send_success_response(request_id, cmd_type, resp_data)
            return
        
        # 2. 状态前置校验（非查询类指令都校验）
        if not self._check_cmd_state_allowed(cmd_type, data):
            self._send_error_response(request_id, cmd_type, ERROR_CODE["STATE_NOT_ALLOW"], "当前状态不允许该操作")
            return
        
        # 3. 分发到STM32执行
        success, result, err_code = self.stm32.send_command(cmd_type, data)
        if not success:
            self._send_error_response(request_id, cmd_type, err_code, "STM32执行失败")
            return
        
        # 4. 执行成功，更新状态，返回响应
        if cmd_type == "print_control":
            action = data["action"]
            if action == "start":
                self.status_mgr.set_print_state(PRINT_STATE["HEATING"])
                self.publish_event("info", "print_start", 1001, {
                    "file_name": data.get("file_name", ""),
                    "estimate_time": 3600
                })
        
        self._send_success_response(request_id, cmd_type, result)

    def _check_cmd_state_allowed(self, cmd_type, data):
        """指令状态前置校验"""
        if cmd_type == "print_control":
            action = data.get("action", "")
            return self.status_mgr.check_state_allowed(action)
        elif cmd_type == "motion_control":
            action = data.get("action", "")
            return self.status_mgr.check_state_allowed(action)
        elif cmd_type == "system_config":
            action = data.get("action", "")
            return self.status_mgr.check_state_allowed(action)
        return True

    def _send_success_response(self, request_id, cmd_type, response_data=None):
        """发送成功响应"""
        resp = self._build_response_msg(request_id, cmd_type, 0, 0, "success", response_data)
        self.publish(f"device/{DEVICE_ID}/response", resp, qos=1)

    def _send_error_response(self, request_id, cmd_type, error_code, msg):
        """发送错误响应"""
        resp = self._build_response_msg(request_id, cmd_type, 1, error_code, msg, {})
        self.publish(f"device/{DEVICE_ID}/response", resp, qos=1)

    def _start_report_thread(self):
        """启动定时状态上报线程"""
        def report_loop():
            while self.connected:
                try:
                    status = self.status_mgr.get_full_status()
                    msg = self._build_common_header("status")
                    msg["data"] = status
                    self.publish(f"device/{DEVICE_ID}/status", msg, qos=0)
                except Exception as e:
                    logger.error(f"状态上报异常：{e}")
                time.sleep(STATUS_REPORT_INTERVAL)
        
        threading.Thread(target=report_loop, daemon=True).start()
        logger.info("状态定时上报线程启动")

    def publish_event(self, level, event_type, event_code, event_data):
        """发布事件（触发式，QoS=1）"""
        event_msg = self._build_event_msg(level, event_type, event_code, event_data)
        self.publish(f"device/{DEVICE_ID}/event", event_msg, qos=1)

    def _publish_device_info(self):
        """发布设备基础信息（保留消息，QoS=0）"""
        info_msg = self._build_common_header("info")
        info_msg["data"] = {
            "device_base": {
                "model": DEVICE_MODEL,
                "serial_no": DEVICE_ID,
                "production_date": "2026-07-02",
                "production_index": 1,
                "mcu_uuid": "2dc7a3ac3edd",
                "mcu_restart_method": "command"
            },
            "system_version": {
                "armbian_version": ARMBIAN_VERSION,
                "mcu_firmware_version": MCU_FIRMWARE_VERSION,
                "app_firmware_version": APP_VERSION
            }
            # TODO: 补充完整的motion_config、extruder_config等硬件配置
        }
        self.publish(f"device/{DEVICE_ID}/info", info_msg, qos=0, retain=True)
        logger.info("设备基础信息保留消息已发布")

    def publish(self, topic, payload, qos=0, retain=False):
        """统一发布接口，离线时自动缓存"""
        payload_str = json.dumps(payload, ensure_ascii=False)
        if self.connected:
            self.client.publish(topic, payload_str, qos=qos, retain=retain)
        else:
            # 关键消息离线缓存，上线补发
            if qos >= 1:
                with self.cache_lock:
                    self.offline_cache.append((topic, payload_str, qos, retain))
                logger.debug(f"连接断开，消息已缓存：{topic}")

    def _flush_offline_cache(self):
        """重连成功后补发缓存的消息"""
        with self.cache_lock:
            while self.offline_cache:
                topic, payload, qos, retain = self.offline_cache.popleft()
                self.client.publish(topic, payload, qos=qos, retain=retain)
        logger.info("断线缓存消息已全部补发")

    def _on_publish(self, client, userdata, mid):
        """消息发布完成回调"""
        pass

    def update_cert(self, new_cert_content, new_key_content):
        """预留：证书更新接口，远程替换证书"""
        try:
            # 备份原证书
            os.rename(CLIENT_CERT_PATH, f"{CLIENT_CERT_PATH}.bak")
            os.rename(CLIENT_KEY_PATH, f"{CLIENT_KEY_PATH}.bak")
            
            with open(CLIENT_CERT_PATH, "w") as f:
                f.write(new_cert_content)
            with open(CLIENT_KEY_PATH, "w") as f:
                f.write(new_key_content)
            
            logger.info("证书更新成功，重启服务后生效")
            return True
        except Exception as e:
            logger.error(f"证书更新失败：{e}")
            # 恢复备份
            if os.path.exists(f"{CLIENT_CERT_PATH}.bak"):
                os.rename(f"{CLIENT_CERT_PATH}.bak", CLIENT_CERT_PATH)
                os.rename(f"{CLIENT_KEY_PATH}.bak", CLIENT_KEY_PATH)
            return False

    def start(self):
        """启动服务"""
        logger.info("MQTT服务启动中...")
        while not self.connected:
            try:
                self.client.connect(MQTT_BROKER, MQTT_PORT, KEEP_ALIVE)
                break
            except Exception as e:
                logger.error(f"初始连接失败：{e}，{self.reconnect_delay}s后重试")
                time.sleep(self.reconnect_delay)
                self.reconnect_delay = min(self.reconnect_delay * 2, self.max_reconnect_delay)
        
        self.client.loop_start()


# ========== 主程序入口 ==========
if __name__ == "__main__":
    logger.info("="*50)
    logger.info("3D打印机RK3566 MQTT服务启动")
    logger.info(f"设备ID：{DEVICE_ID}")
    logger.info(f"协议版本：{PROTOCOL_VERSION}")
    logger.info("="*50)
    
    # 初始化各模块
    stm32_adapter = STM32Adapter()
    status_manager = StatusManager(stm32_adapter)
    mqtt_service = MQTTService(status_manager, stm32_adapter)
    
    # 启动服务
    mqtt_service.start()
    
    # 主线程保活
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("服务被手动终止")
        mqtt_service.client.disconnect()
        exit(0)