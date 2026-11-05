"""
串口会话管理模块。

SerialSession 封装单个串口连接的生命周期：
  - 串口打开（pyserial）
  - 交互式流式输出（后台线程轮询读取，通过 SocketIO 推送）
  - 输入发送（write to serial port）
  - 连接关闭

与 SSHSession 共享关键接口（connected / send / start_streaming / close / socket_id / last_activity），
便于 SessionStore 统一管理。串口连接不支持文件管理、抓包、二级权限。
"""
import logging
import threading
import time

logger = logging.getLogger("parkterm")

try:
    import serial
    _HAS_SERIAL = True
except ImportError:
    _HAS_SERIAL = False
    logger.warning("pyserial not installed, serial connections will be unavailable")


# 串口参数校验映射
_PARITY_MAP = {"N": "N", "E": "E", "O": "O", "None": "N", "Even": "E", "Odd": "O"}
_FLOW_MAP = {"N": None, "None": None, "RTS/CTS": "rtscts", "XON/XOFF": "xonxoff"}
_STOPBITS_MAP = {"1": 1, "1.5": 1.5, "2": 2}


class SerialSession:
    """单个串口会话，管理一条串口连接。"""

    def __init__(self, port, baudrate=115200, databits=8, parity="N",
                 stopbits="1", flowcontrol="N"):
        self.port_name = port
        self.baudrate = int(baudrate)
        self.databits = int(databits)
        self.parity = _PARITY_MAP.get(str(parity), "N")
        self.stopbits = _STOPBITS_MAP.get(str(stopbits), 1)
        self.flowcontrol = _FLOW_MAP.get(str(flowcontrol))
        self.ser = None
        self.connected = False
        self.socket_id = None
        self.last_activity = time.time()
        self.lock = threading.Lock()
        self._stream_thread = None
        # 兼容 SSHSession 接口：文件管理器/抓包管理器占位
        self.file_manager = None
        self.capture_manager = None
        self.level2_entered = False
        self.suppress_output = False
        self._capture_buf = []      # suppress_output 期间捕获的输出
        self.server_type = ""       # 连接时选择的设备类型（同 SSH，用于二级判断）

    def connect(self):
        """打开串口连接。"""
        if not _HAS_SERIAL:
            raise RuntimeError("串口功能不可用：未安装 pyserial 库")
        kwargs = dict(
            port=self.port_name,
            baudrate=self.baudrate,
            bytesize=self.databits,
            parity=self.parity,
            stopbits=self.stopbits,
            timeout=0.05,          # 非阻塞读取超时
            write_timeout=2.0,
        )
        if self.flowcontrol:
            kwargs[self.flowcontrol] = True
        logger.info("Opening serial port %s @ %d baud", self.port_name, self.baudrate)
        self.ser = serial.Serial(**kwargs)
        self.connected = True
        logger.info("Serial port %s opened", self.port_name)

    # ============================================================
    # 流式输出（后台线程轮询读取串口数据）
    # ============================================================
    def start_streaming(self, sid, socketio):
        """启动后台线程，从串口读取输出并通过 SocketIO 实时推送。

        重连安全：每次调用设置新 socket_id，旧 reader 线程检测到 sid 不匹配后自动退出。
        """
        self.socket_id = sid

        def _reader():
            while self.connected and self.ser:
                try:
                    # sid 不匹配说明已重连，旧线程自动退出
                    if self.socket_id != sid:
                        logger.debug("Old serial reader thread exiting (socket reassociated)")
                        break

                    if self.ser.in_waiting > 0:
                        chunk = self.ser.read(self.ser.in_waiting)
                        if chunk:
                            data = chunk.decode("utf-8", errors="replace")
                            if self.suppress_output:
                                # 二级登录等内部交互期间：捕获输出但不推送到终端
                                self._capture_buf.append(data)
                            else:
                                socketio.emit("term-data", data, room=sid)
                        else:
                            # 读取返回空可能意味着端口已关闭
                            time.sleep(0.02)
                    else:
                        time.sleep(0.02)
                except Exception:
                    logger.exception("Serial streaming reader error")
                    self.connected = False
                    try:
                        socketio.emit("term-disconnect", room=sid)
                    except Exception:
                        pass
                    break

        self._stream_thread = threading.Thread(target=_reader, daemon=True)
        self._stream_thread.start()

    def detach_socket(self):
        """Socket.IO 断连时调用：解除 socket 关联但保持串口连接活跃。"""
        self.socket_id = None
        logger.debug("Serial socket detached, connection kept alive")

    # ============================================================
    # 输入 / 调整大小
    # ============================================================
    def send(self, data):
        """向串口写入数据。"""
        if not self.connected or not self.ser:
            raise RuntimeError("串口未连接")
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.ser.write(data)
        self.last_activity = time.time()

    def resize(self, cols, rows):
        """串口不支持 PTY resize，空操作。"""
        pass

    def close(self):
        """关闭串口连接。"""
        self.connected = False
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None

    # ============================================================
    # 兼容接口（串口不支持的功能，返回安全默认值）
    # ============================================================
    def run_command(self, cmd, timeout=None):
        return ""

    def run_command_exec(self, cmd, timeout=None):
        return ""

    def get_device_info(self):
        return {"device_name": "", "serial_number": "", "pub": ""}

    # ============================================================
    # 串口读取辅助（供二级登录等内部交互使用）
    # ============================================================
    def _start_capture(self):
        """开始捕获模式：流式线程暂停推送，数据写入 _capture_buf。"""
        with self.lock:
            self._capture_buf = []
            self.suppress_output = True

    def _stop_capture(self):
        """结束捕获模式，返回捕获到的输出。"""
        with self.lock:
            self.suppress_output = False
            out = "".join(self._capture_buf)
            self._capture_buf = []
            return out

    def _read_until_marker(self, markers, timeout=3.0):
        """等待串口输出中出现任一标记，返回 (buffer, found)。"""
        buf = ""
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                n = self.ser.in_waiting
            except Exception:
                return buf, False
            if n:
                try:
                    buf += self.ser.read(n).decode("utf-8", errors="replace")
                except Exception:
                    pass
                for m in markers:
                    if m in buf:
                        return buf, True
            else:
                time.sleep(0.05)
        return buf, False

    # ============================================================
    # 二级登录（串口）
    # ============================================================
    def enter_level2(self, password):
        """串口下进入设备二级 shell。

        与 SSH 相同的交互序列：login → zhimakaimen → Ctrl+Q → Ctrl+W → 密码。
        串口下无法预读版本号区分新旧流程，采用自适应：
        login 后若直接出现密码提示则按旧版流程（直接发密码），
        出现 Username: 则走标准流程。
        捕获期间设备输出不推送终端，结束时整体返回给前端展示。
        """
        if not self.connected or not self.ser:
            return {"ok": False, "output": "串口未连接"}

        self._start_capture()
        try:
            self.send("login\n")
            buf, _ = self._read_until_marker(
                ["Username:", "Passwd:", "Password:", "password:", "passwd:"], 4.0)

            if any(m in buf for m in ("Passwd:", "Password:", "password:", "passwd:")):
                # 旧版流程：login 后直接输入二级密码
                logger.info("Serial level2: legacy flow (direct password)")
                if password:
                    self.send(password + "\n")
            else:
                # 标准流程：zhimakaimen → Ctrl+Q → Ctrl+W → 密码
                logger.info("Serial level2: standard flow")
                self.send("zhimakaimen\n")
                time.sleep(0.4)
                self._read_until_marker(["\n"], 0.3)
                self.send("\x11")   # Ctrl+Q
                time.sleep(0.2)
                self.send("\x17")   # Ctrl+W
                self._read_until_marker(
                    ["Passwd:", "Password:", "password:", "passwd:"], 3.0)
                if password:
                    self.send(password + "\n")

            # 等待设备响应
            time.sleep(0.3)
            self._read_until_marker(["\n"], 2.0)
            output = self._stop_capture()

            # 根据输出判断成功/失败
            failed_markers = ("incorrect", "Incorrect", "wrong", "Wrong",
                              "denied", "Denied", "失败", "错误", "Login failed")
            ok = not any(m in output for m in failed_markers)
            if ok:
                self.level2_entered = True
            logger.info("Serial level2 result: ok=%s output_len=%d", ok, len(output))
            return {"ok": ok, "output": output}
        except Exception as e:
            self._stop_capture()
            logger.error("Serial level2 error: %s", e)
            return {"ok": False, "output": str(e)}
