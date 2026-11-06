"""
串口会话管理模块。

SerialSession 封装单个串口连接的生命周期：
  - 串口打开（pyserial）
  - 交互式流式输出（后台线程轮询读取，通过 SocketIO 推送）
  - 输入发送（write to serial port）
  - 连接关闭
  - 二级登录（enter_level2）：交互序列与 SSH 完全一致，独占串口读取
  - 设备信息查询（get_device_info）：独占读取执行 manuinfo/version 命令并解析

与 SSHSession 共享关键接口（connected / send / start_streaming / close / socket_id / last_activity），
便于 SessionStore 统一管理。串口连接不支持文件管理、抓包。
"""
import logging
import re
import threading
import time

from . import config

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

# 二级登录提示符变体
# 注意：串口控制台下 login 命令提示符为 "login:"（BusyBox 风格），
# 而 SSH PTY 下为 "Username:"，两者都视为用户名提示
_USERNAME_MARKERS = ["Username:", "username:", "Login:", "login:"]
_PASSWD_MARKERS = ["Passwd:", "Password:", "password:", "passwd:"]
_LOGIN_PROMPT_MARKERS = _USERNAME_MARKERS + _PASSWD_MARKERS


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
        self.server_type = ""       # 连接时选择的设备类型（同 SSH，用于二级判断/设备信息）
        self.device_version = ""
        # 独占读取：置位时流式线程暂停读取，由 enter_level2/get_device_info 独占读取串口
        # （解决双线程抢读导致二级登录收不到提示符的问题）
        self._exclusive_read = threading.Event()
        self._exclusive_owner = False   # 是否正处于独占读取中（close 时等待其退出）
        self._abort = False             # close 时通知独占读取循环提前退出

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
        self._abort = False
        logger.info("Serial port %s opened", self.port_name)

    # ============================================================
    # 流式输出（后台线程轮询读取串口数据）
    # ============================================================
    def start_streaming(self, sid, socketio):
        """启动后台线程，从串口读取输出并通过 SocketIO 实时推送。

        重连安全：每次调用设置新 socket_id，旧 reader 线程检测到 sid 不匹配后自动退出。
        """
        self.socket_id = sid
        self._socketio = socketio

        def _reader():
            while self.connected and self.ser:
                try:
                    # 独占读取期间暂停（enter_level2 / get_device_info 正在与设备交互）
                    if self._exclusive_read.is_set():
                        time.sleep(0.02)
                        continue
                    # sid 不匹配说明已重连，旧线程自动退出
                    if self.socket_id != sid:
                        logger.debug("Old serial reader thread exiting (socket reassociated)")
                        break

                    if self.ser.in_waiting > 0:
                        chunk = self.ser.read(self.ser.in_waiting)
                        if chunk:
                            data = chunk.decode("utf-8", errors="replace")
                            socketio.emit("term-data", data, room=sid)
                        else:
                            # 读取返回空可能意味着端口已关闭
                            time.sleep(0.02)
                    else:
                        time.sleep(0.02)
                except Exception:
                    if not self.connected:
                        break
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
        """向串口写入数据。

        串口设备期望回车符 CR（\r）：用户在终端手动敲回车时 xterm.js 发送 \r，
        因此统一将 \n 转换为 \r（多行粘贴文本同样适用）。
        """
        if not self.connected or not self.ser:
            raise RuntimeError("串口未连接")
        if isinstance(data, str):
            data = data.replace("\r\n", "\r").replace("\n", "\r")
            data = data.encode("utf-8")
        self.ser.write(data)
        self.last_activity = time.time()

    def resize(self, cols, rows):
        """串口不支持 PTY resize，空操作。"""
        pass

    def close(self):
        """关闭串口连接。

        若独占读取正在进行（二级登录/设备信息查询），先通知其提前退出并等待，
        避免 Windows 下串口句柄被占用导致端口未释放、重连失败。
        """
        self.connected = False
        self._abort = True
        self._exclusive_read.set()
        # 等待独占读取退出（最多 3 秒）
        t0 = time.time()
        while self._exclusive_owner and time.time() - t0 < 3.0:
            time.sleep(0.05)
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None
        logger.info("Serial port %s closed", self.port_name)

    # ============================================================
    # 兼容接口（串口不支持的功能，返回安全默认值）
    # ============================================================
    def run_command(self, cmd, timeout=None):
        return ""

    def run_command_exec(self, cmd, timeout=None):
        return ""

    # ============================================================
    # 独占读取辅助（二级登录 / 设备信息查询使用）
    # ============================================================
    def _drain(self, quiet=0.15):
        """排空串口接收缓冲（丢弃残留数据）。"""
        t0 = time.time()
        while time.time() - t0 < quiet:
            if self._abort or not self.connected:
                return
            try:
                n = self.ser.in_waiting
                if n:
                    self.ser.read(n)
                    t0 = time.time()
                else:
                    time.sleep(0.03)
            except Exception:
                break

    def _read_until_marker(self, markers, timeout=3.0, quiet=1.0):
        """等待串口输出中出现标记或静默，返回 (buffer, found)。

        markers 可为字符串或列表；quiet 秒无新数据则提前结束。
        """
        if isinstance(markers, str):
            markers = [markers]
        buf = ""
        t0 = time.time()
        last_data = time.time()
        while time.time() - t0 < timeout:
            if self._abort or not self.connected:
                return buf, False
            try:
                n = self.ser.in_waiting
            except Exception:
                return buf, False
            if n:
                try:
                    buf += self.ser.read(n).decode("utf-8", errors="replace")
                except Exception:
                    pass
                last_data = time.time()
                for m in markers:
                    if m in buf:
                        return buf, True
            elif time.time() - last_data > quiet:
                break
            else:
                time.sleep(0.03)
        return buf, False

    def _read_available(self, timeout=2.0, quiet=0.6, on_data=None):
        """读取串口输出直到静默（quiet 秒无新数据）或超时，返回文本。

        on_data：每读到一块数据时的回调（用于独占读取期间实时推送终端）。
        """
        buf = ""
        t0 = time.time()
        last_data = time.time()
        while time.time() - t0 < timeout:
            if self._abort or not self.connected:
                break
            try:
                n = self.ser.in_waiting
            except Exception:
                break
            if n:
                try:
                    chunk = self.ser.read(n).decode("utf-8", errors="replace")
                except Exception:
                    chunk = ""
                if chunk:
                    buf += chunk
                    if on_data:
                        try:
                            on_data(chunk)
                        except Exception:
                            pass
                last_data = time.time()
            elif time.time() - last_data > quiet:
                break
            else:
                time.sleep(0.03)
        return buf

    # ============================================================
    # 版本判定（与 SSHSession._is_vmsb_legacy_version 逻辑一致）
    # ============================================================
    def _is_vmsb_legacy(self):
        """判断 VMS-B 是否为旧版本（.27.xx 及以前），旧版本二级进入方式不同。

        串口下 device_version 来自连接时的设备信息查询；未获取到时保守判定为旧版本。
        """
        if self.server_type != "vms-b":
            return False
        ver = (self.device_version or "").strip()
        if not ver:
            logger.warning("VMS-B serial version unknown, assuming legacy level2 flow")
            return True
        m = re.search(r'VMS-B\d+\.(\d+)\.', ver, re.IGNORECASE)
        if m:
            major = int(m.group(1))
            logger.info("VMS-B serial version=%s, major=%d, legacy=%s",
                        ver, major, major <= 27)
            return major <= 27
        logger.warning("VMS-B serial version=%s unparseable, assuming legacy", ver)
        return True

    # ============================================================
    # 二级登录（串口，流程与 SSHSession.enter_level2 一致）
    # ============================================================
    def enter_level2(self, password):
        """串口下进入设备二级 shell，流程与 SSH 一致。

        一级受限 shell 会拦截 login 命令并内置二级登录魔数
        （login → Username: → zhimakaimen → Ctrl+Q → Ctrl+W → Passwd: → 密码），
        与 SSH 相同。保留串口自适应处理：提示符变体、Ctrl 序列被 TTY 吞掉时
        的普通回车重试、以及提示符异常时的快速失败。
        """
        if not self.connected or not self.ser:
            return {"ok": False, "output": "串口未连接"}

        self._exclusive_read.set()
        self._exclusive_owner = True
        try:
            # 排空连接残留（与 SSH 的二次排空一致）
            self._drain(0.15)
            time.sleep(0.3)
            self._drain(0.1)

            buf = ""
            self.send("login\r")
            buf, prompt = self._wait_prompt(_LOGIN_PROMPT_MARKERS, 4.0)

            if prompt in _PASSWD_MARKERS:
                # 旧版流程：login 后直接等待密码（与 SSH legacy 一致）
                logger.info("Serial level2: legacy flow (direct password prompt)")
                if password:
                    self.send(password + "\r")
            elif prompt:
                # 用户名提示 → 标准流程（与 SSH 一致）
                logger.info("Serial level2: username prompt=%s, standard flow", prompt)
                self.send(config.LEVEL2_USERNAME)
                time.sleep(0.1)
                for ctrl in config.LEVEL2_CTRL_SEQUENCE:
                    self.send(ctrl)
                    time.sleep(0.1)
                self.send("\r")
                # 等待并让设备输出充分静默：吸收设备迟到的响应，
                # 避免残留的登录提示与后续输入交错
                buf2, prompt2 = self._wait_prompt(_LOGIN_PROMPT_MARKERS, 3.0, quiet=1.0)
                buf += buf2
                if prompt2 in _PASSWD_MARKERS:
                    if password:
                        self.send(password + "\r")
                elif prompt2 in _USERNAME_MARKERS:
                    # Ctrl 序列可能被串口 TTY 吞掉（canonical 模式）→ 在当前
                    # 用户名提示下用普通回车方式重试一次
                    logger.warning("Serial level2: ctrl seq ineffective, retrying plain Enter")
                    self.send(config.LEVEL2_USERNAME + "\r")
                    buf3, prompt3 = self._wait_prompt(_LOGIN_PROMPT_MARKERS, 3.0, quiet=1.0)
                    buf += buf3
                    if prompt3 in _PASSWD_MARKERS:
                        if password:
                            self.send(password + "\r")
                    else:
                        text = buf.replace("\r", "").strip()
                        return {"ok": False, "output": f"二级登录失败（未收到密码提示）: {text[-200:]}"}
                elif prompt2:
                    text = buf.replace("\r", "").strip()
                    return {"ok": False, "output": f"二级登录失败（未收到密码提示）: {text[-200:]}"}
                else:
                    # 无提示 → 探测回车后再识别一次
                    self.send("\r")
                    buf3, prompt3 = self._wait_prompt(_LOGIN_PROMPT_MARKERS, 2.5, quiet=1.0)
                    buf += buf3
                    if prompt3 in _PASSWD_MARKERS:
                        if password:
                            self.send(password + "\r")
                    elif prompt3 in _USERNAME_MARKERS:
                        self.send(config.LEVEL2_USERNAME + "\r")
                        buf4, prompt4 = self._wait_prompt(_LOGIN_PROMPT_MARKERS, 3.0, quiet=1.0)
                        buf += buf4
                        if prompt4 in _PASSWD_MARKERS:
                            if password:
                                self.send(password + "\r")
                        else:
                            text = buf.replace("\r", "").strip()
                            return {"ok": False, "output": f"二级登录失败（未收到密码提示）: {text[-200:]}"}
                    else:
                        text = buf.replace("\r", "").strip()
                        return {"ok": False, "output": f"二级登录失败: {text[-200:]}"}
            else:
                # 未出现任何提示：版本判 legacy 时直接发密码（与 SSH 旧版行为一致），否则失败
                if self._is_vmsb_legacy():
                    logger.warning("Serial level2: no prompt seen, sending password directly (legacy)")
                    if password:
                        self.send(password + "\r")
                else:
                    text = buf.replace("\r", "").strip()
                    return {"ok": False, "output": f"未收到登录提示: {text[:200]}"}

            # 读取登录结果（与 SSH 一致：检测到欢迎横幅提前结束）。
            # 窗口 4 秒并以静默收尾，确保迟到的失败信息也被捕获。
            t0 = time.time()
            while time.time() - t0 < 4.0:
                chunk = self._read_available(timeout=0.4, quiet=0.5)
                if chunk:
                    buf += chunk
                    if "Welcome to" in buf:
                        time.sleep(0.4)
                        buf += self._read_available(timeout=0.8, quiet=0.4)
                        break
                else:
                    time.sleep(0.03)

            text = buf.replace("\r", "").strip()
            failed = any(m in text.lower() for m in config.LEVEL2_FAIL_MARKERS)
            self.level2_entered = not failed
            logger.info("Serial level2 result: ok=%s output_len=%d", not failed, len(text))
            return {"ok": not failed, "output": text}
        except Exception as e:
            logger.error("Serial level2 error: %s", e)
            return {"ok": False, "output": str(e)}
        finally:
            self._exclusive_owner = False
            self._exclusive_read.clear()

    def _wait_prompt(self, prompts, timeout=3.0, quiet=0.6):
        """读取直到最后一行以某个提示符结尾，返回 (buffer, matched_prompt)。

        只匹配最后一行（基于行尾判断），避免已提交输入的回显被误认为新提示符。
        quiet：无新数据多久后提前结束（值越大等待越充分，用于吸收设备迟到的响应）。
        """
        buf = ""
        t0 = time.time()
        last_data = time.time()
        while time.time() - t0 < timeout:
            if self._abort or not self.connected:
                return buf, ""
            try:
                n = self.ser.in_waiting
            except Exception:
                return buf, ""
            if n:
                try:
                    buf += self.ser.read(n).decode("utf-8", errors="replace")
                except Exception:
                    pass
                last_data = time.time()
                last = self._last_line(buf)
                for p in prompts:
                    if last.endswith(p):
                        return buf, p
            elif time.time() - last_data > quiet:
                break
            else:
                time.sleep(0.03)
        return buf, ""

    @staticmethod
    def _last_line(buf):
        lines = [l.strip() for l in buf.replace("\r", "\n").split("\n") if l.strip()]
        return lines[-1] if lines else ""

    # ============================================================
    # 设备信息查询（串口独占读取 + 数据驱动解析，与 SSHSession 一致）
    # ============================================================
    def get_device_info(self):
        """查询设备名称、序列号、固件版本。

        串口下没有独立 exec 通道，通过独占读取在交互 shell 中执行命令：
        发送 manuinfo_cmd / version_cmd，捕获输出并按 config.DEVICE_INFO_FIELDS 解析。
        期间流式线程暂停，终端无感。
        """
        info = {"device_name": "", "serial_number": "", "pub": ""}

        if not self.connected or not self.ser:
            return info

        st = config.SERVER_TYPES.get(getattr(self, "server_type", ""), {})
        if not st:
            return info

        def _parse_fields(text):
            for line in text.split("\n"):
                line_clean = line.strip()
                if not line_clean:
                    continue
                for field_key, rules in config.DEVICE_INFO_FIELDS:
                    if info[field_key]:
                        continue
                    for pattern, keyword in rules:
                        if keyword in line_clean:
                            m = re.search(pattern, line_clean, re.IGNORECASE)
                            if m:
                                info[field_key] = m.group(1).strip().rstrip("#$").strip()
                                break

        self._exclusive_read.set()
        self._exclusive_owner = True
        try:
            self._drain(0.15)
            for cmd_key in ("manuinfo_cmd", "version_cmd"):
                cmd = st.get(cmd_key)
                if not cmd:
                    continue
                logger.info("Serial device info: running '%s'", cmd)
                self.send(cmd + "\r")
                # 静默检测读取（实测输出 ~0.1s 到齐，0.5s 静默即结束）。
                # 后台静默执行（与 SSH exec 通道一致）：捕获输出不推送终端
                out = self._read_available(timeout=3.0, quiet=0.5)
                _parse_fields(out)
                self.send("\r")
                self._drain(0.15)
            self.device_version = info.get("pub", "")
            logger.info("Serial device info result: %s", info)
            return info
        except Exception as e:
            logger.error("Serial get_device_info error: %s", e)
            return info
        finally:
            self._exclusive_owner = False
            self._exclusive_read.clear()
