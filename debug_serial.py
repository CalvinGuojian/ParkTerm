"""串口调试：测量设备信息命令的输出与耗时"""
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


def send(data, wait=5.0, tag=""):
    t0 = time.time()
    ser.write(data if isinstance(data, bytes) else data.encode())
    out = read_for(wait)
    show(f"{tag} ({time.time()-t0:.2f}s)", out)
    return out


ser.reset_input_buffer()
send("\r", 2.0, "wake")
# manuinfo 命令（SSH 下由此解析型号/SN）
send("display device manuinfo\r", 6.0, "manuinfo")
# version 命令
send("update -v\r", 6.0, "update -v")

ser.close()
print("\nDone.")
