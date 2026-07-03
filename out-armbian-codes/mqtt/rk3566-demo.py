import paho.mqtt.client as mqtt
import json
import time

# ========== 连接配置 ==========
PC_IP = "4ry508ao2807.vicp.fun"
MQTT_PORT = 34796  # 花生壳外网端口，对应内网EMQX 8883
MQTT_USER = "rk3566"      
MQTT_PWD = "123"  
DEVICE_SN = "rk3566_001"

# ========== 新增：双向TLS证书路径（设备端Linux路径） ==========
CA_CERT_PATH = "/root/mqtt-tls-test/ca.crt"
CLIENT_CERT_PATH = "/root/mqtt-tls-test/client_rk3566_001.crt"
CLIENT_KEY_PATH = "/root/mqtt-tls-test/client_rk3566_001.key"
# ==========================================================

# 设备状态
device_state = {
    "nozzle_temp": 205.2,
    "bed_temp": 60.1,
    "print_progress": 35.6
}

# 连接回调
def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("✅ RK3566 双向TLS连接成功")
        client.subscribe(f"printer/{DEVICE_SN}/cmd")
    else:
        print(f"❌ 连接失败，错误码：{rc}")

# 指令执行函数
def execute_cmd(cmd, params):
    global device_state
    result = {"success": False, "msg": ""}
    if cmd == "set_temp":
        if "nozzle" not in params:
            result["msg"] = "参数缺失：nozzle"
        else:
            device_state["nozzle_temp"] = params["nozzle"]
            result["success"] = True
            result["msg"] = f"喷嘴温度已设置为{params['nozzle']}℃"
    elif cmd == "set_bed_temp":
        if "bed" not in params:
            result["msg"] = "参数缺失：bed"
        else:
            device_state["bed_temp"] = params["bed"]
            result["success"] = True
            result["msg"] = f"床温已设置为{params['bed']}℃"
    elif cmd == "get_status":
        result["success"] = True
        result["data"] = device_state
        result["msg"] = "获取状态成功"
    else:
        result["msg"] = f"未知指令：{cmd}"
    return result

# 消息回调
def on_message(client, userdata, msg):
    try:
        cmd = json.loads(msg.payload.decode())
        print(f"📥 收到指令：{cmd['cmd']}，参数：{cmd['params']}")
        
        exec_result = execute_cmd(cmd["cmd"], cmd.get("params", {}))
        
        result_payload = {
            "sn": DEVICE_SN,
            "time": int(time.time()),
            "cmd": cmd["cmd"],
            "result": exec_result
        }
        client.publish(f"printer/{DEVICE_SN}/cmd_result", json.dumps(result_payload))
        print(f"📤 指令执行结果已上报：{exec_result}")
        
    except json.JSONDecodeError:
        print("❌ 指令格式错误：非合法JSON")
    except KeyError as e:
        print(f"❌ 指令字段缺失：{e}")
    except Exception as e:
        print(f"❌ 指令执行异常：{e}")

if __name__ == "__main__":
    # client = mqtt.Client(client_id=DEVICE_SN, callback_api_version=mqtt.CallbackAPIVersion.VERSION1)
    client = mqtt.Client(client_id=DEVICE_SN)
    client.username_pw_set(MQTT_USER, MQTT_PWD)

    # ========== 核心：双向TLS配置 ==========
    client.tls_set(
        ca_certs=CA_CERT_PATH,
        certfile=CLIENT_CERT_PATH,
        keyfile=CLIENT_KEY_PATH
    )
    # ======================================

    client.on_connect = on_connect
    client.on_message = on_message

    # 自动重连
    connected = False
    while not connected:
        try:
            client.connect(PC_IP, MQTT_PORT, keepalive=60)
            connected = True
        except Exception as e:
            print(f"连接失败，3秒后重试：{e}")
            time.sleep(3)

    client.loop_start()

    try:
        while True:
            payload = {
                "sn": DEVICE_SN,
                "time": int(time.time()),
                "data": device_state
            }
            client.publish(f"printer/{DEVICE_SN}/data", json.dumps(payload))
            print(f"📤 数据已上报：{device_state}")
            time.sleep(3)
    except KeyboardInterrupt:
        client.disconnect()
        print("\n👋 设备端程序被手动终止")
        exit(0)