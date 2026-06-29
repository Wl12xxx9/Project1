import paho.mqtt.client as mqtt
import json
import time

# ========== 配置（保持不变） ==========
PC_IP = "4ry508ao2807.vicp.fun"
MQTT_PORT = 34796
MQTT_USER = "rk3566"      
MQTT_PWD = "123"  
DEVICE_SN = "rk3566_001"  
# =====================================

# 新增：设备状态变量（模拟真实硬件状态）
device_state = {
    "nozzle_temp": 205.2,
    "bed_temp": 60.1,
    "print_progress": 35.6
}

# 连接成功回调（保持不变）
def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("✅ 连接PC服务端成功")
        client.subscribe(f"printer/{DEVICE_SN}/cmd")
    else:
        print(f"❌ 连接失败，错误码：{rc}")

# 新增：指令执行函数（解耦逻辑）
def execute_cmd(cmd, params):
    global device_state
    result = {"success": False, "msg": ""}
    if cmd == "set_temp":
        # 校验参数
        if "nozzle" not in params:
            result["msg"] = "参数缺失：nozzle"
        else:
            # 模拟设置喷嘴温度（实际场景替换为硬件控制代码）
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
        # 返回当前设备状态
        result["success"] = True
        result["data"] = device_state
        result["msg"] = "获取状态成功"
    else:
        result["msg"] = f"未知指令：{cmd}"
    return result

# 修改：收到PC指令回调（新增指令处理+结果上报）
def on_message(client, userdata, msg):
    try:
        cmd = json.loads(msg.payload.decode())
        print(f"📥 收到指令：{cmd['cmd']}，参数：{cmd['params']}")
        
        # 执行指令
        exec_result = execute_cmd(cmd["cmd"], cmd.get("params", {}))
        
        # 上报指令执行结果
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
    client.on_connect = on_connect
    client.on_message = on_message

    # 优化重连逻辑（避免无限循环创建连接）
    connected = False
    while not connected:
        try:
            client.connect(PC_IP, MQTT_PORT, keepalive=60)
            connected = True
        except Exception as e:
            print(f"连接失败，3秒后重试：{e}")
            time.sleep(3)

    client.loop_start()

    # 修改：上报真实设备状态（而非固定值）
    while True:
        payload = {
            "sn": DEVICE_SN,
            "time": int(time.time()),
            "data": device_state  # 使用实时状态
        }
        client.publish(f"printer/{DEVICE_SN}/data", json.dumps(payload))
        print(f"📤 数据已上报：{device_state}")
        time.sleep(3)