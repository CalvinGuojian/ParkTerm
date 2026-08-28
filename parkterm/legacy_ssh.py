"""
Legacy SSH 兼容性模块。

许多嵌入式/工业 SSH 网关（如宇视 VMS-B）仅提供老式主机密钥算法
（ssh-rsa SHA-1、ssh-dss）以及旧的密钥交换/加密/MAC 算法。
现代 paramiko (>=3) 默认已移除这些算法，导致握手失败。
本模块在不削弱现代连接安全性的前提下，将老式算法追加为兜底选项。

同时提供原始 socket 预读 banner 的回退连接方式，用于处理
在 SSH banner 之前发送前置文本的非标设备。
"""
import logging
import socket as _socket

import paramiko

logger = logging.getLogger("parkterm")

# ============================================================
# 带预加载数据的缓冲 socket 包装器
# ============================================================
class BufferedSocket:
    """包装一个原始 socket，先消费预加载的字节，再从真实 socket 读取。

    用于 raw socket 回退：我们先从 socket 读取到 SSH banner 为止，
    把 banner 及之后的数据预加载，然后把包装后的 socket 交给 paramiko，
    这样 paramiko 看到的就是标准 SSH 协议流。
    """

    def __init__(self, raw_sock, preloaded):
        self._sock = raw_sock
        self._preloaded = preloaded

    def recv(self, n):
        if self._preloaded:
            data = self._preloaded[:n]
            self._preloaded = self._preloaded[n:]
            return data
        return self._sock.recv(n)

    def send(self, data):
        return self._sock.send(data)

    def sendall(self, data):
        return self._sock.sendall(data)

    def settimeout(self, t):
        self._sock.settimeout(t)

    def setblocking(self, b):
        self._sock.setblocking(b)

    def gettimeout(self):
        return self._sock.gettimeout()

    def close(self):
        self._sock.close()

    def fileno(self):
        return self._sock.fileno()

    def getpeername(self):
        return self._sock.getpeername()

    def __getattr__(self, name):
        return getattr(self._sock, name)


class RawClient:
    """基于 paramiko Transport 的轻量 SSH 客户端，用于 raw socket 回退。

    提供与 paramiko.SSHClient 兼容的最小接口：
    get_transport() / exec_command() / open_sftp() / close()
    """

    def __init__(self, transport):
        self._transport = transport

    def get_transport(self):
        return self._transport

    def exec_command(self, cmd, timeout=10):
        return self._transport.exec_command(cmd, timeout=timeout)

    def open_sftp(self):
        return self._transport.open_sftp_client()

    def close(self):
        self._transport.close()


# ============================================================
# Legacy 算法恢复
# ============================================================
def enable_legacy_ssh_algorithms():
    """将老式 SSH 算法追加到 paramiko 的可接受列表末尾。

    现代算法仍排在前面优先协商；仅当服务器不支持任何现代算法时才回退到老式算法。
    涵盖：主机密钥、签名哈希、密钥类注册、密钥交换、加密、MAC。
    """
    t = paramiko.transport.Transport

    # --- 主机密钥算法 ---
    for alg in ("ssh-rsa", "ssh-dss"):
        if alg not in t._preferred_keys:
            t._preferred_keys = t._preferred_keys + (alg,)

    # --- 签名哈希（paramiko >=3 移除了 SHA-1）---
    try:
        from cryptography.hazmat.primitives.hashes import SHA1
        if "ssh-rsa" not in paramiko.rsakey.RSAKey.HASHES:
            paramiko.rsakey.RSAKey.HASHES["ssh-rsa"] = SHA1
        _dss = getattr(paramiko, "dsskey", None)
        if _dss is not None and "ssh-dss" not in _dss.DSSKey.HASHES:
            _dss.DSSKey.HASHES["ssh-dss"] = SHA1
    except Exception as _he:
        logger.warning("无法恢复 SHA-1 签名支持: %s", _he)

    # --- 密钥类注册（paramiko >=3 移除了 ssh-rsa 的注册表项）---
    try:
        if "ssh-rsa" not in t._key_info:
            t._key_info["ssh-rsa"] = paramiko.RSAKey
    except Exception as _ke:
        logger.warning("无法恢复 ssh-rsa 密钥类注册: %s", _ke)

    # --- 密钥交换算法 ---
    for alg in (
        "diffie-hellman-group1-sha1",
        "diffie-hellman-group14-sha1",
        "diffie-hellman-group-exchange-sha1",
        "diffie-hellman-group-exchange-sha256",
    ):
        if alg not in t._preferred_kex:
            t._preferred_kex = t._preferred_kex + (alg,)

    # --- 加密算法 ---
    for alg in (
        "aes128-cbc", "aes192-cbc", "aes256-cbc", "3des-cbc",
        "blowfish-cbc", "cast128-cbc",
    ):
        if alg not in t._preferred_ciphers:
            t._preferred_ciphers = t._preferred_ciphers + (alg,)

    # --- MAC 算法 ---
    for alg in ("hmac-sha1", "hmac-sha1-96", "hmac-md5", "hmac-md5-96"):
        if alg not in t._preferred_macs:
            t._preferred_macs = t._preferred_macs + (alg,)


def make_raw_client(host, port, username, password,
                     connect_timeout=30, banner_timeout=60, auth_timeout=30):
    """通过原始 socket 连接，丢弃 banner 前的前置文本，然后构建 paramiko Transport。

    用于标准 SSHClient.connect() 因 banner 异常（前置文本、banner 过长等）失败的场景。
    返回 RawClient（可用于 SFTP 和 exec）。
    """
    logger.debug("Raw socket: connecting to %s:%s", host, port)
    sock = _socket.create_connection((host, port), timeout=connect_timeout)
    sock.settimeout(banner_timeout)

    # 读取数据直到找到 SSH banner 行（以 "SSH-" 开头）
    buf = b""
    banner_start = -1
    while len(buf) < 16384:
        try:
            chunk = sock.recv(1024)
        except _socket.timeout:
            logger.error("Raw socket: timeout reading banner, got %d bytes", len(buf))
            raise paramiko.SSHException(
                f"连接超时：服务器在端口 {port} 未发送 SSH 标识，"
                f"请确认该端口提供 SSH 服务"
            )
        if not chunk:
            logger.error("Raw socket: connection closed, got %d bytes", len(buf))
            raise paramiko.SSHException(
                f"连接断开：服务器在端口 {port} 关闭了连接，"
                f"请确认该端口提供 SSH 服务"
            )
        buf += chunk
        logger.debug("Raw socket: received %d bytes, total %d bytes", len(chunk), len(buf))
        idx = buf.find(b"SSH-")
        if idx >= 0:
            banner_start = idx
            logger.info("Raw socket: found SSH banner at offset %d", idx)
            logger.debug("Pre-banner data: %s",
                         buf[:idx].decode("utf-8", errors="replace")[:200])
            break

    if banner_start < 0:
        preview = buf[:200].decode("utf-8", errors="replace")
        logger.error("Raw socket: no SSH banner found in %d bytes. Data: %s", len(buf), preview)
        raise paramiko.SSHException(
            f"端口 {port} 似乎不是 SSH 服务。收到的数据：{preview}"
        )

    # 从 banner_start 开始是有效的 SSH 协议数据，前置文本丢弃。
    remaining = buf[banner_start:]
    wrapped = BufferedSocket(sock, remaining)
    logger.debug("Raw socket: creating Transport with buffered socket")

    transport = paramiko.Transport(wrapped)
    transport.banner_timeout = banner_timeout * 2
    transport.auth_timeout = auth_timeout
    transport.connect(username=username, password=password)
    logger.info("Raw socket: Transport connected successfully")
    return RawClient(transport)
