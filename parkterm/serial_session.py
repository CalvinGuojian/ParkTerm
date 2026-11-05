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

    def _read_available(self, timeout=2.0, quiet=0.6):
        """读取串口输出直到静默（quiet 秒无新数据）或超时，返回文本。"""
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
                    buf += self.ser.read(n).decode("utf-8", errors="replace")
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
    # 二级登录（串口）
    # ============================================================
    def enter_level2(self, password):
        """串口下进入二级。

        实机调试结论（COM3 实测）：
        - 串口控制台的 login 为 BusyBox 登录（/bin/login → /bin/busybox，
          提示符 login:/Password:），设备的二级登录魔数（Username:/zhimakaimen/
          Ctrl+Q/Ctrl+W）内嵌于 SSH 会话的自定义受限 shell 中，文件系统中
          不存在对应程序——串口下无法也无需复现 SSH 的二级登录流程。
        - 串口控制台本身就是无限制的 root shell（ps/ls/grep 等均可直接执行，
          而 SSH 一级环境会返回 "Not Support Cmd Yet!"），权限等同/超过
          SSH 的二级环境。

        因此串口下进入二级不做任何设备交互，直接置为二级状态。
        """
        if not self.connected:
            return {"ok": False, "output": "串口未连接"}
        logger.info("Serial level2: serial console is already a full-privilege root shell, "
                    "marking level2 without device interaction")
        self.level2_entered = True
        return {"ok": True,
                "output": "串口控制台已具备最高权限（root），直接进入二级环境"}

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
                # 静默检测读取：1 秒无新数据即认为输出结束（上限 5 秒）
                out = self._read_available(timeout=5.0, quiet=1.0)
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
