"""
ParkTerm 入口模块。

负责：
  - 日志系统初始化
  - Legacy SSH 算法启用
  - Werkzeug 启动横幅抑制
  - 端口自动探测
  - 控制台启动动画
  - 服务启动与浏览器自动打开
  - 会话清理线程启动
"""
import io
import logging
import logging.handlers
import socket
import sys
import threading
import time
import webbrowser
import warnings

import paramiko

from . import config
from .legacy_ssh import enable_legacy_ssh_algorithms
from .server import app, socketio, store

logger = logging.getLogger("parkterm")


# ============================================================
# 日志初始化
# ============================================================
def setup_logging():
    """初始化日志系统：RotatingFileHandler + 抑制库日志噪音。"""
    log_path = config.get_log_path()
    handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=config.LOG_MAX_BYTES,
        backupCount=config.LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(config.LOG_FORMAT, datefmt=config.LOG_DATE_FORMAT))

    root = logging.getLogger("parkterm")
    root.setLevel(logging.DEBUG)
    # 避免重复添加 handler（多次调用时）
    if not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers):
        root.addHandler(handler)

    # 抑制库日志噪音
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    logging.getLogger("socketio").setLevel(logging.ERROR)
    logging.getLogger("engineio").setLevel(logging.ERROR)
    logging.getLogger("paramiko").setLevel(logging.WARNING)

    warnings.filterwarnings("ignore")
    return log_path


# ============================================================
# Legacy SSH 算法启用
# ============================================================
def init_legacy_ssh():
    try:
        enable_legacy_ssh_algorithms()
        logger.info("已启用兼容老式SSH算法(ssh-rsa/ssh-dss等)以支持老网关设备")
    except Exception as e:
        logger.warning("启用兼容SSH算法失败: %s", e)


# ============================================================
# Werkzeug 横幅抑制
# ============================================================
class _BannerStripper(io.TextIOBase):
    """包装 sys.stdout/stderr，丢弃包含 Werkzeug 启动横幅的完整行。"""

    _TARGETS = ("Serving Flask app", "Debug mode")

    def __init__(self, stream):
        self._stream = stream
        self._buf = ""

    def write(self, text):
        self._buf += text
        out = ""
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if any(t in line for t in self._TARGETS):
                continue
            out += line + "\n"
        if self._buf:
            if any(t in self._buf for t in self._TARGETS):
                pass
            else:
                out += self._buf
                self._buf = ""
        if out:
            self._stream.write(out)
        return len(text)

    def flush(self):
        if self._buf and not any(t in self._buf for t in self._TARGETS):
            self._stream.write(self._buf)
            self._buf = ""
        self._stream.flush()


def suppress_werkzeug_banner():
    """抑制 Werkzeug 启动横幅（"Serving Flask app" / "Debug mode"）。"""
    if sys.stdout is not None and not isinstance(sys.stdout, _BannerStripper):
        sys.stdout = _BannerStripper(sys.stdout)
    if sys.stderr is not None and not isinstance(sys.stderr, _BannerStripper):
        sys.stderr = _BannerStripper(sys.stderr)

    # belt-and-suspenders：logging filter
    wz = logging.getLogger("werkzeug")

    class _BannerFilter(logging.Filter):
        def filter(self, record):
            msg = record.getMessage()
            return not any(t in msg for t in _BannerStripper._TARGETS)

    wz.addFilter(_BannerFilter())


# ============================================================
# 端口探测
# ============================================================
def find_free_port(start=None, end=None):
    if start is None:
        start = config.PORT_RANGE_START
    if end is None:
        end = config.PORT_RANGE_END
    for port in range(start, end):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((config.BIND_HOST, port))
                return port
            except OSError:
                continue
    return start


def wait_port(port, timeout=None):
    if timeout is None:
        timeout = config.SERVER_START_TIMEOUT
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex((config.BIND_HOST, port)) == 0:
                return True
        time.sleep(0.1)
    return False


# ============================================================
# 主入口
# ============================================================
def main():
    suppress_werkzeug_banner()
    log_path = setup_logging()
    init_legacy_ssh()

    port = find_free_port()
    url = f"http://{config.BIND_HOST}:{port}"

    # 启动会话清理线程
    store.start_cleanup_thread()

    # 控制台启动动画
    stop_anim = threading.Event()

    def animate():
        i = 0
        try:
            while not stop_anim.is_set():
                dots = "." * (i % 4)
                sys.stdout.write("\rParkTerm启动中" + dots + "    ")
                sys.stdout.flush()
                time.sleep(0.4)
                i += 1
        except Exception:
            pass

    def run_server():
        socketio.run(
            app,
            host=config.BIND_HOST,
            port=port,
            debug=False,
            allow_unsafe_werkzeug=True,
            use_reloader=False,
        )

    anim_t = threading.Thread(target=animate, daemon=True)
    anim_t.start()
    srv_t = threading.Thread(target=run_server, daemon=True)
    srv_t.start()

    ok = wait_port(port)
    stop_anim.set()
    anim_t.join(timeout=1)

    # 清除动画行
    sys.stdout.write("\r" + " " * 40 + "\r")
    sys.stdout.flush()

    if ok:
        print(f"ParkTerm 已启动  (版本 {config.VERSION})", flush=True)
        print(f"访问地址: {url}", flush=True)
        print(f"日志文件: {log_path}", flush=True)
        print("请勿关闭此窗口。按 Ctrl+C 退出。", flush=True)
    else:
        print("警告：Web 服务未能在预期时间内启动，请查看日志。")

    logger.info("ParkTerm %s starting on %s", config.VERSION, url)
    logger.info("Log file: %s", log_path)
    logger.info("Python: %s", sys.version)
    logger.info("Paramiko: %s", paramiko.__version__)

    # 服务就绪后自动打开浏览器
    try:
        webbrowser.open(url)
    except Exception:
        pass

    srv_t.join()
