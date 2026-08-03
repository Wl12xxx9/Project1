import paho.mqtt.client as mqtt
import json
import socket
import time
import threading
import queue
import logging
from logging.handlers import RotatingFileHandler
import os
import ssl
from datetime import datetime
from collections import deque
from pydantic import BaseSettings, Field
from functools import lru_cache
import yaml
import subprocess
import base64
import websockets
import asyncio
import concurrent.futures

RPC_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=1)


# 测试模拟开关：True=无硬件模拟返回，不调用Moonraker
# MOCK_MOONRAKER = True
MOCK_MOONRAKER = False

# ==================================================
# ========== 🔧 需自行配置的参数区域 START ==========
# ==================================================

# 1. 设备基础信息（对应协议设备ID规则）
DEVICE_ID = "PRT_COREXY_20260702_001"  # 设备唯一标识
DEVICE_MODEL = "CoreXY Pro"
PROTOCOL_VERSION = "1.0.0"
MCU_FIRMWARE_VERSION = "1.0.0"
APP_VERSION = "1.0.0"
ARMBIAN_VERSION = "Armbian_26.05_RK3566"

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

# 4. Klipper配置
# Klipper Moonraker Unix Socket 路径（Armbian标准路径）
MOONRAKER_SOCKET = "/home/klipper/printer_data/comms/moonraker.sock"
# ========== Websocket连接地址 ==========
MOONRAKER_WS_URI = "ws://127.0.0.1:7125/websocket"
# Klipper CAN总线STM32下位机唯一CAN UUID
CAN_MCU_UUID = "2dc7a3ac3edd"
# 芯片型号仅做日志打印用，不参与通信判断
MCU_CHIP_TYPE = "stm32f407"
# Klipper指令超时,
KLIPPER_TIMEOUT = 12

# 5. 上报频率配置
STATUS_REPORT_INTERVAL = 3  # 实时状态上报间隔，单位秒
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
    "SYSTEM_ERROR": 5000,
    "AI_CAMERA_CLOSED": 6000,
    "AI_STREAM_TIMEOUT": 6001,
    "AI_HW_ERROR": 6002,
    "LIGHT_PARAM_INVALID": 6100
}

# 状态转移允许规则（状态前置校验依据）
STATE_TRANSITION_ALLOW = {
    # PRINT_STATE["IDLE"]: ["heating", "printing", "home_all", "pid_tune", "bed_leveling"],
    # PRINT_STATE["HEATING"]: ["printing", "idle", "error"],
    # PRINT_STATE["PRINTING"]: ["paused", "stopped", "complete", "error"],
    # PRINT_STATE["PAUSED"]: ["resume", "stopped", "error"],
    # PRINT_STATE["COMPLETE"]: ["idle"],
    # PRINT_STATE["ERROR"]: ["idle"]
    PRINT_STATE["IDLE"]: ["heating", "printing", "home_all", "pid_tune", "bed_leveling", "jog", "motors_off"],
    PRINT_STATE["HEATING"]: ["printing", "idle", "error"],
    PRINT_STATE["PRINTING"]: ["paused", "stopped", "complete", "error"],
    PRINT_STATE["PAUSED"]: ["resume", "stopped", "error"],
    PRINT_STATE["COMPLETE"]: ["idle"],
    PRINT_STATE["ERROR"]: ["idle"]
}

class MQTTSettings(BaseSettings):
    broker: str
    port: int
    user: str
    pwd: str
    keep_alive: int = 60

class TLSSettings(BaseSettings):
    ca_cert: str
    client_cert: str
    client_key: str
    expire_warn_days: int = 30

class KlipperSettings(BaseSettings):
    moonraker_socket: str
    timeout: int = 3

class LogSettings(BaseSettings):
    log_file: str = "/var/log/printer/mqtt_service.log"
    log_max_size: int = 10485760
    log_backup: int = 5

class PersistSettings(BaseSettings):
    config_file: str = "/opt/printer/config.json"
    breakpoint_file: str = "/opt/printer/breakpoint.json"

class AppSettings(BaseSettings):
    mqtt: MQTTSettings
    tls: TLSSettings
    klipper: KlipperSettings
    log: LogSettings
    persist: PersistSettings
    device_id: str = "PRT_COREXY_20260702_001"
    protocol_version: str = "1.0.0"

@lru_cache()
def get_settings():
    with open("config.yaml", "r") as f:
        yaml_config = yaml.safe_load(f)
    # 环境变量替换
    for key, value in yaml_config.items():
        if isinstance(value, dict):
            for k, v in value.items():
                if isinstance(v, str) and v.startswith("${"):
                    yaml_config[key][k] = os.getenv(v.strip("${}"), v)
    return AppSettings(**yaml_config)

settings = get_settings()

# 使用
DEVICE_ID = settings.device_id
PROTOCOL_VERSION = settings.protocol_version
CA_CERT_PATH = settings.tls.ca_cert
CLIENT_CERT_PATH = settings.tls.client_cert
CLIENT_KEY_PATH = settings.tls.client_key
CERT_EXPIRE_WARN_DAYS = settings.tls.expire_warn_days
MOONRAKER_SOCKET = settings.klipper.moonraker_socket
KLIPPER_TIMEOUT = settings.klipper.timeout

# AI摄像头配置
RTSP_STREAM_URL = "rtsp://127.0.0.1:8554/stream"
SNAPSHOT_SAVE_DIR = "/opt/printer/media/snapshots"
HTTP_MEDIA_BASE = "http://{device_ip}:8080/media/snapshots"

# ========== 日志系统初始化 ==========
def init_logger():
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    logger = logging.getLogger("printer_mqtt")
    # logger.setLevel(logging.INFO)
    logger.setLevel(logging.DEBUG)
    # 优化日志格式：添加进程ID、线程ID
    formatter = logging.Formatter(
        "%(asctime)s - %(process)d - %(threadName)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    # 按时间（每天）+ 大小（10MB）轮转
    # from logging.handlers import TimedRotatingFileHandler
    # file_handler = TimedRotatingFileHandler(
    #     LOG_FILE,
    #     when="D",  # 每天轮转
    #     interval=1,
    #     backupCount=7,  # 保留7天
    #     encoding="utf-8"
    # )
    # 叠加大小限制
    file_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=10*1024*1024,
        backupCount=5,
        encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    # 控制台handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    return logger

logger = init_logger()

# ========== STM32适配层（双Socket根治版：订阅长连接 + RPC独立短连接） ==========
class STM32Adapter:
    """
    Websocket双链路架构：
    1. sub_ws：长连接订阅 notify_status 实时推送
    2. rpc_ws：每次RPC新建临时连接，查询/下发指令
    """
    def __init__(self, status_mgr):
        self.status_mgr = status_mgr
        self.ws_uri = MOONRAKER_WS_URI
        self.sub_ws = None
        self.sub_task = None
        self.status_callback = None
        self._start_subscribe_loop()
        logger.info(f"Moonraker Websocket订阅链路启动 WS:{self.ws_uri}")

    async def _sub_listener(self):
        """订阅长连接异步循环"""
        while True:
            try:
                async with websockets.connect(
                    self.ws_uri,
                    ping_interval=10,
                    ping_timeout=8
                ) as ws:
                    self.sub_ws = ws
                    logger.info("Websocket订阅连接成功")
                    # 修正后的订阅请求
                    sub_req = {
                        "jsonrpc": "2.0",
                        "method": "printer.objects.subscribe",
                        "params": {
                            "objects": {
                                "extruder": None,
                                "heater_bed": None,
                                "fan": None,
                                "toolhead": None,
                                "print_stats": None
                            }
                        },
                        "id": int(time.time() * 1000)
                    }
                    await ws.send(json.dumps(sub_req))
                    # 新增：等待订阅ACK，避免连接直接断掉
                    ack_raw = await ws.recv()
                    ack = json.loads(ack_raw)
                    logger.info(f"订阅服务端ACK返回: {ack}")
                    # 持续接收notify推送
                    async for raw_msg in ws:
                        try:
                            obj = json.loads(raw_msg)
                            method_name = obj.get("method")
                            logger.debug(f"收到Moonraker推送: {method_name}")
                            if method_name == "notify_status_update" and self.status_callback:
                                # params 是列表，取下标0才是状态dict
                                data_dict = obj["params"][0]
                                self.status_callback(data_dict)
                        except json.JSONDecodeError:
                            continue
            except Exception as e:
                logger.warning(f"订阅Websocket断开，3s重连: {e}")
                self.sub_ws = None
                await asyncio.sleep(3)

    def _start_subscribe_loop(self):
        def run_async_sub():
            while True:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(self._sub_listener())
                except Exception as e:
                    logger.error(f"订阅协程崩溃，3s重启:{e}")
                finally:
                    loop.close()
                time.sleep(3)
        self.sub_task = threading.Thread(target=run_async_sub, daemon=True, name="mr_sub_ws")
        self.sub_task.start()

    async def _rpc_single_conn(self):
        """单次RPC独立短连接，对应ws_test2查询方式"""
        return await websockets.connect(self.ws_uri)

    async def _send_moonraker_rpc_async(self, rpc_method, params=None):
        """异步RPC底层，完全对齐ws_test2请求格式"""
        if MOCK_MOONRAKER:
            logger.info(f"【模拟Moonraker】method={rpc_method}")
            if rpc_method == "server.files.list":
                mock_result = {
                    "total": 1,
                    "page": 1,
                    "page_size": 20,
                    "files": [{"filename":"test_print.gcode","size":1024,"modified":1783320000}]
                }
                return True, mock_result, ERROR_CODE["SUCCESS"]
            return True, {}, ERROR_CODE["SUCCESS"]
        try:
            ws = await self._rpc_single_conn()
            req = {
                "jsonrpc": "2.0",
                "method": rpc_method,
                "params": params if params else {},
                "id": int(time.time()*1000)
            }
            await ws.send(json.dumps(req))
            resp_raw = await ws.recv()
            await ws.close()
            resp = json.loads(resp_raw)
            if "error" in resp:
                logger.error(f"RPC返回错误 {resp['error']}")
                return False, {}, ERROR_CODE["MCU_TIMEOUT"]
            return True, resp.get("result", {}), ERROR_CODE["SUCCESS"]
        except Exception as e:
            logger.error(f"Websocket RPC失败 {e}")
            return False, {}, ERROR_CODE["MCU_TIMEOUT"]

    # 替换STM32Adapter内的 _send_moonraker_rpc
    def _send_moonraker_rpc(self, rpc_method, params=None):
        logger.debug(f"RPC请求：{rpc_method}")
        if MOCK_MOONRAKER:
            if rpc_method == "server.files.list":
                mock_result = {
                    "total": 1, "page": 1, "page_size": 20,
                    "file_list": [{"file_name":"test_print.gcode","size":1024,"upload_time":1783320000}]
                }
                return True, mock_result, ERROR_CODE["SUCCESS"]
            return True, {}, ERROR_CODE["SUCCESS"]
        try:
            # 在线程池新开线程跑协程，不抢占主线程循环
            future = RPC_POOL.submit(asyncio.run, self._send_moonraker_async(rpc_method, params))
            return future.result(timeout=12)
        except Exception as e:
            logger.error(f"RPC异常：{e}")
            return False, {}, ERROR_CODE["MCU_TIMEOUT"]

    def send_command(self, cmd_type, params):
        """send_command 上层逻辑完全不用改动，底层RPC已切换WS"""
        # 原有全部分支代码保留，无需修改
        # 1. 温控设置 param_set
        if cmd_type == "param_set":
            logger.info(f"【业务分支-param_set】原始入参 params={params}")
            cmds = []
            if "nozzle_target" in params:
                cmds.append(f"M104 S{params['nozzle_target']}")
            if "bed_target" in params:
                cmds.append(f"M140 S{params['bed_target']}")
            if "fan_speed" in params:
                cmds.append(f"M106 P0 S{int(params['fan_speed']*2.55)}")
            gcode = "\n".join(cmds)
            logger.info(f"【param_set 生成Gcode】{gcode}")
            return self._send_moonraker_rpc("printer.gcode.script", {"script": gcode})
        # 2. 运动控制 motion_control
        elif cmd_type == "motion_control":
            logger.info(f"【业务分支-motion_control】原始入参 params={params}")
            action = params.get("action")
            if action == "home_all":
                return self._send_moonraker_rpc("printer.gcode.script", {"script": "G28"})
            elif action == "jog":
                axis = params["axis"]
                dist = params["distance"]
                speed = params["speed"]
                rel = "G91" if params.get("relative", True) else "G90"
                gcode = f"{rel}\nG1 {axis}{dist} F{speed*60}"
                return self._send_moonraker_rpc("printer.gcode.script", {"script": gcode})
            elif action == "motors_off":
                return self._send_moonraker_rpc("printer.gcode.script", {"script": "M84"})
        # 3. 打印控制 print_control
        elif cmd_type == "print_control":
            logger.info(f"【业务分支-print_control】原始入参 params={params}")
            action = params.get("action")
            if action == "start":
                fname = params["file_name"]
                return self._send_moonraker_rpc("printer.print.start", {"filename": fname})
            elif action == "pause":
                return self._send_moonraker_rpc("printer.print.pause", {})
            elif action == "resume":
                return self._send_moonraker_rpc("printer.print.resume", {})
            elif action in ["stop", "cancel"]:
                return self._send_moonraker_rpc("printer.print.cancel", {})
        # 4. 系统校准 system_config
        elif cmd_type == "system_config":
            logger.info(f"【业务分支-system_config】原始入参 params={params}")
            action = params.get("action")
            if action == "pid_tune":
                target = params["target"]
                temp = params["target_temp"]
                heater = "extruder" if target == "extruder" else "heater_bed"
                gcode = f"M303 E{heater} S{temp}"
                return self._send_moonraker_rpc("printer.gcode.script", {"script": gcode})
            elif action == "bed_leveling":
                return self._send_moonraker_rpc("printer.gcode.script", {"script": "BED_MESH_CALIBRATE"})
        elif cmd_type == "file_manage":
            logger.info(f"【业务分支-file_manage】原始入参 params={params}")
            action = params.get("action")
            if action == "list":
                page = params.get("page", 1)
                page_size = params.get("page_size", 20)
                success, result, code = self._send_moonraker_rpc("server.files.list", {"root": "gcodes"})
                if not success:
                    return False, {}, code
                files = result.get("files", [])
                total = len(files)
                start = (page - 1) * page_size
                end = start + page_size
                page_files = files[start:end]
                file_list = [{
                    "file_name": f.get("filename", ""),
                    "size": f.get("size", 0),
                    "upload_time": f.get("modified", 0),
                    "layer_count": 0,
                    "print_time": f.get("print_time", 0)
                } for f in page_files]
                return True, {
                    "total": total,
                    "page": page,
                    "page_size": page_size,
                    "file_list": file_list
                }, ERROR_CODE["SUCCESS"]
            elif action == "delete":
                file_name = params.get("file_name", "")
                if not file_name:
                    return False, {}, ERROR_CODE["PARAM_MISS"]
                success, result, code = self._send_moonraker_rpc("server.files.delete", {
                    "root": "gcodes",
                    "filename": file_name
                })
                return success, result, {}
        elif cmd_type == "ai_camera_control":
            logger.info(f"【业务分支-ai_camera_control】原始入参 params={params}")
            action = params.get("action")
            if action == "switch":
                hw_enable = params.get("enable", False)
                with self.status_mgr.status_lock:
                    self.status_mgr.status_cache["ai_camera"]["hardware_switch"] = hw_enable
                if not hw_enable:
                    self.status_mgr.status_cache["ai_thread_exit"] = True
                return True, {"hardware_switch": hw_enable}, ERROR_CODE["SUCCESS"]
            elif action in ["capture", "set_mode"]:
                hw_state = self.status_mgr.status_cache.get("ai_camera", False)
                if not hw_state:
                    return False, {}, ERROR_CODE["AI_CAMERA_CLOSED"]
                if action == "capture":
                    success, result = self._capture_snapshot()
                    return success, result, ERROR_CODE["SUCCESS"] if success else ERROR_CODE["SYSTEM_ERROR"]
                elif action == "set_mode":
                    enable = params.get("enable", True)
                    mode = params.get("mode", "")
                    self.status_mgr.set_ai_mode(enable, mode)
                    return True, {"enable": enable, "mode": mode}, ERROR_CODE["SUCCESS"]
        elif cmd_type == "light_control":
            switch = params.get("switch", False)
            bri = params.get("brightness", 100)
            r = params.get("r")
            g = params.get("g")
            b = params.get("b")
            mode = params.get("mode", "normal")
            if not (0 <= bri <= 100 and 0<=r<=255 and 0<=g<=255 and 0<=b<=255):
                logger.error("灯光参数非法")
                return False, {}, ERROR_CODE["LIGHT_PARAM_INVALID"]
            valid_modes = ["normal", "breath", "flash"]
            if mode not in valid_modes:
                return False, {}, ERROR_CODE["LIGHT_PARAM_INVALID"]
            if MOCK_MOONRAKER:
                self.status_mgr.status_cache["light"] = {
                    "switch": switch,
                    "brightness": bri,
                    "r": r, "g": g, "b": b,
                    "mode": mode
                }
                return True, {}, ERROR_CODE["SUCCESS"]
            self.status_mgr.status_cache["light"]["switch"] = switch
            self.status_mgr.status_cache["light"]["brightness"] = bri
            self.status_mgr.status_cache["r"] = r
            self.status_mgr.status_cache["g"] = g
            self.status_mgr.status_cache["b"] = b
            self.status_mgr.status_cache["mode"] = mode
            return True, {}, ERROR_CODE["SUCCESS"]
        return True, {}, ERROR_CODE["SUCCESS"]

    def get_realtime_data(self):
        """
        完全对齐ws_test2查询字段，替换原残缺解析代码
        """
        success, klipper_state, code = self._send_moonraker_rpc("printer.objects.query", {
            "objects": {
                "extruder": ["temperature", "target", "power"],
                "heater_bed": ["temperature", "target", "power"],
                "fan": ["speed"],
                "toolhead": ["position", "homed_axes", "velocity"],
                "print_stats": ["state", "filename", "print_duration", "progress", "filament_used"]
            }
        })
        if not success:
            return {
                "temperature": {"nozzle":{"current":25,"target":0,"heating":False},"bed":{"current":25,"target":0,"heating":False}},
                "motion": {"position":{"x":0,"y":0,"z":0,"e":0},"current_velocity":0,"motors_enabled":False,"homed":{"x":False,"y":False,"z":False},"driver_status":{"x":"normal","y":"normal","z":"normal","e":"normal"}},
                "print_progress": {
                    "current_file": "",
                    "print_duration": 0,
                    "remain_time": 0,
                    "progress_percent": 0,
                    "current_layer": 0,
                    "total_layers": 0,
                    "filament_used_mm": 0
                }
            }
        # 修复变量名错误 klipper -> klipper_state
        toolhead = klipper_state.get("toolhead", {})
        extruder = klipper_state.get("extruder", {})
        heater_bed = klipper_state.get("heater_bed", {})
        fan = klipper_state.get("fan", {})
        print_stats = klipper_state.get("print_stats", {})
        pos = toolhead.get("position", [0,0,0,0])
        homed_str = toolhead.get("homed_axes", "")
        homed = {
            "x": "x" in homed_str,
            "y": "y" in homed_str,
            "z": "z" in homed
        }
        nozzle_heat = (extruder.get("power", 0.0) or 0.0) > 0
        bed_heat = (heater_bed.get("power", 0.0) or 0.0) > 0
        # 打印进度解析，同步ws_test2逻辑
        p_duration = print_stats.get("print_duration", 0.0) or 0.0
        p_progress = (print_stats.get("progress", 0.0) or 0.0) * 100
        p_file = print_stats.get("filename", "")
        filament = print_stats.get("filament_used", 0.0) or 0.0
        est_time = print_stats.get("estimated_time", 0.0) or 0.0
        remain = est_time - p_duration
        return {
            "temperature": {
                "nozzle": {
                    "current": round((extruder.get("temperature",25.0) or 0.0),1),
                    "target": extruder.get("target",0.0) or 0.0,
                    "heating": nozzle_heat
                },
                "bed": {
                    "current": round((heater_bed.get("temperature",25.0) or 0.0),1),
                    "target": heater_bed.get("target",0.0) or 0.0,
                    "heating": bed_heat
                }
            },
            "motion": {
                "position": {"x":pos[0],"y":pos[1],"z":pos[2],"e":pos[3]},
                "current_velocity": round((toolhead.get("velocity",0.0) or 0.0),1),
                "motors_enabled": len(homed_str) > 0,
                "homed": homed,
                "driver_status":{"x":"normal","y":"normal","z":"normal","e":"normal"}
            },
            "print_progress": {
                "current_file": p_file,
                "print_duration": round(p_duration,1),
                "remain_time": round(remain,1),
                "progress_percent": round(p_progress,2),
                "current_layer": 0,
                "total_layers": 0,
                "filament_used_mm": round(filament,2)
            }
        }

    def _capture_snapshot(self):
        """从RTSP流截取一帧图片，返回文件名、访问地址、base64缩略图"""
        import shutil
        if shutil.which("ffmpeg") is None:
            logger.error("未安装ffmpeg，抓拍功能失效")
            with self.status_mgr.status_lock:
                self.status_mgr.status_cache["ai_camera"]["abnormal_type"] = "ffmpeg_missing"
            return False, {"err_msg":"ffmpeg not found"}
        # 检测rtsp端口是否监听
        import socket
        sock_test = socket.socket()
        sock_test.settimeout(1)
        try:
            sock_test.connect(("127.0.0.1", 8554))
            sock_test.close()
        except:
            logger.error("MediaMTX RTSP服务未启动，无法抓拍")
            return False, {"err_msg":"rtsp服务未运行"}
        os.makedirs(SNAPSHOT_SAVE_DIR, exist_ok=True)
        # 自动清理72小时前截图，防止磁盘占满
        def clean_expire_snap():
            import glob
            files = glob.glob(os.path.join(SNAPSHOT_SAVE_DIR, "snap_*.jpg"))
            now = time.time()
            for f in files:
                if now - os.path.getctime(f) > 72 * 3600:
                    os.remove(f)
        try:
            clean_expire_snap()
        except Exception as e:
            logger.warning(f"清理过期截图失败：{e}")

        timestamp = int(time.time()*1000)
        file_name = f"snap_{timestamp}.jpg"
        save_path = os.path.join(SNAPSHOT_SAVE_DIR, file_name)
        # ffmpeg抓拍命令，TCP传输降低丢帧
        cmd = [
            "ffmpeg", "-y",
            "-rtsp_transport", "tcp", "-flags", "low_delay",
            "-i", RTSP_STREAM_URL,
            "-vframes", "1", "-q:v", "3", "-t", "3",
            save_path
        ]
        try:
            # 执行抓拍，超时3秒
            subprocess.run(cmd, timeout=3, capture_output=True, check=True)
            if not os.path.exists(save_path):
                logger.error("抓拍生成文件不存在")
                return False, {}
            # 读取图片base64缩略图
            with open(save_path, "rb") as f:
                raw = f.read()
                thumbnail = base64.b64encode(raw).decode("utf-8")
            # 获取本机局域网IP，替换127.0.0.1
            def get_local_ip():
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    s.connect(("8.8.8.8", 80))
                    return s.getsockname()[0]
                except Exception:
                    return "127.0.0.1"
                finally:
                    s.close()
            dev_ip = get_local_ip()
            img_url = HTTP_MEDIA_BASE.format(device_ip=dev_ip) + "/" + file_name
            return True, {
                "img_file": file_name,
                "img_url": img_url,
                "thumbnail": thumbnail,
                "capture_time": timestamp
            }
        except subprocess.TimeoutExpired:
            logger.error("ffmpeg抓拍超时，RTSP流异常")
            # 新增异常标记
            with self.status_mgr.status_lock:
                self.status_mgr.status_cache["ai_camera"]["abnormal_type"] = "camera_disconnect"
            return False, {"err_msg": "rtsp stream timeout"}
        except Exception as e:
            logger.error(f"AI抓拍异常：{str(e)}")
            # 新增异常标记
            with self.status_mgr.status_lock:
                self.status_mgr.status_cache["ai_camera"]["abnormal_type"] = "camera_disconnect"
            return False, {"err_msg": str(e)}


# ========== 状态管理层 ==========
class StatusManager:
    def __init__(self, stm32_adapter, mqtt_srv):
        self.stm32 = stm32_adapter
        self.mqtt_service = mqtt_srv  # 保存MQTT实例
        self.status_lock = threading.Lock()
        self._start_system_monitor_thread()
        
        # 全量状态缓存（内存缓存，对应协议status结构）
        self.status_cache = {
            "print_state": PRINT_STATE["IDLE"],
            "motion": {
                "position": {"x":0.0,"y":0.0,"z":0.0,"e":0.0},
                "current_velocity": 0.0,
                "motors_enabled": False,
                "homed":{"x":False,"y":False,"z":False},
                "driver_status":{"x":"normal","y":"normal","z":"normal","e":"normal"}
            },
            "temperature": {
                "nozzle": {"current":25.0, "target":0.0, "heating":False},
                "bed": {"current":25.0, "target":0.0, "heating":False}
            },
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
            "light": {
                "switch": False,
                "brightness": 100,
                "r": 255,
                "g": 255,
                "b": 255,
                "mode": "normal"
            },
            "ai_camera": {
                "hardware_switch": False,
                "enabled": False,
                "current_mode": None,
                "last_detect_result": "normal",
                "confidence": 0.0,
                "abnormal_type": None,
                "detect_running": False,
                "last_snap_time": 0,
                "ai_thread_exit": False,
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
        # self.stm32.status_callback = self._on_klipper_status_update

    def _on_klipper_status_update(self, klipper_state):
        try:
            logger.debug(f"进入状态回调，原始数据:{list(klipper_state.keys())}")
            with self.status_lock:
                temp_root = self.status_cache.get("temperature", {})
                nozzle_cache = temp_root.get("nozzle", {})
                bed_cache = temp_root.get("bed", {})
                temp_out = self.status_cache["temperature"]

                # 喷头增量更新
                if "extruder" in klipper_state:
                    extruder_upd = klipper_state["extruder"]
                    for k, v in extruder_upd.items():
                        if k in ("current", "target", "power"):
                            temp_out["nozzle"][k] = v
                # 热床增量更新
                if "heater_bed" in klipper_state:
                    bed_upd = klipper_state["heater_bed"]
                    for k, v in bed_upd.items():
                        if k in ("current", "target", "power"):
                            temp_out["bed"][k] = v

                # 安全读取功率
                extruder_data = klipper_state.get("extruder", {})
                bed_data = klipper_state.get("heater_bed", {})
                extruder_power = extruder_data.get("power", 0.0)
                bed_power = bed_data.get("power", 0.0)
                temp_out["nozzle"]["heating"] = extruder_power > 0
                temp_out["bed"]["heating"] = bed_power > 0

                # 运动轴
                if "toolhead" in klipper_state:
                    th = klipper_state["toolhead"]
                    motion = self.status_cache["motion"]
                    if "position" in th:
                        pos = th["position"]
                        motion["position"]["x"] = pos[0]
                        motion["position"]["y"] = pos[1]
                        motion["position"]["z"] = pos[2]
                        motion["position"]["e"] = pos[3]
                    if "homed_axes" in th:
                        ha = th["homed_axes"]
                        motion["homed"]["x"] = "x" in ha
                        motion["homed"]["y"] = "y" in ha
                        motion["homed"]["z"] = "z" in ha
                    if "velocity" in th:
                        motion["current_velocity"] = round(th["velocity"],1)

                # 打印状态
                if "print_stats" in klipper_state:
                    ps = klipper_state["print_stats"]
                    prog = self.status_cache["print_progress"]
                    state_map = {
                        "standby": PRINT_STATE["IDLE"],
                        "printing": PRINT_STATE["PRINTING"],
                        "paused": PRINT_STATE["paused"],
                        "complete": PRINT_STATE["COMPLETE"],
                        "cancelled": PRINT_STATE["IDLE"],
                        "error": PRINT_STATE["ERROR"]
                    }
                    if "state" in ps:
                        self.status_cache["print_state"] = state_map.get(ps["state"], PRINT_STATE["IDLE"])
                    if "filename" in ps:
                        prog["current_file"] = ps["filename"]
                    if "print_duration" in ps:
                        prog["print_duration"] = round(ps.get("print_duration"),1)
                    if "progress" in ps:
                        prog["progress_percent"] = round(ps.get("progress",0)*100,2)
                    if "filament_used" in ps:
                        prog["filament_used"] = round(ps.get("filament_used"),2)

                # 加温判断
                nozzle_target = temp_out["nozzle"].get("target", 0.0)
                bed_target = temp_out["bed"].get("target", 0.0)
                curr_st = self.status_cache["print_state"]
                if curr_st == PRINT_STATE["IDLE"] and (nozzle_target > 0 or bed_target > 0):
                    self.status_cache["print_state"] = PRINT_STATE["HEATING"]
        except Exception as e:
            logger.error(f"notify回调异常: {e}", exc_info=True)

    def _load_persistent_data(self):
        """加载持久化配置和断点数据"""
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r") as f:
                    config = json.load(f)
                    logger.info("持久化配置加载完成")
            except Exception as e:
                logger.warning(f"配置文件加载失败：{e}")

    def get_full_status(self):
        logger.debug("get_full_status：尝试获取status_lock锁")
        with self.status_lock:
            logger.debug("get_full_status：成功拿到锁，复制缓存")
            snap = json.loads(json.dumps(self.status_cache))
            logger.debug("get_full_status：缓存复制完成，释放锁")
            return snap

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

    def _start_system_monitor_thread(self):
        """启动系统状态采集线程"""
        def monitor_loop():
            while True:
                try:
                    sys_data = self._collect_system_status()
                    with self.status_lock:
                        self.status_cache["system_status"] = sys_data
                except Exception as e:
                    logger.error(f"系统状态采集异常：{e}")
                time.sleep(2)  # 2秒采集一次

        threading.Thread(target=monitor_loop, daemon=True, name="system_monitor").start()
        logger.info("系统状态采集线程启动")

    def _collect_system_status(self):
        """
        纯原生/proc/sysfs采集系统状态，无psutil依赖
        返回 {cpu_usage, mem_usage, soc_temp, storage_usage}
        """
        # 1. 计算CPU使用率（两次/proc/stat采样差值）
        def read_cpu_total():
            with open("/proc/stat", "r") as f:
                line = f.readline()
            parts = list(map(int, line.split()[1:]))
            total = sum(parts)
            idle = parts[3] + parts[4]
            return total, idle
        t1_total, t1_idle = read_cpu_total()
        time.sleep(0.5)
        t2_total, t2_idle = read_cpu_total()
        delta_total = t2_total - t1_total
        delta_idle = t2_idle - t1_idle
        cpu_usage = round((1 - delta_idle / delta_total) * 100, 1) if delta_total > 0 else 0.0

        # 2. 内存使用率 /proc/meminfo
        with open("/proc/meminfo", "r") as f:
            mem_lines = f.readlines()
        mem_data = {}
        for line in mem_lines:
            k, v = line.split(":")
            mem_data[k.strip()] = int(v.strip().split()[0])
        total_kb = mem_data["MemTotal"]
        avail_kb = mem_data["MemAvailable"]
        mem_usage = round((1 - avail_kb / total_kb) * 100, 1)

        # 3. 根分区存储使用率（df / 原生读取）
        statvfs = os.statvfs("/")
        total_blocks = statvfs.f_blocks * statvfs.f_frsize
        free_blocks = statvfs.f_bfree * statvfs.f_frsize
        storage_usage = round((1 - free_blocks / total_blocks) * 100, 1) if total_blocks > 0 else 0.0

        # 4. RK3566 SOC温度 /sys/class/thermal/thermal_zone0 毫摄氏度转℃
        soc_temp = 0.0
        thermal_path = "/sys/class/thermal/thermal_zone0/temp"
        if os.path.exists(thermal_path):
            try:
                with open(thermal_path, "r") as f:
                    temp_milli = int(f.read().strip())
                    soc_temp = round(temp_milli / 1000, 1)
            except Exception:
                pass

        return {
            "cpu_usage": cpu_usage,
            "mem_usage": mem_usage,
            "soc_temp": soc_temp,
            "storage_usage": storage_usage
        }

    # def set_ai_mode(self, enable: bool, mode: str):
    #     """切换AI检测模式，启停后台检测线程"""
    #     with self.status_lock:
    #         old_enable = self.status_cache["ai_camera"]["enabled"]
    #         old_mode = self.status_cache["ai_camera"]["current_mode"]
    #         self.status_cache["ai_camera"]["enabled"] = enable
    #         self.status_cache["ai_camera"]["current_mode"] = mode
            
    #         # ===== 新增：关闭AI时设置线程退出标记 =====
    #         if not enable:
    #             self.status_cache["ai_camera"]["ai_thread_exit"] = True
            
    #         # 开关或模式变更，重启检测线程
    #         if old_enable != enable or old_mode != mode:
    #             if enable:
    #                 self._start_ai_detect_thread()
    #             else:
    #                 self.status_cache["ai_camera"]["detect_running"] = False
    #     logger.info(f"AI摄像头模式切换 enable={enable}, mode={mode}")
    def set_ai_mode(self, enable: bool, mode: str):
        """切换AI检测模式，启停后台检测线程"""
        # 仅读写状态缓存，快速释放锁，不在线程创建逻辑内持有锁
        with self.status_lock:
            old_enable = self.status_cache["ai_camera"]["enabled"]
            old_mode = self.status_cache["ai_camera"]["current_mode"]
            self.status_cache["ai_camera"]["enabled"] = enable
            self.status_cache["ai_camera"]["current_mode"] = mode
            if not enable:
                self.status_cache["ai_camera"]["ai_thread_exit"] = True
        # 锁释放后，再执行线程启停（避免锁长期占用导致指令超时）
        if old_enable != enable or old_mode != mode:
            if enable:
                self._start_ai_detect_thread()
            else:
                with self.status_lock:
                    self.status_cache["ai_camera"]["detect_running"] = False 
        logger.info(f"AI摄像头模式切换 enable={enable}, mode={mode}")
        
    def _start_ai_detect_thread(self):
        """AI定时图像检测后台线程，打印状态为PRINTING时自动抓拍分析"""
        # 增加判断，已运行直接返回
        with self.status_lock:
            if self.status_cache["ai_camera"]["detect_running"]:
                logger.info("AI检测线程已存在，无需重复创建")
                return
        def ai_loop():
            detect_interval = 5  # 5秒检测一次
            while True:
                # ===== 新增：判断线程退出标记 =====
                with self.status_lock:
                    if self.status_cache["ai_camera"]["ai_thread_exit"]:
                        logger.info("AI检测线程收到关闭信号，退出")
                        self.status_cache["ai_camera"]["detect_running"] = False
                        self.status_cache["ai_camera"]["ai_thread_exit"] = False
                        break
                
                with self.status_lock:
                    ai_enable = self.status_cache["ai_camera"]["enabled"]
                    run_flag = self.status_cache["ai_camera"]["detect_running"]
                    print_state = self.status_cache["print_state"]
                if not ai_enable or not run_flag or print_state != PRINT_STATE["PRINTING"]:
                    time.sleep(1)
                    continue
                # 调用抓拍接口
                success, snap_info = self.stm32._capture_snapshot()
                if not success:
                    logger.warning("AI自动抓拍失败，跳过本轮检测")
                    time.sleep(detect_interval)
                    continue
                # ======================
                # 此处预留AI图像推理接口（后续接入opencv/YOLO模型）
                # 伪代码示例：
                # result, conf, abnormal = ai_model_infer(snap_info["img_file"])
                result = "normal"
                conf = 0.92
                abnormal = None
                # ======================
                with self.status_lock:
                    self.status_cache["ai_camera"]["last_detect_result"] = result
                    self.status_cache["ai_camera"]["confidence"] = conf
                    self.status_cache["ai_camera"]["abnormal_type"] = abnormal
                    self.status_cache["ai_camera"]["last_snap_time"] = snap_info["capture_time"]
                # 检测到异常，上报EVENT告警
                if result != "normal":
                    self.mqtt_service.publish_event(
                        level="warn",
                        event_type="ai_detect_abnormal",
                        event_code=6001,
                        event_data={
                            "abnormal_type": abnormal,
                            "confidence": conf,
                            "snap_url": snap_info["img_url"]
                        }
                    )
                time.sleep(detect_interval)
        # 启动守护线程
        t = threading.Thread(target=ai_loop, daemon=True, name="ai_detect_bg")
        t.start()
        with self.status_lock:
            self.status_cache["ai_camera"]["detect_running"] = True

# ========== MQTT核心层 ==========
class MQTTService:
    def __init__(self, status_manager, stm32_adapter):
        self.status_mgr = status_manager
        self.stm32 = stm32_adapter
        self.cmd_queue = queue.PriorityQueue(maxsize=100)
        self.cmd_timeout = 15  # 指令执行超时
        self.fuse_trigger = False  # 熔断标记
        self.fuse_count = 0  # 连续失败次数
        self.fuse_threshold = 5  # 连续失败5次触发熔断
        self.client = None
        self.connected = False
        self.reconnect_delay = 1  # 重连初始间隔，指数退避
        self.max_reconnect_delay = 60
        
        # 断线缓存队列（关键事件/响应，上线补发）
        self.offline_cache = deque(maxlen=MAX_CACHE_EVENT)
        self.cache_lock = threading.Lock()
        
        # 指令优先级队列
        # self.cmd_queue = queue.PriorityQueue()
        
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
        # self.client = mqtt.Client(client_id=DEVICE_ID, callback_api_version=mqtt.CallbackAPIVersion.VERSION1)
        self.client = mqtt.Client(
            client_id=DEVICE_ID,
            # callback_api_version=mqtt.CallbackAPIVersion.VERSION2,  # 最新API
            protocol=mqtt.MQTTv311  # 建议先改用v3.1.1，兼容绝大多数broker
        )
        self.client.username_pw_set(MQTT_USER, MQTT_PWD)
        
        # 双向TLS配置
        context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        context.load_verify_locations(CA_CERT_PATH)
        context.load_cert_chain(certfile=CLIENT_CERT_PATH, keyfile=CLIENT_KEY_PATH)
        # 启用TLSv1.3，禁用弱加密套件
        # context.options |= ssl.OP_NO_TLSv1 | ssl.OP_NO_TLSv1_1 | ssl.OP_NO_TLSv1_2
        # 仅禁用 TLS1.0 和 1.1，保留1.2和1.3，保证兼容性
        # context.options |= ssl.OP_NO_TLSv1 | ssl.OP_NO_TLSv1_1    ---这类常量在新版 Python ssl 库被标记废弃
        context.set_ciphers('ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384')
        self.client.tls_set_context(context)
        self.client.tls_insecure_set(False)  # 强制校验服务端证书
        
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
        if rc == 0:
            self.connected = True
            self.reconnect_delay = 1
            logger.info("✅ MQTT双向TLS连接成功")
            
            # 订阅topic分段日志
            logger.info("开始订阅下行指令Topic...")
            cmd_topics = [
                (f"device/{DEVICE_ID}/cmd/print", 1),
                (f"device/{DEVICE_ID}/cmd/param", 1),
                (f"device/{DEVICE_ID}/cmd/motion", 1),
                (f"device/{DEVICE_ID}/cmd/file", 1),
                (f"device/{DEVICE_ID}/cmd/system", 1),
                (f"device/{DEVICE_ID}/cmd/ai", 1),
                (f"device/{DEVICE_ID}/cmd/light", 1),
            ]
            client.subscribe(cmd_topics)
            logger.info("所有下行指令Topic订阅完成")

            # 发布设备信息日志
            logger.info("即将执行设备信息发布...")
            self._publish_device_info()
            logger.info("设备基础信息已发布完成")

            # 补发缓存日志
            logger.info("开始补发离线缓存消息...")
            self._flush_offline_cache()
            logger.info("断线缓存消息已全部补发")

            # 核心卡点：单独捕获上报线程启动异常
            logger.info("==================== 准备启动定时上报线程 ====================")
            try:
                self._start_report_thread()
                logger.info("==================== 定时上报线程创建成功 ====================")
            except Exception as thread_err:
                logger.error("启动上报线程抛出异常！", exc_info=True)
        else:
            logger.error(f"❌ MQTT连接失败，错误码：{rc}")

    def _on_disconnect(self, client, userdata, rc):
        """连接断开回调，指数退避重连（不阻塞事件循环）"""
        self.connected = False
        logger.warning(f"MQTT连接断开，错误码：{rc}，{self.reconnect_delay}s后重连")
        
        # 启动单独线程执行重连，不阻塞事件循环
        def reconnect_worker():
            while not self.connected:
                try:
                    time.sleep(self.reconnect_delay)
                    self.client.reconnect()
                    # 指数退避
                    self.reconnect_delay = min(self.reconnect_delay * 2, self.max_reconnect_delay)
                except Exception as e:
                    logger.error(f"重连失败：{e}，{self.reconnect_delay}s后重试")
        
        threading.Thread(target=reconnect_worker, daemon=True, name="mqtt_reconnect").start()

    def _on_message(self, client, userdata, msg):
        """下行消息接收回调"""
        try:
            # 外层统一捕获JSON解析、基础字段缺失异常
            payload = json.loads(msg.payload.decode())
            logger.info(f"【MQTT收到PC下行指令】topic={msg.topic}, 完整报文={json.dumps(payload, ensure_ascii=False)}")
            request_id = payload.get("request_id", "")
            cmd_type = payload.get("cmd_type", "")
            logger.info(f"【指令基础信息】request_id={request_id}, cmd_type={cmd_type}")

            # 校验下行指令必填字段
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

            cmd_type = payload["cmd_type"]
            request_id = payload["request_id"]
            # 计算优先级，只算一次
            priority = self._get_cmd_priority(cmd_type)

            # 熔断拦截：设备持续故障拒绝新指令
            if self.fuse_trigger:
                self._send_error_response(
                    request_id, cmd_type,
                    ERROR_CODE["DEVICE_BUSY"],
                    "设备熔断保护中，暂不接收指令"
                )
                return

            # 队列满拦截 + 单独捕获入队异常
            if self.cmd_queue.full():
                self._send_error_response(
                    request_id, cmd_type,
                    ERROR_CODE["DEVICE_BUSY"],
                    "指令队列已满，请稍后重试"
                )
                logger.warning(f"指令队列已满，丢弃指令 request_id:{request_id}")
                return

            # 安全入队，单独捕获队列异常
            try:
                self.cmd_queue.put((priority, msg.topic, payload), block=False)
            except queue.Full:
                self._send_error_response(
                    request_id, cmd_type,
                    ERROR_CODE["DEVICE_BUSY"],
                    "指令队列瞬时满载"
                )
                logger.warning(f"put队列临时满，丢弃 {request_id}")

        except json.JSONDecodeError:
            logger.error(f"下行指令JSON格式非法，丢弃消息，原始数据：{repr(msg.payload)}")
        except Exception as e:
            logger.error(f"消息整体处理异常：{str(e)}", exc_info=True)

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
        """外层封装：负责超时熔断，调用真实执行逻辑"""
        request_id = payload["request_id"]
        cmd_type = payload["cmd_type"]
        data = payload["data"]

        try:
            # 启动执行线程，限时等待
            work_thread = threading.Thread(
                target=self._do_execute,
                args=(request_id, cmd_type, data),
                daemon=True
            )
            work_thread.start()
            work_thread.join(timeout=self.cmd_timeout)

            if work_thread.is_alive():
                raise TimeoutError("指令执行超时")

        except TimeoutError:
            self._send_error_response(request_id, cmd_type, ERROR_CODE["MCU_TIMEOUT"], "指令执行超时")
            self.fuse_count += 1
            # 连续失败达阈值，触发熔断
            if self.fuse_count >= self.fuse_threshold and not self.fuse_trigger:
                self.fuse_trigger = True
                threading.Timer(5, self._reset_fuse).start()
                logger.warning("连续执行失败，触发设备熔断保护")

        except Exception as e:
            self._send_error_response(request_id, cmd_type, ERROR_CODE["SYSTEM_ERROR"], str(e))
            self.fuse_count += 1
            if self.fuse_count >= self.fuse_threshold and not self.fuse_trigger:
                self.fuse_trigger = True
                threading.Timer(5, self._reset_fuse).start()
        else:
            self.fuse_count = 0  # 执行成功，重置熔断计数

    def _do_execute(self, request_id, cmd_type, data):
        """真实指令处理：查询校验、硬件调用、状态更新、返回响应"""
        logger.info(f"【指令正式处理开始】request_id={request_id}, cmd_type={cmd_type}, 原始data={data}")
        # 1. 查询类指令：直接返回缓存，不访问硬件
        if cmd_type == "common_query":
            query_type = data.get("query_type", "")
            if query_type == "full_status":
                resp_data = self.status_mgr.get_full_status()
                self._send_success_response(request_id, cmd_type, resp_data)
            elif query_type == "version":
                resp_data = {
                    "protocol_version": PROTOCOL_VERSION,
                    "app_version": APP_VERSION,
                    "mcu_firmware_version": MCU_FIRMWARE_VERSION,
                    "armbian_version": ARMBIAN_VERSION
                }
                self._send_success_response(request_id, cmd_type, resp_data)
            elif query_type == "config":
                resp_data = self._get_device_config()
                self._send_success_response(request_id, cmd_type, resp_data)
            else:
                self._send_error_response(request_id, cmd_type, ERROR_CODE["PARAM_INVALID"], "未知查询类型")
            return

        # 2. 状态前置校验
        if not self._check_cmd_state_allowed(cmd_type, data):
            self._send_error_response(request_id, cmd_type, ERROR_CODE["STATE_NOT_ALLOW"], "当前状态不允许该操作")
            return

        # 新增：打印启动前置全量校验（移到上层MQTTService，拥有request_id、状态、文件查询能力）
        if cmd_type == "print_control" and data.get("action") == "start":
            req_id = request_id
            file_name = data.get("file_name", "").strip()
            # 校验1：文件名为空
            if not file_name:
                self._send_error_response(req_id, cmd_type, 2002, "打印文件名不能为空")
                return
            # 校验2：当前状态是否允许启动打印
            current_state = self.status_mgr.get_full_status()["print_state"]
            if current_state != PRINT_STATE["IDLE"]:
                self._send_error_response(req_id, cmd_type, 2001, "当前状态不允许启动打印")
                return
            # 校验3：查询文件列表，判断文件是否存在
            if MOCK_MOONRAKER:
                # Mock模拟模式：跳过文件校验，直接放行
                logger.info(f"Mock模式，跳过文件存在校验，文件名:{file_name}")
            else:
                # 真实Moonraker查询文件列表
                file_ok, file_list_resp, file_err = self.stm32._send_moonraker_rpc(
                    "server.files.list", {"root": "gcodes"}
                )
                if not file_ok:
                    self._send_error_response(req_id, cmd_type, 3001, "读取文件列表失败，无法校验文件")
                    return
                file_names = [f["filename"] for f in file_list_resp.get("files", [])]
                if file_name not in file_names:
                    self._send_error_response(req_id, cmd_type, 3001, f"打印文件不存在:{file_name}")
                    return
                    
        # 3. 分发到STM32/Klipper执行
        success, result, err_code = self.stm32.send_command(cmd_type, data)
        # 新增：AI开关成功后推送事件
        if cmd_type == "ai_camera_control" and data.get("action") == "switch" and success:
            self.publish_event(
                level="info",
                event_type="ai_camera_switch",
                event_code=6000,
                event_data={"hardware_switch": data.get("enable")}
            )
        logger.info(f"【硬件调用返回】request_id={request_id}, success={success}, err_code={err_code}, result摘要={str(result)[:200]}")
        if not success:
            self._send_error_response(request_id, cmd_type, err_code, "硬件执行失败")
            return

        # 4. 打印类指令：状态变更 + 事件推送
        if cmd_type == "print_control" and data.get("action") == "start":
            self.status_mgr.set_print_state(PRINT_STATE["HEATING"])
            self.publish_event("info", "print_start", 1001, {
                "file_name": data.get("file_name", ""),
                "estimate_time": 3600
            })

        if cmd_type == "ai_camera_control" and data.get("action") == "set_mode":
            enable = data.get("enable")
            mode = data.get("mode")
            self.publish_event(
                level="info",
                event_type="ai_mode_switch",
                event_code=6000,
                event_data={"enable": enable, "mode": mode}
            )
            # with self.status_mgr.status_lock:
            #     self.status_mgr.status_cache["ai_camera"]["enabled"] = data.get("enable", True)
            #     self.status_mgr.status_cache["ai_camera"]["current_mode"] = data.get("mode", "")
                
        # 5. 返回成功响应
        logger.info(f"【指令执行完成，准备回复PC】request_id={request_id}")
        self._send_success_response(request_id, cmd_type, result)

    def _get_device_config(self):
        """生成完整设备配置结构体，对齐协议文档"""
        return {
            "device_base": {
                "model": DEVICE_MODEL,
                "serial_no": DEVICE_ID,
                "production_date": "2026-07-02",
                "production_index": 1,
                "mcu_uuid": CAN_MCU_UUID,
                "mcu_restart_method": "command"
            },
            "motion_config": {
                "axis_count": 4,
                "axis_list": ["x", "y", "z", "e"],
                "max_speed": {"x": 300, "y": 300, "z": 20, "e": 50},
                "max_accel": {"x": 3000, "y": 3000, "z": 500, "e": 1000},
                "travel_range": {"x": 220, "y": 220, "z": 250},
                "homing_order": ["z", "x", "y"],
                "motor_type": "stepper_tmc2209"
            },
            "extruder_config": {
                "nozzle_count": 1,
                "nozzle_diameter": 0.4,
                "max_temp": 260,
                "min_temp": 0,
                "pid_support": True,
                "filament_diameter": 1.75
            },
            "bed_config": {
                "support": True,
                "max_temp": 110,
                "min_temp": 0,
                "size": {"x": 220, "y": 220},
                "pid_support": True,
                "leveling_support": True
            },
            "fan_config": {
                "part_cooling_fan": True,
                "nozzle_cooling_fan": True,
                "fan_count": 2,
                "speed_range": [0, 100]
            },
            "ai_camera_config": {
                "support": True,
                "model": "USB-1080P-HD",
                "support_modes": ["first_layer_detect", "warpage_detect", "full_process_monitor"],
                "rtsp_url": f"rtsp://{{device_ip}}:8554/stream",
                "http_media_base": f"http://{{device_ip}}:8080/media",
                "resolution": "1920x1080",
                "encode_codec": "h264",
                "stream_fps": 15,
                "thumbnail_size": "320x240",
                "media_expire_hour": 72
            },
            "system_version": {
                "armbian_version": ARMBIAN_VERSION,
                "mcu_firmware_version": MCU_FIRMWARE_VERSION,
                "app_firmware_version": APP_VERSION,
                "protocol_version": PROTOCOL_VERSION
            },
            "light_config": {
                "support": True,
                "channel_count": 3,
                "max_brightness": 100,
                "support_mode": ["normal", "breath", "flash"],
            },
        }

    def _publish_device_info(self):
        """发布设备基础信息（保留消息）"""
        info_msg = self._build_common_header("device_info")
        info_msg["data"] = {
            "device_id": DEVICE_ID,
            "device_model": DEVICE_MODEL,
            "protocol_version": PROTOCOL_VERSION,
            "mcu_firmware_version": MCU_FIRMWARE_VERSION,
            "app_version": APP_VERSION,
            "armbian_version": ARMBIAN_VERSION,
            "mcu_chip_type": MCU_CHIP_TYPE,
            "can_mcu_uuid": CAN_MCU_UUID
        }
        self.client.publish(
            topic=f"device/{DEVICE_ID}/info",
            payload=json.dumps(info_msg),
            qos=1,
            retain=True
        )
        logger.info("设备基础信息已发布")

    def _reset_fuse(self):
        """重置熔断状态，仅做状态重置，无业务逻辑"""
        self.fuse_trigger = False
        self.fuse_count = 0
        logger.info("设备熔断状态已重置，恢复指令接收")

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
        logger.info(f"【成功响应已推送至MQTT】request_id={request_id}")

    def _send_error_response(self, request_id, cmd_type, error_code, msg):
        """发送错误响应"""
        resp = self._build_response_msg(request_id, cmd_type, 1, error_code, msg, {})
        self.publish(f"device/{DEVICE_ID}/response", resp, qos=1)
        logger.error(f"【错误响应已推送至MQTT】request_id={request_id}, error_code={error_code}, msg={msg}")

    def _start_report_thread(self):
        logger.info("进入_start_report_thread函数，准备创建线程")
        def report_loop():
            logger.info("===== 状态上报守护线程 已成功进入循环 ====")
            loop_cnt = 0
            while True:
                try:
                    logger.debug(f"上报循环[{loop_cnt}] 轮次开始，connected={self.connected}")
                    if self.connected:
                        logger.debug(f"开始读取全量状态缓存")
                        status = self.status_mgr.get_full_status()
                        logger.debug(f"缓存读取完毕，打印状态：{status['print_state']}")
                        msg = self._build_common_header("status")
                        msg["data"] = status
                        logger.debug(f"开始调用publish上报topic=device/{DEVICE_ID}/status")
                        pub_topic = f"device/{DEVICE_ID}/status"
                        logger.debug(f"定时上报发布完整topic：{pub_topic}")
                        self.publish(pub_topic, msg, qos=1)
                        logger.info(f"【定时上报成功 第{loop_cnt}次】打印状态:{status['print_state']}")
                        loop_cnt += 1
                    else:
                        logger.debug(f"MQTT离线，跳过本次上报")
                except Exception as e:
                    logger.error(f"【上报单次异常】{e}", exc_info=True)
                logger.debug(f"本轮结束，休眠{STATUS_REPORT_INTERVAL}秒")
                time.sleep(STATUS_REPORT_INTERVAL)
        t = threading.Thread(target=report_loop, daemon=True, name="status_report")
        t.start()
        logger.info("上报线程Thread已调用start()，后台运行")

    def publish_event(self, level, event_type, event_code, event_data):
        """发布事件（触发式，QoS=1）"""
        event_msg = self._build_event_msg(level, event_type, event_code, event_data)
        self.publish(f"device/{DEVICE_ID}/event", event_msg, qos=1)

    def publish(self, topic, payload, qos=0, retain=False):
        payload_str = json.dumps(payload, ensure_ascii=False)
        logger.debug(f"publish 准备发送 topic={topic}, qos={qos}, connected={self.connected}")
        if self.connected:
            try:
                pub_info = self.client.publish(topic, payload_str, qos=qos, retain=retain)
                pub_info.wait_for_publish(timeout=2)
                if pub_info.is_published():
                    logger.debug(f"publish成功，mid={pub_info.mid}")
                else:
                    logger.error(f"publish发送未确认 mid={pub_info.mid}")
            except Exception as pub_err:
                logger.error(f"publish发送异常：{pub_err}", exc_info=True)
        else:
            with self.cache_lock:
                self.offline_cache.append((topic, payload_str, qos, retain))
            logger.debug(f"离线缓存消息 topic={topic}")

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
        """启动服务，指数退避重试初始连接"""
        logger.info("MQTT服务启动中...")
        reconnect_delay = 1
        max_reconnect_delay = 60

        while not self.connected:
            try:
                self.client.connect(MQTT_BROKER, MQTT_PORT, KEEP_ALIVE)
                break
            except Exception as e:
                logger.error(f"初始连接失败：{e}，{reconnect_delay}s后重试")
                time.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)

        self.client.loop_start()

    def _make_err_resp(self, err_code, msg):
        """生成硬件调用格式的错误返回值，适配send_command返回结构"""
        return False, {}, err_code

# ========== 主程序入口 ==========
if __name__ == "__main__":
    logger.info("="*50)
    logger.info("3D打印机RK3566 MQTT服务启动")
    logger.info(f"设备ID：{DEVICE_ID}")
    logger.info(f"协议版本：{PROTOCOL_VERSION}")
    logger.info("="*50)
    
    # 1. 初始化状态管理器
    status_manager = StatusManager(None, None)
    # 2. 创建适配器，变量名 stm32_adapter
    stm32_adapter = STM32Adapter(status_manager)
    # 3. 双向绑定，正确变量名 stm32_adapter
    status_manager.stm32 = stm32_adapter
    # 4. 赋值回调（必须在创建实例之后）
    stm32_adapter.status_callback = status_manager._on_klipper_status_update

    # 5. 创建MQTT服务
    mqtt_service = MQTTService(status_manager, stm32_adapter)
    status_manager.mqtt_service = mqtt_service
    
    # 启动MQTT服务
    mqtt_service.start()
    
    # 主线程保活
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("服务被手动终止")
        mqtt_service.client.disconnect()
        exit(0)