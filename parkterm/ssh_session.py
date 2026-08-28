"""
SSH 会话管理模块。

SSHSession 封装单个 SSH 连接的生命周期：
  - 连接建立（标准 SSHClient + raw socket 回退）
  - 交互式 shell 的实时流式输出（阻塞 recv，替代轮询）
  - 命令执行与输出捕获（run_command）
  - 二级权限提升（enter_level2）
  - 设备信息查询（get_device_info，数据驱动解析）
  - 文件操作专用连接（fs_client，与交互 shell 隔离）

文件传输的三级降级（SFTP→SCP→base64）在 file_manager.py 中实现，
本模块仅提供基础连接与 exec 通道。
"""
import logging
import re
import socket
import threading
import time

import paramiko

from . import config
from .legacy_ssh import make_raw_client, RawClient

logger = logging.getLogger("parkterm")

# ANSI 转义序列正则（用于清理命令输出）
_ANSI_CSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_ANSI_OSC = re.compile(r"\x1b\][^\x07]*\x07")
_ANSI_CHARSET = re.compile(r"\x1b[()][A-B]")
_ANSI_PRIVATE = re.compile(r"\x1b\[\?[0-9;]*[hl]")


def strip_ansi(text):
    """全面移除 ANSI 转义序列。"""
    text = _ANSI_CSI.sub("", text)
    text = _ANSI_OSC.sub("", text)
    text = _ANSI_CHARSET.sub("", text)
    text = _ANSI_PRIVATE.sub("", text)
    return text


class SSHSession:
    """单个 SSH 会话，管理一条交互式 shell 连接 + 一条文件操作专用连接。"""

    def __init__(self, host, port, username, password):
        self.host = host
        self.port = int(port)
        self.username = username
        self.password = password
        self.client = None
        self.channel = None
        self.connected = False
        self.lock = threading.Lock()
        self.last_activity = time.time()
        self.level2_entered = False
        self.socket_id = None
        self.suppress_output = False
        self.capturing = False
        # 二级权限进入时，通知正在进行的 device_info 中止
        self.device_info_abort = False
        # 文件操作专用连接
        self.fs_client = None
        self.fs_lock = threading.Lock()
        self.fs_connected = False
        # 流式输出线程引用
        self._stream_thread = None

    # ============================================================
    # 连接管理
    # ============================================================
    def connect(self):
        """建立 SSH 连接并调用交互式 shell。"""
        logger.info("Connecting to %s:%s as %s", self.host, self.port, self.username)
        self.client = self._make_client()
        self.channel = self.client.get_transport().open_session()
        self.channel.get_pty(config.TERM_TYPE,
                              width=config.TERM_DEFAULT_COLS,
                              height=config.TERM_DEFAULT_ROWS)
        self.channel.invoke_shell()

        # 启用 SSH keepalive，防止长时间空闲被设备/NAT 断开
        try:
            transport = self.client.get_transport()
            if transport and config.SSH_KEEPALIVE_INTERVAL > 0:
                transport.set_keepalive(config.SSH_KEEPALIVE_INTERVAL)
                logger.debug("SSH keepalive enabled: %ds", config.SSH_KEEPALIVE_INTERVAL)
        except Exception as e:
            logger.debug("Failed to set SSH keepalive: %s", e)

        self.connected = True
        logger.info("Connected to %s:%s, shell invoked", self.host, self.port)

    def _make_client(self):
        """创建并返回一个已连接的 SSH 客户端（标准或 raw socket 回退）。

        不打开交互式 shell，留给 connect() 处理。
        也被 get_fs_client() 复用于文件操作连接。
        """
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            logger.debug("Standard SSHClient.connect")
            client.connect(
                hostname=self.host, port=self.port,
                username=self.username, password=self.password,
                timeout=config.SSH_CONNECT_TIMEOUT,
                banner_timeout=config.SSH_BANNER_TIMEOUT,
                auth_timeout=config.SSH_AUTH_TIMEOUT,
                allow_agent=False, look_for_keys=False,
                disabled_algorithms={},
            )
            logger.info("Standard connect succeeded")
            return client
        except Exception as e:
            err_msg = str(e).lower()
            logger.warning("Standard connect failed: %s", e)
            if any(kw in err_msg for kw in ("banner", "too long", "indecipherable")):
                logger.info("Falling back to raw socket pre-read method")
                return make_raw_client(
                    self.host, self.port, self.username, self.password,
                    connect_timeout=config.SSH_CONNECT_TIMEOUT,
                    banner_timeout=config.RAW_SOCKET_TIMEOUT,
                    auth_timeout=config.SSH_AUTH_TIMEOUT,
                )
            raise

    # ============================================================
    # 流式输出（轮询 recv_ready，避免与 run_command 竞态）
    # ============================================================
    def start_streaming(self, sid, socketio):
        """启动后台线程，从 channel 读取输出并通过 SocketIO 实时推送。

        使用轮询 recv_ready() 而非阻塞 recv()：阻塞 recv 在 capturing 切换时
        会产生竞态（_reader 阻塞在 recv 上时，run_command 设置 capturing=True
        并读取 channel，设备输出可能被 _reader 先读走）。轮询模式下
        capturing=True 时线程直接 sleep，绝不触碰 channel，无竞态。
        """
        self.socket_id = sid

        def _reader():
            while self.connected:
                try:
                    # run_command 活跃期间不触碰 channel
                    if self.capturing:
                        time.sleep(0.05)
                        continue
                    if self.channel and self.channel.recv_ready():
                        chunk = self.channel.recv(4096)
                        if chunk:
                            if not self.suppress_output:
                                data = chunk.decode("utf-8", errors="replace")
                                socketio.emit("term-data", data, room=sid)
                        else:
                            self.connected = False
                            try:
                                socketio.emit("term-disconnect", room=sid)
                            except Exception:
                                pass
                            break
                    else:
                        time.sleep(0.02)
                except Exception:
                    logger.exception("Streaming reader error")
                    self.connected = False
                    try:
                        socketio.emit("term-disconnect", room=sid)
                    except Exception:
                        pass
                    break

        self._stream_thread = threading.Thread(target=_reader, daemon=True)
        self._stream_thread.start()

    # ============================================================
    # 输入 / 调整大小
    # ============================================================
    def send(self, data):
        if not self.connected or not self.channel:
            raise RuntimeError("未连接")
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.channel.sendall(data)
        self.last_activity = time.time()

    def resize(self, cols, rows):
        if self.channel and self.connected:
            try:
                self.channel.resize_pty(cols, rows)
            except Exception:
                pass

    def close(self):
        self.connected = False
        for attr in ("channel", "client", "fs_client"):
            obj = getattr(self, attr, None)
            if obj:
                try:
                    obj.close()
                except Exception:
                    pass
        self.fs_connected = False
        self.fs_client = None

    # ============================================================
    # 命令执行
    # ============================================================
    def _drain_channel(self):
        """读取并丢弃 channel 中待处理的输出。"""
        while self.channel and self.channel.recv_ready():
            self.channel.recv(65536)
            time.sleep(0.01)

    def run_command(self, cmd, timeout=None):
        """通过交互式 shell 发送命令并捕获输出。

        线程安全：使用 self.lock 防止并发访问 channel。
        检测 "Not Support Cmd Yet!" 立即返回，不等待超时。
        """
        if timeout is None:
            timeout = config.CMD_DEFAULT_TIMEOUT
        with self.lock:
            self.capturing = True
            self.suppress_output = True
            time.sleep(config.CMD_DRAIN_DELAY)
            self._drain_channel()
            try:
                self.send(cmd + "\n")
                buf = b""
                t0 = time.time()
                while time.time() - t0 < timeout:
                    if self.channel and self.channel.recv_ready():
                        chunk = self.channel.recv(65536)
                        if chunk:
                            buf += chunk
                            text_check = buf.decode("utf-8", errors="replace")
                            if "Not Support Cmd Yet!" in text_check:
                                break
                            if b"\n" in buf:
                                last_line = buf.rstrip().split(b"\n")[-1].strip()
                                if last_line.endswith(b"#") or last_line.endswith(b"$"):
                                    break
                    time.sleep(0.05)
                time.sleep(config.CMD_POST_WAIT)
                if self.channel and self.channel.recv_ready():
                    buf += self.channel.recv(65536)

                text = buf.decode("utf-8", errors="replace")
                logger.debug("run_command '%s' raw output: %r", cmd, text[:500])
                result = self._filter_command_output(cmd, text)
                logger.debug("run_command '%s' filtered: %r", cmd, result[:300])
                return result
            finally:
                self.capturing = False
                self.suppress_output = False

    def run_command_exec(self, cmd, timeout=None):
        """使用独立的 SSH exec channel 执行命令，不经过交互式 shell。

        与 run_command 的区别：
        - 创建独立的 exec channel，不与终端共享 shell channel
        - 不设置 capturing，不阻塞终端输出
        - 适用于设备信息查询等后台命令
        - 若设备不支持并发 channel 或 exec 失败，返回空字符串，调用方回退到 run_command
        """
        if timeout is None:
            timeout = config.CMD_DEVICE_INFO_TIMEOUT
        if not self.connected or not self.client:
            return ""
        try:
            transport = self.client.get_transport()
            if not transport or not transport.is_active():
                return ""
            # 创建独立的 exec channel
            chan = transport.open_session()
            try:
                chan.settimeout(timeout)
                chan.exec_command(cmd)
                buf = b""
                t0 = time.time()
                while time.time() - t0 < timeout:
                    if chan.recv_ready():
                        chunk = chan.recv(65536)
                        if chunk:
                            buf += chunk
                        else:
                            break
                    elif chan.exit_status_ready():
                        # 读取剩余输出
                        while chan.recv_ready():
                            chunk = chan.recv(65536)
                            if chunk:
                                buf += chunk
                        break
                    else:
                        time.sleep(0.05)
                text = buf.decode("utf-8", errors="replace")
                logger.debug("run_command_exec '%s' output: %r", cmd, text[:300])
                return text
            finally:
                try:
                    chan.close()
                except Exception:
                    pass
        except Exception as e:
            logger.debug("run_command_exec '%s' failed: %s", cmd, e)
            return ""

    @staticmethod
    def _filter_command_output(cmd, raw_text):
        """从命令原始输出中移除 ANSI 转义、回显行、提示符等，保留有效输出。"""
        text = strip_ansi(raw_text)
        text = text.replace("\r", "")
        lines = text.split("\n")
        out = []
        cmd_stripped = cmd.strip()
        for line in lines:
            s = line.strip()
            if not s:
                continue
            if s == cmd_stripped:
                continue
            if (s.endswith("#") or s.endswith("$")) and len(s) < 80:
                continue
            if s.startswith("login:") or s.startswith("password:"):
                continue
            out.append(line)
        return "\n".join(out)

    # ============================================================
    # 二级权限提升
    # ============================================================
    def enter_level2(self, password):
        """进入设备后台二级 shell。

        返回 {'ok': bool, 'output': str}。
        ok=True 表示二级权限已获取；ok=False 时 output 包含设备的拒绝信息。
        """
        self.device_info_abort = True
        with self.lock:
            self.capturing = True
            self.suppress_output = True
            time.sleep(config.CMD_DRAIN_DELAY)
            self._drain_channel()
            try:
                self.send("login\n")
                time.sleep(1.5)
                self.send(config.LEVEL2_USERNAME)
                time.sleep(0.1)
                for ctrl in config.LEVEL2_CTRL_SEQUENCE:
                    self.send(ctrl)
                    time.sleep(0.1)
                self.send("\n")
                time.sleep(1.5)
                if password:
                    self.send(password + "\n")
                time.sleep(1.0)

                buf = b""
                t0 = time.time()
                while time.time() - t0 < config.LEVEL2_RESPONSE_WAIT:
                    if self.channel and self.channel.recv_ready():
                        buf += self.channel.recv(65536)
                    else:
                        time.sleep(0.1)
                text = strip_ansi(buf.decode("utf-8", errors="replace"))
                text = text.replace("\r", "").strip()

                failed = any(m in text.lower() for m in config.LEVEL2_FAIL_MARKERS)
                self.level2_entered = not failed
                return {"ok": not failed, "output": text}
            finally:
                self.capturing = False
                self.suppress_output = False
                self.device_info_abort = False

    # ============================================================
    # 设备信息查询（数据驱动解析）
    # ============================================================
    def get_device_info(self):
        """查询设备名称、序列号、固件版本。

        使用 config.DEVICE_INFO_COMMANDS 定义的命令优先级，
        使用 config.DEVICE_INFO_FIELDS 定义的正则规则解析输出。
        """
        info = {"device_name": "", "serial_number": "", "pub": ""}

        if self.device_info_abort:
            logger.info("device_info skipped: level-2 entry in progress")
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

        def _try_run(cmd, timeout=None):
            if timeout is None:
                timeout = config.CMD_DEVICE_INFO_TIMEOUT
            # 优先使用独立 exec channel（不阻塞终端）
            out = self.run_command_exec(cmd, timeout=timeout)
            if out:
                return out
            # exec channel 不可用时回退到交互式 shell（会短暂阻塞终端）
            try:
                out = self.run_command(cmd, timeout=timeout)
                logger.debug("run_command fallback '%s': %r", cmd, out[:200])
                return out
            except Exception as e:
                logger.debug("run_command fallback '%s' failed: %s", cmd, e)
            return ""

        # 设备名 & SN
        for cmd in config.DEVICE_INFO_COMMANDS["manuinfo"]:
            if info["device_name"] and info["serial_number"]:
                break
            if self.device_info_abort:
                return info
            _parse_fields(_try_run(cmd))

        # 版本号
        for cmd in config.DEVICE_INFO_COMMANDS["version"]:
            if info["pub"] or self.device_info_abort:
                break
            _parse_fields(_try_run(cmd))

        logger.info("Device info result: %s", info)
        return info

    # ============================================================
    # 文件操作专用连接
    # ============================================================
    def get_fs_client(self):
        """返回文件操作专用客户端。

        优先使用独立的第二条 SSH 连接（避免与交互 shell 共享 transport 导致挂死）。
        若第二条连接无法建立（设备限制并发会话数），回退到主连接。
        """
        with self.fs_lock:
            if self.fs_connected and self.fs_client is not None:
                try:
                    t = self.fs_client.get_transport()
                    if t is None or not t.is_active():
                        self.fs_client = None
                        self.fs_connected = False
                    else:
                        return self.fs_client
                except Exception:
                    self.fs_client = None
                    self.fs_connected = False
            try:
                logger.info("Opening dedicated file-operation SSH connection")
                client = self._make_client()
                self.fs_client = client
                self.fs_connected = True
                return client
            except Exception as e:
                logger.warning("Dedicated FS connection failed (%s); falling back to main connection", e)
                return self.client

    def get_sftp(self):
        if not self.connected or not self.client:
            raise RuntimeError("未连接")
        client = self.get_fs_client()
        return client.open_sftp()

    # ============================================================
    # exec 通道（用于文件操作、抓包等非交互式命令）
    # ============================================================
    def cmd_transport(self):
        """返回用于 exec / SCP / base64 操作的 transport。

        故意使用主连接（始终存活），而非专用 SFTP 连接——
        后者可能在设备拒绝 SFTP 子系统后处于关闭状态。
        """
        if self.client:
            t = self.client.get_transport()
            if t is not None and t.is_active():
                return t
        return None

    def exec_capture(self, cmd, timeout=None):
        """在主连接上执行命令，返回 (stdout_text, stderr_text)。"""
        if timeout is None:
            timeout = config.FILE_EXEC_TIMEOUT
        transport = self.cmd_transport()
        if not transport:
            raise RuntimeError("无可用文件连接")
        chan = transport.open_session()
        chan.settimeout(timeout)
        try:
            chan.exec_command(cmd)
            stdout = b""
            while True:
                try:
                    chunk = chan.recv(65536)
                except Exception:
                    break
                if not chunk:
                    break
                stdout += chunk
            stderr = b""
            try:
                while chan.recv_stderr_ready():
                    stderr += chan.recv_stderr(65536)
            except Exception:
                pass
            return (
                stdout.decode("utf-8", errors="replace"),
                stderr.decode("utf-8", errors="replace"),
            )
        finally:
            try:
                chan.close()
            except Exception:
                pass

    def exec_run(self, cmd, timeout=None):
        """通过 exec 通道执行命令，返回 True/False 表示是否成功执行。"""
        if timeout is None:
            timeout = config.FILE_EXEC_RUN_TIMEOUT
        try:
            transport = self.cmd_transport()
            if not transport:
                return False
            chan = transport.open_session()
            chan.settimeout(timeout)
            chan.exec_command(cmd)
            chan.close()
            logger.debug("_exec_run: '%s' executed via exec channel", cmd)
            return True
        except Exception as e:
            logger.debug("_exec_run: exec channel failed (%s)", e)
            return False
