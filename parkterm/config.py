"""
ParkTerm 集中化配置模块。
所有版本号、超时、路径、端口范围、缓存时长等常量统一在此定义，
避免魔法数字散落在各模块中。发版时只需修改 VERSION 一处。
"""
import os
import sys

# ============================================================
# 版本信息（唯一来源，build.bat / spec / 启动横幅均引用此处）
# ============================================================
VERSION = "V2.0.12"
APP_NAME = "ParkTerm"

# ============================================================
# 网络与服务
# ============================================================
# 本地服务绑定地址（仅监听回环，不对外暴露）
BIND_HOST = "127.0.0.1"
# 自动探测端口范围
PORT_RANGE_START = 5000
PORT_RANGE_END = 5100
# 等待服务启动的超时（秒）
SERVER_START_TIMEOUT = 15

# ============================================================
# SSH 连接超时（秒）
# ============================================================
SSH_CONNECT_TIMEOUT = 30
SSH_BANNER_TIMEOUT = 60
SSH_AUTH_TIMEOUT = 30
# 原始 socket 预读 banner 的超时
RAW_SOCKET_TIMEOUT = 60
RAW_SOCKET_MAX_BUF = 16384
# SSH keepalive 间隔（秒），0 表示禁用
SSH_KEEPALIVE_INTERVAL = 30

# ============================================================
# 终端 PTY
# ============================================================
TERM_TYPE = "xterm-256color"
TERM_DEFAULT_COLS = 120
TERM_DEFAULT_ROWS = 40
# 流式读取缓冲区大小（字节）
STREAM_READ_CHUNK = 4096
# 命令执行缓冲区大小（字节）
CMD_READ_CHUNK = 65536
# 文件传输基础缓冲区大小（字节）
FILE_TRANSFER_CHUNK = 65536

# ============================================================
# 命令执行（run_command）
# ============================================================
CMD_DEFAULT_TIMEOUT = 8
CMD_DEVICE_INFO_TIMEOUT = 4
CMD_CHECK_LEVEL1_TIMEOUT = 5
# 发送命令后等待输出稳定的短暂延时（秒）
CMD_DRAIN_DELAY = 0.2
CMD_POST_WAIT = 0.3

# ============================================================
# 文件操作超时（秒）
# ============================================================
FILE_SFTP_LISTDIR_TIMEOUT = 15
FILE_SFTP_CHANNEL_TIMEOUT = 120
FILE_EXEC_TIMEOUT = 10
FILE_EXEC_RUN_TIMEOUT = 8
FILE_DELETE_TIMEOUT = 30
FILE_MKDIR_TIMEOUT = 30
# base64 上传大文件阈值（字节），超过则警告
BASE64_UPLOAD_WARN_SIZE = 30 * 1024 * 1024
BASE64_UPLOAD_CHUNK = 65536
BASE64_DOWNLOAD_TIMEOUT = 120

# ============================================================
# 抓包（tcpdump）
# ============================================================
# 抓包文件保存目录（远程SSH设备上的绝对路径）
CAPTURE_DIR = "/root/parkterm_captures"
# 最多保留抓包文件数
CAPTURE_MAX_FILES = 3
# 抓包列表缓存时长（秒）
CAPTURE_LIST_CACHE_TTL = 2.5
# 文件大小 / PID 存活缓存时长（秒）
CAPTURE_SIZE_CACHE_TTL = 2
CAPTURE_PID_CACHE_TTL = 5
# 前端轮询间隔（秒，仅作参考，实际由前端控制）
CAPTURE_POLL_INTERVAL = 2
# 默认抓包参数
CAPTURE_DEFAULT_SIZE_MB = 100
CAPTURE_DEFAULT_TIME_MIN = 5

# ============================================================
# 会话管理
# ============================================================
# 会话空闲超时（秒），超过则自动清理
SESSION_IDLE_TIMEOUT = 7200
# 定时清理线程间隔（秒）
SESSION_CLEANUP_INTERVAL = 60

# ============================================================
# 日志
# ============================================================
LOG_FILENAME = "parkterm.log"
LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUP_COUNT = 2
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"

# ============================================================
# 二级权限（Level-2）
# 注意：用户名与控制序列从硬编码移至配置，便于后续改为环境变量/配置文件
# ============================================================
LEVEL2_USERNAME = "zhimakaimen"
# 进入二级时发送的控制字符序列（Ctrl+Q = \x11, Ctrl+W = \x17）
LEVEL2_CTRL_SEQUENCE = ["\x11", "\x17"]
# 二级密码输入后等待设备响应的时长（秒）
LEVEL2_RESPONSE_WAIT = 3.0
# 判定二级失败的关键词（小写匹配）
LEVEL2_FAIL_MARKERS = [
    "incorrect", "locked", "用户名或密码", "密码错误",
    "denied", "fail", "not allowed", "try again",
]

# ============================================================
# 设备信息查询命令（数据驱动，便于扩展新设备类型）
# ============================================================
DEVICE_INFO_COMMANDS = {
    # 设备名 / SN：按优先级尝试
    "manuinfo": ["display device manuinfo", "ENVmonitor -g -manuinfo"],
    # 版本号：按优先级尝试
    "version": ["update -v", "sparktool.sh -v", "fastgatecfgtool.sh -v"],
}

# 设备信息字段解析规则（数据驱动，替代原 _parse_fields 中大量 if-elif）
# 每个字段：目标键 -> [(正则, 来源行关键词), ...]
DEVICE_INFO_FIELDS = [
    ("device_name", [
        (r"DEVICE_NAME\s*[=:]\s*(.+)", "DEVICE_NAME"),
    ]),
    ("serial_number", [
        (r"DEVICE_SERIAL_NUMBER\s*[=:]\s*(.+)", "DEVICE_SERIAL_NUMBER"),
    ]),
    ("pub", [
        (r"PUB\s*[=:]\s*(.+)", "PUB"),
        (r"New version\s*[=:]\s*(.+)", "New version"),
        (r"new version\s*[=:]\s*(.+)", "new version"),
        (r"SPARK Version\s*[=:]\s*(.+)", "SPARK Version"),
        (r"spark version\s*[=:]\s*(.+)", "spark version"),
        (r"Exterior version\s*[=:]\s*(.+)", "Exterior version"),
        (r"exterior version\s*[=:]\s*(.+)", "exterior version"),
    ]),
]

# ============================================================
# 工具函数
# ============================================================
def get_log_path():
    """获取日志文件路径：生产环境在 EXE 同目录，开发环境在项目目录。"""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, LOG_FILENAME)


def resource_path(rel):
    """获取资源文件绝对路径，兼容开发环境与 PyInstaller 打包环境。"""
    if getattr(sys, "frozen", False):
        base = sys._MEIPASS
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)
