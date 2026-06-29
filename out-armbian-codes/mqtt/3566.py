import paho.mqtt.client as mqtt
import json
import time

# ========== 配置，和EMQX一致 ==========
# PC_IP = "10.60.1.107"  
# MQTT_PORT = 1883
PC_IP = "4ry508ao2807.vicp.fun"
MQTT_PORT = 34796
MQTT_USER = "rk3566"      
MQTT_PWD = "123"  
DEVICE_SN = "rk3566_001"  # 设备标识
# =====================================

# 连接成功回调
def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("✅ 连接PC服务端成功")
        # 订阅指令话题
        client.subscribe(f"printer/{DEVICE_SN}/cmd")
    else:
        print(f"❌ 连接失败，错误码：{rc}")

# 收到PC指令回调
def on_message(client, userdata, msg):
    cmd = json.loads(msg.payload.decode())
    print(f"📥 收到指令：{cmd['cmd']}，参数：{cmd['params']}")
    # 后续在这里加控制逻辑

if __name__ == "__main__":
    # client = mqtt.Client(client_id=DEVICE_SN, callback_api_version=mqtt.CallbackAPIVersion.VERSION1)
    client = mqtt.Client(client_id=DEVICE_SN)
    client.username_pw_set(MQTT_USER, MQTT_PWD)
    client.on_connect = on_connect
    client.on_message = on_message

    # 自动重连机制，断网后自动尝试重连
    while True:
        try:
            client.connect(PC_IP, MQTT_PORT, keepalive=60)
            break
        except Exception as e:
            print(f"连接失败，3秒后重试：{e}")
            time.sleep(3)

    # 后台运行网络循环
    client.loop_start()

    # 模拟每隔3秒上报一次数据
    while True:
        payload = {
            "sn": DEVICE_SN,
            "time": int(time.time()),
            "data": {
                "nozzle_temp": 205.2,
                "bed_temp": 60.1,
                "print_progress": 35.6
            }
        }
        client.publish(f"printer/{DEVICE_SN}/data", json.dumps(payload))
        print("📤 数据已上报")
        time.sleep(3)
