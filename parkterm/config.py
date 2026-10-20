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
VERSION = "v1.0.6"
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
SSH_CONNECT_TIMEOUT = 10
SSH_BANNER_TIMEOUT = 15
SSH_AUTH_TIMEOUT = 10
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
# 性能优化：drain 循环本身已排空输出，缩短固定等待
CMD_DRAIN_DELAY = 0.1
CMD_POST_WAIT = 0.2

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
# 抓包列表缓存时长（秒）—— 须小于前端 1s 轮询间隔，保证每秒刷新
CAPTURE_LIST_CACHE_TTL = 0.5
# 文件大小 / PID 存活缓存时长（秒）
CAPTURE_SIZE_CACHE_TTL = 0.5
CAPTURE_PID_CACHE_TTL = 1.0
# 前端轮询间隔（秒，仅作参考，实际由前端控制）
CAPTURE_POLL_INTERVAL = 2
# 默认抓包参数
CAPTURE_DEFAULT_SIZE_MB = 100
CAPTURE_DEFAULT_TIME_MIN = 5

# ============================================================
# 会话管理
# ============================================================
# 会话空闲超时（秒），超过则自动清理
# 设为 0 表示禁用空闲清理（SSH keepalive 负责保活，由前端心跳更新 last_activity）
SESSION_IDLE_TIMEOUT = 0
# 定时清理线程间隔（秒）
SESSION_CLEANUP_INTERVAL = 60
# Socket.IO 心跳间隔（秒），前端定期发送 ping 保持连接活跃
SOCKETIO_HEARTBEAT_INTERVAL = 25
# Socket.IO 断连后会话保留时间（秒），超时才真正清理
# 给浏览器标签页切换/网络抖动留出重连窗口
SESSION_SOCKET_GRACE = 300

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
# enter_level2 已改为自适应检测欢迎横幅提前结束，此值为兜底上限
LEVEL2_RESPONSE_WAIT = 2.0
# 判定二级失败的关键词（小写匹配）
LEVEL2_FAIL_MARKERS = [
    "incorrect", "locked", "用户名或密码", "密码错误",
    "denied", "fail", "not allowed", "try again",
]

# ============================================================
# 设备信息字段解析规则（数据驱动，替代原 _parse_fields 中大量 if-elif）
# 每个字段：目标键 -> [(正则, 来源行关键词), ...]
DEVICE_INFO_FIELDS = [
    ("device_name", [
        (r"DEVICE_NAME\s*[=:]\s*(.+)", "DEVICE_NAME"),
        (r"BOARD_TYPE\s*[=:]\s*(.+)", "BOARD_TYPE"),
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
# 服务器类型配置（登录时预选，按类型执行对应命令）
# ============================================================
# 每个类型定义:
#   label:        登录界面显示名
#   manuinfo_cmd: 设备名/SN 命令（None 表示跳过设备信息获取）
#   version_cmd:  版本号命令（None 表示跳过）
#   service_cmd:  服务查看命令（None 表示从解析的 deviceVersion 推断）
#   guide_type:   维护指南筛选类型（None 表示从 deviceName 推断）
#   default_port: SSH 默认端口
#   need_level2:  是否需要二级权限检测
#   needs_ssh_enable: 是否需要通过 WEB API 开启 SSH（仅 VMS-U）
#   features:     该产品类型可用的功能列表（前端按此显示/隐藏工具栏按钮）
#                level2        进入二级
#                service_view  服务查看
#                commands      常用命令
#                guide         维护指南
#                diagnostic    诊断导出
#                capture       抓包管理
#                upgrade       版本升级
FEATURE_ALL = ["service_view", "commands", "guide", "capture"]
SERVER_TYPES = {
    "ug": {
        "label": "UG系列",
        "manuinfo_cmd": "display device manuinfo",
        "version_cmd": "update -v",
        "service_cmd": "server.sh status",
        "guide_type": "ug-i500",
        "default_port": 23333,
        "need_level2": True,
        "needs_ssh_enable": False,
        "features": FEATURE_ALL + ["diagnostic", "upgrade", "backup"],
    },
    "vms-u": {
        "label": "VMS-U系列",
        "manuinfo_cmd": "display device manuinfo",
        "version_cmd": "update -v",
        "service_cmd": "server.sh status",
        "guide_type": "unv-guard",
        "default_port": 23333,
        "need_level2": True,
        "needs_ssh_enable": True,
        "features": FEATURE_ALL + ["diagnostic", "upgrade", "backup"],
    },
    "vms-b": {
        "label": "VMS-B系列",
        "manuinfo_cmd": "display device manuinfo",
        "version_cmd": "update -v",
        "service_cmd": "server.sh status",
        "guide_type": "vms-unicorn",
        "default_port": 7777,
        "need_level2": True,
        "needs_ssh_enable": False,
        "features": FEATURE_ALL + ["diagnostic", "runtime_clean"],
    },
    "egs": {
        "label": "EGS系列",
        "manuinfo_cmd": "ENVmonitor -g -manuinfo",
        "version_cmd": "fastgatecfgtool.sh -v",
        "service_cmd": "FastGateServer.sh status",
        "guide_type": "all",
        "default_port": 22,
        "need_level2": False,
        "needs_ssh_enable": False,
        "features": FEATURE_ALL,
    },
    "pms": {
        "label": "PMS系列",
        "manuinfo_cmd": "display device manuinfo",
        "version_cmd": "sparktool.sh -v",
        "service_cmd": "SPARKServer.sh status",
        "guide_type": "all",
        "default_port": 22,
        "need_level2": False,
        "needs_ssh_enable": False,
        "features": FEATURE_ALL,
    },
    "other": {
        "label": "其他",
        "manuinfo_cmd": None,
        "version_cmd": None,
        "service_cmd": None,
        "guide_type": "all",
        "default_port": 22,
        "need_level2": False,
        "needs_ssh_enable": False,
        "features": FEATURE_ALL,
    },
}

# ============================================================
# VMS-U WEB API 配置（用于自动开启 SSH）
# ============================================================
# SSH 开启 API 端口
VMSU_API_PORT = 23006
# SSH 开启后等待时间（秒）
VMSU_SSH_ENABLE_WAIT = 1

# ============================================================
# 数据库备份（VMS-U）配置
# ============================================================
# 备份文件目录
BACKUP_DIR = "/Config_DB"
# 备份脚本超时（秒）
BACKUP_TIMEOUT = 1800
# 备份进度轮询间隔（秒）
BACKUP_POLL_INTERVAL = 1.0

# ============================================================
# 版本升级（VMS-U）配置
# ============================================================
# 升级软件包远程目录（相对 home 路径）
UPGRADE_DIR_NAME = "upgradefiles"
# 软件包格式校验（tar.gz，如 VMS-B2101.1.3.9.P17.260720.tar.gz）
UPGRADE_PKG_SUFFIX = ".tar.gz"
# 版本号从文件名提取：去掉 .tar.gz 后再去掉末尾的 .日期码（6位数字）
UPGRADE_VERSION_RE = r"^(.+?)\.\d{6}$"
# 解压/升级各阶段超时（秒）
UPGRADE_EXTRACT_TIMEOUT = 600
UPGRADE_UPGRADE_TIMEOUT = 3600
# 解压/升级进度轮询间隔（秒）
UPGRADE_POLL_INTERVAL = 1.0

# ============================================================
# 自动更新（ParkTerm 自身）
# ============================================================
# version.json 检查地址（jsDelivr CDN 加速，国内快；raw.githubusercontent.com 国内慢/超时）
UPDATE_CHECK_URL = "https://cdn.jsdelivr.net/gh/CalvinGuojian/ParkTerm@main/version.json"
# 备用地址（jsDelivr 不可用时回退到 GitHub raw，可能慢但可用）
UPDATE_CHECK_URL_FALLBACK = "https://raw.githubusercontent.com/CalvinGuojian/ParkTerm/main/version.json"
# GitHub 下载加速代理前缀（为空则直连 GitHub Releases）
UPDATE_DOWNLOAD_PROXY = "https://ghp.ci/"
# 检查更新超时（秒）
UPDATE_CHECK_TIMEOUT = 8
# 下载 exe 超时（秒）
UPDATE_DOWNLOAD_TIMEOUT = 300
# 下载进度轮询间隔（秒）
UPDATE_POLL_INTERVAL = 0.5


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
