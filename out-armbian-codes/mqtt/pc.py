import paho.mqtt.client as mqtt
import json
import time
import threading

# ============== 配置（保持不变） ==============
MQTT_HOST = "4ry508ao2807.vicp.fun"
MQTT_PORT = 34796
MQTT_USER = "admin"
MQTT_PWD = "123"
DEVICE_SN = "rk3566_001"
# ==============================================

# 连接成功回调（新增订阅「指令结果」话题）
def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("✅ PC业务端连接Broker成功")
        client.subscribe(f"printer/{DEVICE_SN}/data")          # 设备状态上报
        client.subscribe(f"printer/{DEVICE_SN}/cmd_result")    # 指令执行结果
    else:
        print(f"❌ 连接失败，错误码：{rc}")

# 修改：收到消息回调（区分数据上报/指令结果）
def on_message(client, userdata, msg):
    try:
        data = json.loads(msg.payload.decode())
        if msg.topic == f"printer/{DEVICE_SN}/data":
            print(f"\n📥 收到设备[{data['sn']}]状态上报：{data['data']}")
        elif msg.topic == f"printer/{DEVICE_SN}/cmd_result":
            print(f"\n📥 设备[{data['sn']}]指令执行结果：")
            print(f"  指令：{data['cmd']}")
            print(f"  结果：{data['result']}")
    except json.JSONDecodeError:
        print(f"❌ 消息格式错误：{msg.payload}")
    except Exception as e:
        print(f"❌ 消息解析异常：{e}")

# 新增：手动输入指令的线程（避免阻塞MQTT循环）
def input_cmd_thread(client):
    print("\n===== 指令下发控制台 =====")
    print("支持指令：")
    print("1. set_temp {\"nozzle\": 220}  # 设置喷嘴温度")
    print("2. set_bed_temp {\"bed\": 65}  # 设置床温")
    print("3. get_status {}  # 获取设备状态")
    print("输入 'exit' 退出程序")
    while True:
        try:
            user_input = input("\n请输入指令（格式：指令名 参数JSON）：")
            if user_input.strip() == "exit":
                client.disconnect()
                print("👋 程序已退出")
                exit(0)
            # 解析用户输入（示例：set_temp {"nozzle":220}）
            cmd_part, params_part = user_input.split(" ", 1)
            cmd = cmd_part.strip()
            params = json.loads(params_part.strip())
            
            # 下发指令
            cmd_payload = {"cmd": cmd, "params": params}
            client.publish(f"printer/{DEVICE_SN}/cmd", json.dumps(cmd_payload))
            print(f"📤 已下发指令：{cmd_payload}")
        except ValueError:
            print("❌ 格式错误！示例：set_temp {\"nozzle\": 220}")
        except json.JSONDecodeError:
            print("❌ 参数JSON格式错误！")
        except Exception as e:
            print(f"❌ 指令下发失败：{e}")

if __name__ == "__main__":
    # client = mqtt.Client("pc_test_service")
    client = mqtt.Client(client_id="pc_test_service", callback_api_version=mqtt.CallbackAPIVersion.VERSION1)
    client.username_pw_set(MQTT_USER, MQTT_PWD)
    client.on_connect = on_connect
    client.on_message = on_message

    # 新增：自动重连逻辑（和设备端一致）
    connected = False
    while not connected:
        try:
            client.connect(MQTT_HOST, MQTT_PORT, 60)
            connected = True
        except Exception as e:
            print(f"连接失败，3秒后重试：{e}")
            time.sleep(3)

    client.loop_start()

    # 启动手动输入指令的线程
    threading.Thread(target=input_cmd_thread, args=(client,), daemon=True).start()

    # 保持主线程运行（替代原有的固定10秒测试指令）
    while True:
        time.sleep(1)