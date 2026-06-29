import paho.mqtt.client as mqtt
import json
import time

# ============== 配置，和EMQX账号一致 ===================
# MQTT_HOST = "127.0.0.1"
# MQTT_PORT = 1883
# 外网花生壳远程访问，启用下面两行，注释上面两行
MQTT_HOST = "4ry508ao2807.vicp.fun"
MQTT_PORT = 34796
MQTT_USER = "admin"
MQTT_PWD = "123"
DEVICE_SN = "rk3566_001"
# ======================================================

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("✅ PC业务端连接Broker成功")
        # 订阅打印机上报的话题
        client.subscribe(f"printer/{DEVICE_SN}/data")
    else:
        print(f"❌ 连接失败，错误码：{rc}")

def on_message(client, userdata, msg):
    data = json.loads(msg.payload.decode())
    print(f"📥 收到设备[{data['sn']}]上报：{data['data']}")

if __name__ == "__main__":
    # client = mqtt.Client("pc_test_service")
    client = mqtt.Client(client_id="pc_test_service", callback_api_version=mqtt.CallbackAPIVersion.VERSION1)
    client.username_pw_set(MQTT_USER, MQTT_PWD)
    client.on_connect = on_connect
    client.on_message = on_message

    client.connect(MQTT_HOST, MQTT_PORT, 60)
    client.loop_start()

    # 测试：10秒后下发一条指令
    time.sleep(10)
    cmd = {
        "cmd": "set_temp",
        "params": {"nozzle": 220}
    }
    client.publish(f"printer/{DEVICE_SN}/cmd", json.dumps(cmd))
    print("📤 已下发测试指令")

    while True:
        time.sleep(1)