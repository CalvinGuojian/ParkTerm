"""
内置 TFTP 服务器模块（RFC 1350 简化实现）。

用于串口连接下的文件传输：PC 与设备之间通过设备端的 tftp 命令传输文件。
  - 设备下载（从 PC 拉取）: tftp -g -r <文件名> <PC的IP>
  - 设备上传（推送到 PC）: tftp -p -l <文件名> <PC的IP>

特点：
  - 仅支持 octet（二进制）模式
  - 每个 RRQ/WRQ 使用独立的随机 UDP 端口传输线程
  - 支持超时重传
  - 文件名安全过滤（禁止路径穿越）
"""
import logging
import os
import re
import socket
import struct
import threading
import time

logger = logging.getLogger("parkterm")

TFTP_PORT = 69
BLOCK_SIZE = 512
DEFAULT_TIMEOUT = 5.0
MAX_RETRIES = 6

# TFTP 操作码
OP_RRQ = 1
OP_WRQ = 2
OP_DATA = 3
OP_ACK = 4
OP_ERR = 5

# 安全文件名：允许字母数字/下划线/点/横杠/中文
_SAFE_NAME = re.compile(r"^[\w.\-\u4e00-\u9fff]+$")


def get_local_ips():
    """获取本机所有 IPv4 地址（排除回环），出口 IP 排在最前。"""
    ips = []
    out_ip = ""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            out_ip = s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and ip != "127.0.0.1":
                ips.append(ip)
    except Exception:
        pass
    if out_ip and out_ip != "127.0.0.1":
        if out_ip in ips:
            ips.remove(out_ip)
        ips.insert(0, out_ip)
    return ips or ["127.0.0.1"]


class _TransferHandler(threading.Thread):
    """单次 TFTP 传输（RRQ 或 WRQ），使用独立随机端口。"""

    def __init__(self, server, data, client_addr):
        super().__init__(daemon=True)
        self.server = server
        self.data = data
        self.client = client_addr

    def run(self):
        try:
            opcode = struct.unpack(">H", self.data[:2])[0]
            # 解析 filename / mode（以 \x00 分隔）
            parts = self.data[2:].split(b"\x00")
            if len(parts) < 2:
                self._send_error(2, "Illegal filename")
                return
            filename = parts[0].decode("utf-8", errors="replace")
            mode = parts[1].decode("ascii", errors="replace").lower()
            if not self._safe_filename(filename):
                self._send_error(2, "Illegal filename")
                return
            filepath = os.path.join(self.server.root_dir, filename)
            if opcode == OP_RRQ:
                logger.info("TFTP RRQ '%s' from %s (mode=%s)", filename, self.client[0], mode)
                self._handle_read(filepath)
            elif opcode == OP_WRQ:
                logger.info("TFTP WRQ '%s' from %s (mode=%s)", filename, self.client[0], mode)
                self._handle_write(filepath)
            else:
                self._send_error(4, "Illegal operation")
        except Exception:
            logger.exception("TFTP transfer error")

    @staticmethod
    def _safe_filename(name):
        if not name or len(name) > 255:
            return False
        if ".." in name or "/" in name or "\\" in name or ":" in name:
            return False
        return bool(_SAFE_NAME.match(name))

    def _make_sock(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("0.0.0.0", 0))
        sock.settimeout(DEFAULT_TIMEOUT)
        return sock

    def _send_error(self, code, msg):
        try:
            sock = self._make_sock()
            sock.sendto(struct.pack(">HH", OP_ERR, code) + msg.encode("utf-8") + b"\x00", self.client)
            sock.close()
        except Exception:
            pass

    # ------------------------------------------------------------
    # 读请求：PC → 设备（设备执行 tftp -g 拉取）
    # ------------------------------------------------------------
    def _handle_read(self, filepath):
        if not os.path.isfile(filepath):
            self._send_error(1, "File not found")
            return
        sock = self._make_sock()
        try:
            block = 1
            with open(filepath, "rb") as f:
                while True:
                    payload = f.read(BLOCK_SIZE)
                    pkt = struct.pack(">HH", OP_DATA, block) + payload
                    retries = 0
                    while retries < MAX_RETRIES:
                        sock.sendto(pkt, self.client)
                        try:
                            resp, addr = sock.recvfrom(516)
                            if addr != self.client:
                                continue
                            op, ack_block = struct.unpack(">HH", resp[:4])
                            if op == OP_ACK and ack_block == block:
                                break
                        except socket.timeout:
                            retries += 1
                            logger.warning("TFTP read ACK timeout (block %d, retry %d)", block, retries)
                    else:
                        logger.error("TFTP read aborted: no ACK for block %d", block)
                        return
                    if len(payload) < BLOCK_SIZE:
                        return
                    block = (block + 1) % 65536
                    if block == 0:
                        block = 1
        finally:
            sock.close()

    # ------------------------------------------------------------
    # 写请求：设备 → PC（设备执行 tftp -p 推送）
    # ------------------------------------------------------------
    def _handle_write(self, filepath):
        sock = self._make_sock()
        try:
            tmp_path = filepath + ".tftp_part"
            ack0 = struct.pack(">HH", OP_ACK, 0)
            sock.sendto(ack0, self.client)
            block = 1
            with open(tmp_path, "wb") as f:
                while True:
                    retries = 0
                    data_pkt = None
                    while retries < MAX_RETRIES:
                        try:
                            resp, addr = sock.recvfrom(BLOCK_SIZE + 4)
                            if addr != self.client:
                                continue
                            op, rx_block = struct.unpack(">HH", resp[:4])
                            if op == OP_ERR:
                                logger.error("TFTP write error from client: %s", resp[4:].split(b"\x00")[0])
                                return
                            if op == OP_DATA and rx_block == block:
                                data_pkt = resp
                                break
                            if op == OP_DATA and rx_block == block - 1:
                                # 上一个块的重复 DATA → 重发 ACK
                                sock.sendto(struct.pack(">HH", OP_ACK, rx_block), self.client)
                        except socket.timeout:
                            retries += 1
                            sock.sendto(struct.pack(">HH", OP_ACK, block - 1), self.client)
                            logger.warning("TFTP write DATA timeout (block %d, retry %d)", block, retries)
                    if data_pkt is None:
                        logger.error("TFTP write aborted: no DATA for block %d", block)
                        return
                    payload = data_pkt[4:]
                    f.write(payload)
                    sock.sendto(struct.pack(">HH", OP_ACK, block), self.client)
                    if len(payload) < BLOCK_SIZE:
                        break
                    block = (block + 1) % 65536
                    if block == 0:
                        block = 1
            # 传输完成，重命名为正式文件
            if os.path.exists(filepath):
                os.remove(filepath)
            os.rename(tmp_path, filepath)
            logger.info("TFTP write completed: %s (%d bytes)", filepath, os.path.getsize(filepath))
        finally:
            sock.close()
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass


class TFTPServer:
    """TFTP 服务器（全局单例使用）。"""

    def __init__(self):
        self.root_dir = ""
        self._sock = None
        self._thread = None
        self._running = False
        self._lock = threading.Lock()
        self.started_at = None

    @property
    def running(self):
        return self._running

    def start(self, root_dir):
        """启动 TFTP 服务器（已在运行则先更新根目录）。"""
        with self._lock:
            root_dir = os.path.abspath(root_dir)
            os.makedirs(root_dir, exist_ok=True)
            self.root_dir = root_dir
            if self._running:
                logger.info("TFTP server already running, root updated to %s", root_dir)
                return True
            try:
                self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self._sock.bind(("0.0.0.0", TFTP_PORT))
            except OSError as e:
                logger.error("TFTP bind port %d failed: %s", TFTP_PORT, e)
                self._sock = None
                raise RuntimeError(f"TFTP 端口 {TFTP_PORT} 绑定失败（可能被占用）: {e}")
            self._running = True
            self.started_at = time.time()
            self._thread = threading.Thread(target=self._serve, daemon=True)
            self._thread.start()
            logger.info("TFTP server started, root=%s", root_dir)
            return True

    def stop(self):
        with self._lock:
            self._running = False
            if self._sock:
                try:
                    self._sock.close()
                except Exception:
                    pass
                self._sock = None
            self.started_at = None
            logger.info("TFTP server stopped")

    def _serve(self):
        while self._running and self._sock:
            try:
                data, addr = self._sock.recvfrom(1024)
            except OSError:
                break
            except Exception:
                continue
            if not self._running:
                break
            opcode = struct.unpack(">H", data[:2])[0] if len(data) >= 2 else 0
            if opcode in (OP_RRQ, OP_WRQ):
                _TransferHandler(self, data, addr).start()
            else:
                logger.debug("TFTP ignore opcode %d from %s", opcode, addr)


# 全局单例
tftp_server = TFTPServer()
