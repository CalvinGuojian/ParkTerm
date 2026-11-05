"""串口调试（一级环境）：测试 login 命令的真实提示符行为（不提交密码）"""
import time
import serial

PORT = "COM3"
BAUD = 115200

ser = serial.Serial(PORT, BAUD, timeout=0.1, write_timeout=2.0)
print(f"Opened {PORT} @ {BAUD}")


def read_for(max_t, quiet=1.0):
    buf = b""
    end = time.time() + max_t
    last = time.time()
    while time.time() < end:
        try:
            n = ser.in_waiting
        except Exception as e:
            print(f"[read error: {e}]")
            break
        if n:
            buf += ser.read(n)
            last = time.time()
        elif time.time() - last > quiet:
            break
        else:
            time.sleep(0.02)
    return buf


def show(tag, data):
    print(f"\n===== {tag} =====")
    print(repr(data.decode("utf-8", errors="replace")))


def send(data, wait=2.0, tag=""):
    ser.write(data if isinstance(data, bytes) else data.encode())
    out = read_for(wait)
    show(tag or repr(data), out)
    return out


ser.reset_input_buffer()
send("\r", 2.0, "wake (confirm level1)")
# 一级环境下执行 login，观察真实提示符
send("login\r", 4.0, "login (level1)")
# 不提交任何凭据，Ctrl+C 取消登录
send("\x03", 2.0, "ctrl-c cancel")
send("\r", 2.0, "prompt after cancel")

ser.close()
print("\nDone.")
