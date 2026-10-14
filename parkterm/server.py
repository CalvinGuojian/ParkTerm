"""
Flask + SocketIO 服务端模块。

包含：
  - Flask app 与 SocketIO 实例创建
  - SessionStore（会话存储 + 后台定时清理线程）
  - 所有 HTTP API 路由
  - 所有 SocketIO 事件处理

每个 SSHSession 附加一个 FileManager 和一个 CaptureManager，
文件操作与抓包状态完全按会话隔离。
"""
import errno
import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
import urllib.request
import urllib.error

import paramiko
from flask import Flask, render_template, request, jsonify, send_file
from flask_socketio import SocketIO, emit, join_room

from . import config
from .ssh_session import SSHSession
from .serial_session import SerialSession
from .file_manager import FileManager
from .capture import CaptureManager
from .upgrade import UpgradeManager, parse_upgrade_version
from .backup import BackupManager
from .updater import updater as app_updater

logger = logging.getLogger("parkterm")


# ============================================================
# VMS-U SSH 自动开启辅助函数
# ============================================================
def _vmsu_enable_ssh(host):
    """通过 openAPI 开启 VMS-U SSH 功能（无需认证）。"""
    ssh_url = f"http://{host}:{config.VMSU_API_PORT}/openAPI/system/v1/network/SSH/set"
    data = json.dumps({"enable": 1}).encode('utf-8')
    headers = {'Content-Type': 'application/json'}
    req = urllib.request.Request(ssh_url, data=data, method='POST', headers=headers)
    logger.info("VMS-U SSH enable: POST %s", ssh_url)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp_text = resp.read().decode('utf-8')
            logger.info("VMS-U SSH enable response: status=%d, body=%s", resp.status, resp_text)
            result = json.loads(resp_text)
        # 检查返回码
        if result.get('code') == 0:
            return True
        else:
            logger.error("VMS-U SSH enable API returned error: %s", resp_text)
            return False
    except urllib.error.HTTPError as e:
        err_body = ''
        try:
            err_body = e.read().decode('utf-8')
        except Exception:
            pass
        logger.error("VMS-U SSH enable HTTP error: %s (code=%d), body=%s", e, e.code, err_body)
        return False
    except Exception as e:
        logger.error("VMS-U SSH enable error: %s", e, exc_info=True)
        return False

# ============================================================
# Flask / SocketIO 实例
# ============================================================
app = Flask(__name__, template_folder=config.resource_path("templates"),
            static_folder=config.resource_path("static"), static_url_path="/static")

# 服务仅监听 127.0.0.1 回环地址，不对外暴露，因此 CORS 设为 * 不会有安全风险。
# 若使用带通配符端口的 origin 列表（如 http://127.0.0.1:*），Flask-SocketIO
# 的 origin 匹配可能不兼容，导致 SocketIO 握手被拒、前端工具栏误报"未连接"。
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="threading",
    logger=False,
    engineio_logger=False,
)


# ============================================================
# 会话存储（含后台定时清理）
# ============================================================
class SessionStore:
    def __init__(self):
        self.sessions = {}
        self.lock = threading.Lock()
        self._cleanup_thread = None
        self._stop_cleanup = threading.Event()

    def create(self, conn_type='ssh', host='', port=22, username='', password='',
               serial_port='', baudrate=115200, databits=8, parity='N',
               stopbits='1', flowcontrol='N', server_type=''):
        sid = str(uuid.uuid4())
        if conn_type == 'serial':
            session = SerialSession(serial_port, baudrate, databits, parity, stopbits, flowcontrol)
        else:
            session = SSHSession(host, port, username, password, server_type=server_type)
            # 附加文件管理器与抓包管理器（按会话隔离）
            session.file_manager = FileManager(session, socketio)
            session.capture_manager = CaptureManager(session)
            # 附加版本升级管理器（VMS-U）
            session.upgrade_manager = UpgradeManager(session)
            # 附加数据库备份管理器（VMS-U）
            session.backup_manager = BackupManager(session)
        with self.lock:
            self.sessions[sid] = session
        return sid, session

    def get(self, sid):
        with self.lock:
            s = self.sessions.get(sid)
            if s:
                s.last_activity = time.time()
            return s

    def get_by_socket(self, socket_sid):
        with self.lock:
            for s in self.sessions.values():
                if s.socket_id == socket_sid:
                    s.last_activity = time.time()
                    return s
        return None

    def remove_by_socket(self, socket_sid):
        """通过 socket ID 查找并移除会话，关闭 SSH 连接。"""
        with self.lock:
            target_sid = None
            for sid, s in self.sessions.items():
                if s.socket_id == socket_sid:
                    target_sid = sid
                    break
        if target_sid:
            self.remove(target_sid)

    def remove(self, sid):
        with self.lock:
            s = self.sessions.pop(sid, None)
        if s:
            s.close()

    def cleanup_stale(self, max_idle=None):
        """清理过期会话。

        清理策略：
        - SSH 连接已断开的会话：立即清理
        - SSH 连接活跃但 socket 已断开：超过 SESSION_SOCKET_GRACE 秒后清理
        - SSH 连接活跃且 socket 也活跃：SESSION_IDLE_TIMEOUT > 0 时按空闲超时清理
          （默认 0 = 禁用，由 SSH keepalive + 前端心跳负责保活）
        """
        now = time.time()
        stale = []
        with self.lock:
            for sid, s in self.sessions.items():
                # SSH 连接已断开：立即清理
                if not s.connected:
                    stale.append(sid)
                    continue
                # socket 已断开但 SSH 仍活跃：grace 超时后清理
                if s.socket_id is None:
                    if now - s.last_activity > config.SESSION_SOCKET_GRACE:
                        stale.append(sid)
                    continue
                # 一切正常：按空闲超时清理（0 = 禁用）
                if max_idle and max_idle > 0:
                    if now - s.last_activity > max_idle:
                        stale.append(sid)
        for sid in stale:
            logger.info("Cleaning up stale session: %s", sid)
            self.remove(sid)

    def start_cleanup_thread(self):
        """启动后台定时清理线程，每 SESSION_CLEANUP_INTERVAL 秒清理一次过期会话。"""
        if self._cleanup_thread and self._cleanup_thread.is_alive():
            return
        self._stop_cleanup.clear()

        def _loop():
            while not self._stop_cleanup.is_set():
                try:
                    self.cleanup_stale()
                except Exception:
                    logger.exception("Session cleanup error")
                self._stop_cleanup.wait(config.SESSION_CLEANUP_INTERVAL)

        self._cleanup_thread = threading.Thread(target=_loop, daemon=True)
        self._cleanup_thread.start()
        logger.info("Session cleanup thread started (interval=%ds)",
                    config.SESSION_CLEANUP_INTERVAL)

    def stop_cleanup_thread(self):
        self._stop_cleanup.set()


store = SessionStore()


# ============================================================
# HTTP 路由
# ============================================================
@app.route("/")
def index():
    return render_template("index.html", app_version=config.VERSION)


@app.route("/api/connect", methods=["POST"])
def api_connect():
    d = request.json or {}
    conn_type = d.get("type", "ssh")
    if conn_type == "serial":
        serial_port = (d.get("port") or "").strip()
        if not serial_port:
            return jsonify({"ok": False, "error": "请选择串口"}), 400
        baudrate = int(d.get("baudrate", 115200) or 115200)
        databits = int(d.get("databits", 8) or 8)
        parity = d.get("parity", "N")
        stopbits = d.get("stopbits", "1")
        flowcontrol = d.get("flowcontrol", "N")
        store.cleanup_stale()
        logger.info("Serial connect request: port=%s baud=%d", serial_port, baudrate)
        try:
            sid, session = store.create(
                conn_type='serial', serial_port=serial_port, baudrate=baudrate,
                databits=databits, parity=parity, stopbits=stopbits, flowcontrol=flowcontrol)
            session.connect()
            return jsonify({"ok": True, "session_id": sid, "port_name": serial_port})
        except Exception as e:
            import traceback
            logger.error("Serial connect failed: %s -> %s\n%s", serial_port, e, traceback.format_exc())
            return jsonify({"ok": False, "error": f"串口连接失败: {e}"}), 500

    # SSH 连接
    host = (d.get("host") or "").strip()
    port = d.get("port", 22)
    username = (d.get("username") or "").strip()
    password = d.get("password", "")
    server_type = d.get("server_type", "")
    web_password = d.get("web_password", "")
    if not host or not username:
        return jsonify({"ok": False, "error": "请填写主机地址和用户名"}), 400
    if not server_type:
        return jsonify({"ok": False, "error": "请选择服务器类型"}), 400
    store.cleanup_stale()
    logger.info("Connect request: host=%s, port=%s, user=%s, type=%s", host, port, username, server_type)

    # 先尝试直接 SSH 连接
    try:
        sid, session = store.create(conn_type='ssh', host=host, port=port,
                                     username=username, password=password,
                                     server_type=server_type)
        session.connect()
        st = config.SERVER_TYPES.get(server_type, {})
        return jsonify({"ok": True, "session_id": sid,
                        "need_level2": st.get("need_level2", False),
                        "features": st.get("features", [])})
    except paramiko.AuthenticationException:
        logger.warning("Auth failed: %s:%s user=%s", host, port, username)
        return jsonify({"ok": False, "error": "认证失败：用户名或密码错误"}), 401
    except paramiko.SSHException as e:
        logger.error("SSH error: %s:%s -> %s", host, port, e)
        return jsonify({"ok": False, "error": f"SSH协议错误: {e}"}), 500
    except OSError as e:
        logger.error("Network error: %s:%s errno=%s -> %s", host, port, e.errno, e)
        # 端口不可达时，VMS-U/UG 类型尝试自动开启 SSH
        if server_type in ('vms-u', 'ug'):
            if web_password:
                logger.info("VMS-U/UG: port unreachable, trying to enable SSH via openAPI for host=%s", host)
                ssh_enabled = _vmsu_enable_ssh(host)
                if ssh_enabled:
                    logger.info("VMS-U/UG: SSH enabled, waiting %ds before retry", config.VMSU_SSH_ENABLE_WAIT)
                    time.sleep(config.VMSU_SSH_ENABLE_WAIT)
                    # 重试连接
                    try:
                        sid, session = store.create(conn_type='ssh', host=host, port=port,
                                                     username=username, password=password,
                                                     server_type=server_type)
                        session.connect()
                        st = config.SERVER_TYPES.get(server_type, {})
                        return jsonify({"ok": True, "session_id": sid,
                                        "need_level2": st.get("need_level2", False),
                                        "features": st.get("features", [])})
                    except paramiko.AuthenticationException:
                        logger.warning("Auth failed (retry): %s:%s user=%s", host, port, username)
                        return jsonify({"ok": False, "error": "认证失败：用户名或密码错误"}), 401
                    except paramiko.SSHException as e2:
                        logger.error("SSH error (retry): %s:%s -> %s", host, port, e2)
                        return jsonify({"ok": False, "error": f"SSH协议错误: {e2}"}), 500
                    except OSError as e2:
                        logger.error("Network error (retry): %s:%s errno=%s -> %s", host, port, e2.errno, e2)
                        return jsonify({"ok": False, "error": f"SSH已开启但连接仍失败: {e2}"}), 500
                    except Exception as e2:
                        logger.error("Connect failed (retry): %s:%s -> %s", host, port, e2)
                        return jsonify({"ok": False, "error": f"SSH已开启但连接失败: {e2}"}), 500
                else:
                    return jsonify({"ok": False, "error": "SSH端口不可达，自动开启SSH失败，请手动开启或检查网络"}), 500
            else:
                return jsonify({"ok": False, "error": "SSH端口不可达，请手动开启SSH或输入WEB密码后使用工具重连自动开启"}), 500
        # 非 VMS-U/UG 类型的常规错误提示
        if e.errno in (errno.ECONNREFUSED, 10061):
            return jsonify({"ok": False, "error": f"连接被拒绝：请确认端口 {port} 是否正确且服务已启动"}), 500
        if e.errno in (errno.ETIMEDOUT, 10060):
            return jsonify({"ok": False, "error": f"连接超时：请确认主机 {host} 可达且端口 {port} 已开放"}), 500
        return jsonify({"ok": False, "error": f"网络错误: {e}"}), 500
    except Exception as e:
        import traceback
        logger.error("Connect failed: %s:%s -> %s\n%s", host, port, e, traceback.format_exc())
        return jsonify({"ok": False, "error": f"连接失败: {e}"}), 500


@app.route("/api/disconnect", methods=["POST"])
def api_disconnect():
    sid = (request.json or {}).get("session_id", "")
    store.remove(sid)
    return jsonify({"ok": True})


@app.route("/api/level2", methods=["POST"])
def api_level2():
    d = request.json or {}
    sid = d.get("session_id", "")
    password = d.get("password", "")
    session = store.get(sid)
    if not session or not session.connected:
        return jsonify({"ok": False, "error": "未连接到服务器"}), 400
    try:
        res = session.enter_level2(password)
        return jsonify({"ok": True, "level2_ok": bool(res.get("ok")), "output": res.get("output", "")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/device_info", methods=["POST"])
def api_device_info():
    d = request.json or {}
    sid = d.get("session_id", "")
    session = store.get(sid)
    if not session or not session.connected:
        return jsonify({"ok": False, "error": "未连接"}), 400
    try:
        info = session.get_device_info()
        # 附带该产品类型的服务查看命令，前端不再需要硬编码映射
        st = config.SERVER_TYPES.get(session.server_type, {})
        info["service_cmd"] = st.get("service_cmd", "")
        return jsonify({"ok": True, "info": info})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/system_info", methods=["POST"])
def api_system_info():
    """获取SSH连接的远程服务器系统信息（OS、内核、总内存）。"""
    d = request.json or {}
    sid = d.get("session_id", "")
    session = store.get(sid)
    if not session or not session.connected:
        return jsonify({"ok": False, "error": "未连接"}), 400
    try:
        # 使用独立 exec channel，不干扰交互式终端
        cmd = ('uname -r 2>/dev/null; echo "===SPLIT==="; '
               'cat /etc/os-release 2>/dev/null; echo "===SPLIT==="; '
               'grep MemTotal /proc/meminfo 2>/dev/null')
        out = session.run_command_exec(cmd, timeout=config.CMD_DEVICE_INFO_TIMEOUT)
        # 不回退到 run_command，避免干扰终端输出

        os_display = "--"
        kernel = "--"
        total_mem = 0

        if out:
            parts = out.split("===SPLIT===")
            if len(parts) >= 1:
                kernel = parts[0].strip() or "--"
            if len(parts) >= 2:
                for line in parts[1].splitlines():
                    line = line.strip()
                    if line.startswith("PRETTY_NAME="):
                        os_display = line.split("=", 1)[1].strip().strip('"')
                        break
                else:
                    # 没有 /etc/os-release，尝试从 uname 信息推断
                    if kernel != "--":
                        os_display = "Linux"
            if len(parts) >= 3:
                mem_line = parts[2].strip()
                if "MemTotal:" in mem_line:
                    try:
                        total_mem = int(mem_line.split()[1]) * 1024
                    except (IndexError, ValueError):
                        pass

        return jsonify({
            "ok": True,
            "os": os_display,
            "kernel": kernel,
            "total_memory": total_mem,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/check_level1", methods=["POST"])
def api_check_level1():
    sid = (request.json or {}).get("session_id", "")
    session = store.get(sid)
    if not session or not session.connected:
        return jsonify({"ok": False, "error": "未连接"}), 400
    try:
        # 使用独立 exec channel，不阻塞终端输出
        # 探测命令用 echo 而非 ls：VMS-U 受限一级 shell 允许 ls 但拦截 echo，
        # echo 被拦截说明是受限环境（tcpdump/nohup 等均不可用），需进入二级
        out = session.run_command_exec("echo PARKTERM_CHK", timeout=config.CMD_CHECK_LEVEL1_TIMEOUT)
        if not out:
            # exec channel 不可用时回退
            out = session.run_command("echo PARKTERM_CHK", timeout=config.CMD_CHECK_LEVEL1_TIMEOUT)
        logger.debug("check_level1 'echo' output: %r", out[:300])
        if "Not Support Cmd Yet!" in out:
            return jsonify({"ok": True, "need_level2": True})
        return jsonify({"ok": True, "need_level2": False})
    except Exception as e:
        logger.debug("check_level1 failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/signal", methods=["POST"])
def api_signal():
    d = request.json or {}
    sid = d.get("session_id", "")
    signal_name = d.get("signal", "Ctrl+C")
    session = store.get(sid)
    if not session or not session.connected:
        return jsonify({"ok": False, "error": "未连接"}), 400
    signals = {"Ctrl+C": "\x03", "Ctrl+Z": "\x1a", "Ctrl+D": "\x04"}
    char = signals.get(signal_name)
    if not char:
        return jsonify({"ok": False, "error": f"未知信号: {signal_name}"}), 400
    try:
        session.send(char)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ============================================================
# 文件管理路由
# ============================================================
@app.route("/api/upload", methods=["POST"])
def api_upload():
    sid = request.form.get("session_id", "")
    remote_path = request.form.get("remote_path", "").strip()
    transfer_id = request.form.get("transfer_id", "")
    session = store.get(sid)
    if not session or not session.connected:
        return jsonify({"ok": False, "error": "未连接"}), 400
    if not remote_path:
        return jsonify({"ok": False, "error": "请填写远程路径"}), 400
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"ok": False, "error": "请选择文件"}), 400
    tmp = os.path.join(tempfile.gettempdir(), f"upload_{uuid.uuid4().hex}_{f.filename}")
    try:
        f.save(tmp)
        session.file_manager.upload(tmp, remote_path, transfer_id=transfer_id)
        return jsonify({"ok": True, "message": f"已上传到 {remote_path}"})
    except Exception as e:
        return jsonify({"ok": False, "error": f"上传失败: {e}"}), 500
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass


@app.route("/api/download", methods=["POST"])
def api_download():
    d = request.json or {}
    sid = d.get("session_id", "")
    remote_path = d.get("remote_path", "").strip()
    transfer_id = d.get("transfer_id", "")
    session = store.get(sid)
    if not session or not session.connected:
        return jsonify({"ok": False, "error": "未连接"}), 400
    if not remote_path:
        return jsonify({"ok": False, "error": "请填写远程文件路径"}), 400
    filename = os.path.basename(remote_path) or "download"
    tmp = os.path.join(tempfile.gettempdir(), f"dl_{uuid.uuid4().hex}_{filename}")
    try:
        session.file_manager.download(remote_path, tmp, transfer_id=transfer_id)
        return send_file(tmp, as_attachment=True, download_name=filename)
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "远程文件不存在"}), 404
    except Exception as e:
        return jsonify({"ok": False, "error": f"下载失败: {e}"}), 500
    finally:
        # send_file 完成后异步清理（延迟删除，避免文件还在传输中）
        def _cleanup():
            time.sleep(5)
            try:
                os.remove(tmp)
            except Exception:
                pass
        threading.Thread(target=_cleanup, daemon=True).start()


@app.route("/api/sftp_ls", methods=["POST"])
def api_sftp_ls():
    d = request.json or {}
    sid = d.get("session_id", "")
    path = d.get("path", ".").strip()
    session = store.get(sid)
    if not session or not session.connected:
        return jsonify({"ok": False, "error": "未连接"}), 400
    try:
        items = session.file_manager.listdir(path)
        return jsonify({
            "ok": True,
            "path": path,
            "items": items,
            "method": session.file_manager._last_ls_method,
        })
    except RuntimeError as e:
        msg = str(e)
        if msg.startswith("unsupported"):
            detail = msg.split(":", 1)[1] if ":" in msg else "当前环境不支持文件列表"
            return jsonify({"ok": False, "error": detail, "need_level2": True}), 200
        return jsonify({"ok": False, "error": msg}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/sftp_delete", methods=["POST"])
def api_sftp_delete():
    d = request.json or {}
    sid = d.get("session_id", "")
    path = d.get("path", "").strip()
    session = store.get(sid)
    if not session or not session.connected:
        return jsonify({"ok": False, "error": "未连接"}), 400
    try:
        session.file_manager.delete(path)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/sftp_mkdir", methods=["POST"])
def api_sftp_mkdir():
    d = request.json or {}
    sid = d.get("session_id", "")
    path = d.get("path", "").strip()
    session = store.get(sid)
    if not session or not session.connected:
        return jsonify({"ok": False, "error": "未连接"}), 400
    try:
        session.file_manager.mkdir(path)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/sftp_rename", methods=["POST"])
def api_sftp_rename():
    d = request.json or {}
    sid = d.get("session_id", "")
    old_path = d.get("old_path", "").strip()
    new_path = d.get("new_path", "").strip()
    session = store.get(sid)
    if not session or not session.connected:
        return jsonify({"ok": False, "error": "未连接"}), 400
    if not old_path or not new_path:
        return jsonify({"ok": False, "error": "路径不能为空"}), 400
    try:
        session.file_manager.rename(old_path, new_path)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ============================================================
# 诊断导出路由
# ============================================================
DIAGNOSTIC_DIR = "/var/ums_logbackup/diagnosis"
DIAGNOSTIC_API_PORT = 23006
# VMS-B 诊断收集配置
DIAGNOSTIC_DIR_VMSB = "/var/coredump"
DIAGNOSTIC_SCRIPT_REMOTE = "/usr/bin/systemreport_vms_parkterm.sh"
DIAGNOSTIC_PROGRESS_FILE = "/tmp/diagnosis_progress_info.txt"


def _get_diagnostic_dir(session):
    """根据产品类型返回诊断目录。"""
    if getattr(session, 'server_type', '') == 'vms-b':
        return DIAGNOSTIC_DIR_VMSB
    return DIAGNOSTIC_DIR


def _upload_diag_script(session):
    """上传诊断脚本到 /usr/bin/ 并赋权 777。

    使用 file_manager.upload() 三级降级（SFTP → SCP → exec base64分块），
    与手动上传走相同通道，确保兼容性。
    """
    script_path = config.resource_path('parkterm/systemreport_vms_parkterm.sh')
    if not os.path.isfile(script_path):
        logger.error("Diag script not found: %s", script_path)
        return False

    try:
        session.file_manager.upload(script_path, DIAGNOSTIC_SCRIPT_REMOTE)
        logger.info("Diag script uploaded via file_manager: %s", DIAGNOSTIC_SCRIPT_REMOTE)
    except Exception as e:
        logger.error("Failed to upload diag script via file_manager: %s", e)
        return False

    # 赋权 777
    try:
        session.exec_capture(f'chmod 777 {DIAGNOSTIC_SCRIPT_REMOTE}', timeout=5)
    except Exception:
        try:
            session.run_command(f'chmod 777 {DIAGNOSTIC_SCRIPT_REMOTE}', timeout=5)
        except Exception as e:
            logger.warning("chmod failed: %s", e)

    return True


def _diagnostic_api_call(host, path, method='GET', data=None, timeout=15):
    """直接通过 HTTP 调用诊断 API（不经过 SSH curl）。"""
    url = f"http://{host}:{DIAGNOSTIC_API_PORT}{path}"
    headers = {'Content-Type': 'application/json'}
    body = json.dumps(data).encode('utf-8') if data else None
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    logger.info("Diagnostic API: %s %s", method, url)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp_text = resp.read().decode('utf-8')
            logger.info("Diagnostic API response: status=%d, body=%s", resp.status, resp_text[:500])
            return json.loads(resp_text)
    except urllib.error.HTTPError as e:
        err_body = ''
        try:
            err_body = e.read().decode('utf-8')
        except Exception:
            pass
        logger.error("Diagnostic API HTTP error: %s (code=%d), body=%s", e, e.code, err_body)
        return None
    except Exception as e:
        logger.error("Diagnostic API error: %s", e, exc_info=True)
        return None


def _parse_diagnostic_file_list(resp):
    """从 diagnosticLog/list 响应中提取文件列表。
    
    响应格式: {"code":0, "data": {"diagnosticList": [{"name":"xxx.zip", "status":1, "url":"...", "progress":40}, ...]}}
    status: 1=完成, 2=生成中
    返回: (done_files, pending_files, all_items)
      done_files: 已完成的文件名列表
      pending_files: 生成中的文件名列表
      all_items: 完整的文件信息列表 [{name, status, progress, url}, ...]
    """
    if not resp:
        return [], [], []
    data = resp.get('data', {})
    if not isinstance(data, dict):
        return [], [], []
    diag_list = data.get('diagnosticList', [])
    if not isinstance(diag_list, list):
        return [], [], []
    done_files = []
    pending_files = []
    all_items = []
    for item in diag_list:
        if not isinstance(item, dict):
            continue
        name = item.get('name') or item.get('fileName')
        status = item.get('status')
        progress = item.get('progress', 0)
        url = item.get('url', '')
        if name:
            all_items.append({
                'name': name,
                'status': status,
                'progress': progress,
                'url': url,
            })
            if status == 1:
                done_files.append(name)
            else:
                pending_files.append(name)
    return done_files, pending_files, all_items


@app.route("/api/diagnostic_list", methods=["POST"])
def api_diagnostic_list():
    """获取诊断文件列表（含文件大小和修改时间）。"""
    d = request.json or {}
    sid = d.get("session_id", "")
    session = store.get(sid)
    if not session or not session.connected:
        return jsonify({"ok": False, "error": "未连接"}), 400

    host = session.host
    is_vmsb = getattr(session, 'server_type', '') == 'vms-b'
    diag_dir = _get_diagnostic_dir(session)
    # 优先用 API 获取文件列表（含状态/进度）— VMS-B 跳过 API
    api_items = []
    if not is_vmsb:
        resp = _diagnostic_api_call(host, "/openAPI/devOps/v1/diagnosticLog/list", method='POST')
        if resp:
            _, _, api_items = _parse_diagnostic_file_list(resp)
            logger.info("Diagnostic list (API): %d items", len(api_items))

    # 用 SSH 获取文件大小和修改时间（API 不返回这些字段）
    # 使用 FileManager.listdir（三级回退：SFTP→exec→交互shell），与备份列表同源
    file_details = {}
    fm = getattr(session, "file_manager", None)
    if fm:
        try:
            items = fm.listdir(diag_dir)
            for item in items:
                if item.get("is_dir"):
                    continue
                # VMS-B 只显示 .tgz 文件
                if is_vmsb and not item["name"].endswith(".tgz"):
                    continue
                file_details[item["name"]] = {
                    "size": item.get("size", 0),
                    "mtime": item.get("mtime", 0),
                }
            logger.info("Diagnostic listdir: %d files via %s",
                        len(file_details), fm._last_ls_method)
        except Exception as e:
            logger.warning("Diagnostic listdir failed: %s, trying run_command", e)

    # FileManager 不可用或失败时，回退到交互 shell
    if not file_details:
        try:
            ls_out = session.run_command(
                f"ls -la --time-style=+%s {diag_dir}/ 2>/dev/null",
                timeout=8
            )
            if ls_out and "Not Support Cmd Yet!" not in ls_out:
                for line in ls_out.strip().split("\n"):
                    parts = line.split()
                    if len(parts) >= 7 and parts[0].startswith("-"):
                        try:
                            size = int(parts[4])
                            mtime = int(parts[5]) if parts[5].isdigit() else 0
                            name = " ".join(parts[6:])
                            if name and not name.startswith("total"):
                                if is_vmsb and not name.endswith(".tgz"):
                                    continue
                                file_details[name] = {"size": size, "mtime": mtime}
                        except (ValueError, IndexError):
                            pass
                logger.info("Diagnostic run_command fallback: %d files", len(file_details))
        except Exception as e:
            logger.error("Diagnostic run_command fallback failed: %s", e)

    logger.info("Diagnostic list: API=%d items, SSH=%d files",
                len(api_items), len(file_details))

    # 合并 API 和 SSH 数据——以 SSH 文件为全集，API 补充状态/进度
    all_items = []
    api_map = {item["name"]: item for item in api_items}
    for name, detail in file_details.items():
        api_item = api_map.get(name)
        if api_item:
            # API 有此文件——合并状态/进度 + SSH 大小/时间
            all_items.append({
                "name": name,
                "status": api_item.get("status", 1),
                "progress": api_item.get("progress", 100),
                "url": api_item.get("url", ""),
                "size": detail["size"],
                "mtime": detail["mtime"],
            })
        else:
            # SSH 有但 API 没有——视为已完成
            all_items.append({
                "name": name,
                "status": 1,
                "progress": 100,
                "url": "",
                "size": detail["size"],
                "mtime": detail["mtime"],
            })

    # 如果 SSH 完全失败，回退到纯 API 数据
    if not all_items and api_items:
        all_items = api_items

    # 按修改时间倒序
    all_items.sort(key=lambda x: x.get("mtime", 0), reverse=True)
    return jsonify({"ok": True, "files": all_items, "dir": diag_dir})


@app.route("/api/diagnostic_start", methods=["POST"])
def api_diagnostic_start():
    """启动诊断收集：VMS-U 调用 openAPI，VMS-B 执行脚本。"""
    d = request.json or {}
    sid = d.get("session_id", "")
    start_time = d.get("start_time", "").strip()
    end_time = d.get("end_time", "").strip()
    session = store.get(sid)
    if not session or not session.connected:
        return jsonify({"ok": False, "error": "未连接"}), 400

    host = session.host
    is_vmsb = getattr(session, 'server_type', '') == 'vms-b'
    diag_dir = _get_diagnostic_dir(session)

    # VMS-B 不需要时间范围参数
    if not is_vmsb and (not start_time or not end_time):
        return jsonify({"ok": False, "error": "请选择起止时间"}), 400

    logger.info("Diagnostic start: host=%s, type=%s, startTime=%s, endTime=%s",
                host, session.server_type, start_time, end_time)

    try:
        if is_vmsb:
            # === VMS-B: 上传脚本并执行 ===
            # 1. 上传诊断脚本到 /usr/bin/ 并赋权 777
            if not _upload_diag_script(session):
                return jsonify({"ok": False, "error": "诊断脚本上传失败"}), 500

            # 2. 获取基线文件列表
            baseline_files = []
            baseline_out = session.run_command(
                f"ls -1 {diag_dir}/*.tgz 2>/dev/null || echo '__EMPTY__'",
                timeout=8
            )
            if baseline_out:
                baseline_files = [f.strip().split('/')[-1] for f in baseline_out.strip().split("\n")
                                  if f.strip() and f.strip() != "__EMPTY__"]
                logger.info("Diagnostic baseline (VMS-B SSH): %d files", len(baseline_files))

            # 3. 后台执行诊断脚本
            session.run_command(
                f"nohup {DIAGNOSTIC_SCRIPT_REMOTE} > /dev/null 2>&1 &",
                timeout=5
            )
            logger.info("Diagnostic script started (VMS-B)")

            return jsonify({
                "ok": True,
                "baseline": baseline_files,
                "export_response": "VMS-B diagnostic script started",
            })

        else:
            # === VMS-U / UG: 调用 openAPI ===
            # 1. 获取基线文件列表（优先用 API，回退到 SSH ls）
            baseline_files = []
            baseline_resp = _diagnostic_api_call(host, "/openAPI/devOps/v1/diagnosticLog/list", method='POST')
            if baseline_resp:
                baseline_done, baseline_pending, _ = _parse_diagnostic_file_list(baseline_resp)
                baseline_files = baseline_done + baseline_pending
                logger.info("Diagnostic baseline (API): %d done, %d pending", len(baseline_done), len(baseline_pending))
            else:
                # 回退到 SSH ls（交互 shell，设备兼容性更好）
                baseline_out = session.run_command(
                    f"ls -1 {diag_dir}/ 2>/dev/null || echo '__EMPTY__'",
                    timeout=8
                )
                if baseline_out:
                    baseline_files = [f.strip() for f in baseline_out.strip().split("\n") if f.strip() and f.strip() != "__EMPTY__"]
                    logger.info("Diagnostic baseline (SSH): %d files", len(baseline_files))

            # 2. 调用 diagnosticLog/get 触发诊断导出
            export_resp = _diagnostic_api_call(
                host, "/openAPI/devOps/v1/diagnosticLog/get",
                method='POST',
                data={"startTime": start_time, "endTime": end_time},
                timeout=15
            )
            export_msg = ""
            if export_resp:
                export_msg = json.dumps(export_resp, ensure_ascii=False)[:500]
            else:
                # API 调用失败，回退到 SSH curl
                logger.warning("Diagnostic API call failed, falling back to SSH curl")
                curl_cmd = (
                    f'curl -s -X POST http://127.0.0.1:{DIAGNOSTIC_API_PORT}/openAPI/devOps/v1/diagnosticLog/get '
                    f'-H "Content-Type: application/json" '
                    f"-d '{{\"startTime\": \"{start_time}\", \"endTime\": \"{end_time}\"}}' 2>&1"
                )
                curl_out = session.run_command(curl_cmd, timeout=30)
                if curl_out:
                    export_msg = curl_out.strip()[:500]

        return jsonify({
            "ok": True,
            "baseline": baseline_files,
            "export_response": export_msg,
        })
    except Exception as e:
        logger.error("Diagnostic start error: %s", e, exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/diagnostic_check", methods=["POST"])
def api_diagnostic_check():
    """检查诊断文件是否生成：通过 API 或 SSH 对比基线。"""
    d = request.json or {}
    sid = d.get("session_id", "")
    baseline = d.get("baseline", [])
    session = store.get(sid)
    if not session or not session.connected:
        return jsonify({"ok": False, "error": "未连接"}), 400

    host = session.host
    is_vmsb = getattr(session, 'server_type', '') == 'vms-b'
    diag_dir = _get_diagnostic_dir(session)

    try:
        current_done = []
        current_pending = []
        current_items = []

        if is_vmsb:
            # === VMS-B: 通过 SSH 检查进度和新文件 ===
            # 读取进度文件
            progress_pct = 0
            prog_out = session.run_command(
                f"cat {DIAGNOSTIC_PROGRESS_FILE} 2>/dev/null || echo '0'",
                timeout=5
            )
            if prog_out:
                try:
                    progress_pct = int(prog_out.strip())
                except ValueError:
                    progress_pct = 0
            logger.info("Diagnostic check (VMS-B): progress=%d%%", progress_pct)

            # 检查新文件
            current_out = session.run_command(
                f"ls -1 {diag_dir}/*.tgz 2>/dev/null || echo '__EMPTY__'",
                timeout=8
            )
            if current_out:
                all_files = [f.strip().split('/')[-1] for f in current_out.strip().split("\n")
                             if f.strip() and f.strip() != "__EMPTY__"]
                current_done = all_files
                current_items = [{"name": f, "status": 1, "progress": 100, "url": ""} for f in all_files]
                logger.info("Diagnostic check (VMS-B SSH): %d files", len(current_done))
        else:
            # === VMS-U / UG: 通过 API 检查 ===
            # 优先用 API 获取文件列表
            resp = _diagnostic_api_call(host, "/openAPI/devOps/v1/diagnosticLog/list", method='POST')
            if resp:
                current_done, current_pending, current_items = _parse_diagnostic_file_list(resp)
                logger.info("Diagnostic check (API): %d done, %d pending", len(current_done), len(current_pending))
            else:
                # 回退到 SSH ls（交互 shell）
                current_out = session.run_command(
                    f"ls -1 {diag_dir}/ 2>/dev/null || echo '__EMPTY__'",
                    timeout=8
                )
                if current_out:
                    all_files = [f.strip() for f in current_out.strip().split("\n") if f.strip() and f.strip() != "__EMPTY__"]
                    current_done = all_files
                    current_items = [{"name": f, "status": 1, "progress": 100, "url": ""} for f in all_files]
                    logger.info("Diagnostic check (SSH): %d files", len(current_done))

            # 获取生成中文件的进度
            progress_pct = 0
            pending_items = [item for item in current_items if item.get('status') != 1]
            if pending_items:
                progress_pct = max(item.get('progress', 0) for item in pending_items)

        # 新完成的文件 = 当前已完成 - 基线中已有的
        new_done = [f for f in current_done if f not in baseline]

        return jsonify({
            "ok": True,
            "done": len(new_done) > 0,
            "new_files": new_done,
            "pending": current_pending,
            "progress": progress_pct,
            "files": current_items,
            "dir": diag_dir,
        })
    except Exception as e:
        logger.error("Diagnostic check error: %s", e, exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/diagnostic_delete", methods=["POST"])
def api_diagnostic_delete():
    """删除诊断文件。"""
    d = request.json or {}
    sid = d.get("session_id", "")
    name = d.get("name", "").strip()
    session = store.get(sid)
    if not session or not session.connected:
        return jsonify({"ok": False, "error": "未连接"}), 400
    if not name or "/" in name or "\\" in name or ".." in name:
        return jsonify({"ok": False, "error": "非法文件名"}), 400
    try:
        diag_dir = _get_diagnostic_dir(session)
        fpath = f"{diag_dir}/{name}"
        session.run_command(f"rm -f {fpath}", timeout=10)
        logger.info("Diagnostic file deleted: %s", fpath)
        return jsonify({"ok": True})
    except Exception as e:
        logger.error("Diagnostic delete error: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


# ============================================================
# 数据库备份路由（VMS-U）
# ============================================================
@app.route("/api/backup_list", methods=["POST"])
def api_backup_list():
    """列出 /Config_DB 下的备份文件。"""
    sid = (request.json or {}).get("session_id", "")
    session = store.get(sid)
    if not session or not session.connected:
        return jsonify({"ok": False, "error": "未连接"}), 400
    bm = getattr(session, "backup_manager", None)
    if bm is None:
        return jsonify({"ok": False, "error": "备份管理器不可用"}), 400
    try:
        files = bm.list_files()
        return jsonify({"ok": True, "files": files, "dir": config.BACKUP_DIR})
    except Exception as e:
        logger.error("backup_list failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/backup_start", methods=["POST"])
def api_backup_start():
    """启动手动备份（同步脚本到 /Config_DB 后执行）。"""
    sid = (request.json or {}).get("session_id", "")
    session = store.get(sid)
    if not session or not session.connected:
        return jsonify({"ok": False, "error": "未连接"}), 400
    if session.server_type != 'vms-u':
        return jsonify({"ok": False, "error": "数据库备份仅支持 VMS-U 系列设备"}), 400
    bm = getattr(session, "backup_manager", None)
    if bm is None:
        return jsonify({"ok": False, "error": "备份管理器不可用"}), 500
    if bm.is_running():
        return jsonify({"ok": False, "error": "已有备份任务正在进行中"}), 400
    try:
        bm.start()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/backup_status", methods=["POST"])
def api_backup_status():
    """查询备份进度状态。"""
    sid = (request.json or {}).get("session_id", "")
    session = store.get(sid)
    if not session:
        return jsonify({"ok": False, "error": "未连接"}), 400
    bm = getattr(session, "backup_manager", None)
    if bm is None:
        return jsonify({"ok": False, "error": "备份管理器不可用"}), 400
    st = bm.status()
    # 补充耗时
    if st.get("started"):
        now = st.get("finished") or time.time()
        st["elapsed"] = int(now - st["started"])
    else:
        st["elapsed"] = 0
    return jsonify({"ok": True, "status": st})


@app.route("/api/backup_download", methods=["POST"])
def api_backup_download():
    """下载备份文件（仅支持 /Config_DB 下的 .tar.gz）。"""
    d = request.json or {}
    sid = d.get("session_id", "")
    name = (d.get("name") or "").strip()
    session = store.get(sid)
    if not session or not session.connected:
        return jsonify({"ok": False, "error": "未连接"}), 400
    # 文件名安全校验：不含路径分隔符、必须 .tar.gz
    if not name or "/" in name or "\\" in name or ".." in name or not name.endswith(".tar.gz"):
        return jsonify({"ok": False, "error": "非法文件名"}), 400
    remote_path = f"{config.BACKUP_DIR}/{name}"
    filename = os.path.basename(remote_path)
    tmp = os.path.join(tempfile.gettempdir(), f"bk_{uuid.uuid4().hex}_{filename}")
    try:
        session.file_manager.download(remote_path, tmp)
        return send_file(tmp, as_attachment=True, download_name=filename)
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "备份文件不存在（可能已被清理）"}), 404
    except Exception as e:
        return jsonify({"ok": False, "error": f"下载失败: {e}"}), 500


# ============================================================
# 版本升级路由（VMS-U）
# ============================================================
@app.route("/api/upgrade_start", methods=["POST"])
def api_upgrade_start():
    """启动版本升级：接收 tar.gz 软件包并触发后台升级流程。"""
    sid = request.form.get("session_id", "")
    session = store.get(sid)
    if not session or not session.connected:
        return jsonify({"ok": False, "error": "未连接"}), 400

    # 仅 VMS-U 类型支持
    if session.server_type != 'vms-u':
        return jsonify({"ok": False, "error": "版本升级仅支持 VMS-U 系列设备"}), 400

    um = getattr(session, "upgrade_manager", None)
    if um is None:
        return jsonify({"ok": False, "error": "升级管理器不可用"}), 500
    if um.is_running():
        return jsonify({"ok": False, "error": "已有升级任务正在进行中"}), 400

    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"ok": False, "error": "请选择软件包"}), 400

    filename = f.filename
    # 安全校验：文件名不含路径分隔符和 shell 元字符，防止目录穿越和命令注入
    if "/" in filename or "\\" in filename or ".." in filename:
        return jsonify({"ok": False, "error": "非法文件名"}), 400
    # 过滤 shell 元字符（; | $ ` 空格 等），防止拼入 tar/update 命令时注入
    _SHELL_SAFE_RE = re.compile(r'^[a-zA-Z0-9._\-]+$')
    if not _SHELL_SAFE_RE.match(filename):
        return jsonify({"ok": False, "error": "文件名包含非法字符，仅允许字母、数字、点、下划线、连字符"}), 400
    if not filename.lower().endswith(config.UPGRADE_PKG_SUFFIX):
        return jsonify({"ok": False, "error": "软件包格式必须为 .tar.gz"}), 400

    # 保存到本地临时文件
    tmp = os.path.join(tempfile.gettempdir(), f"upgrade_{uuid.uuid4().hex}_{filename}")
    try:
        f.save(tmp)
    except Exception as e:
        return jsonify({"ok": False, "error": f"软件包保存失败: {e}"}), 500

    version = parse_upgrade_version(filename)
    try:
        um.start(tmp, filename)
    except Exception as e:
        try:
            os.remove(tmp)
        except Exception:
            pass
        return jsonify({"ok": False, "error": str(e)}), 500

    return jsonify({"ok": True, "version": version, "filename": filename})


@app.route("/api/upgrade_status", methods=["POST"])
def api_upgrade_status():
    """查询升级进度状态。"""
    sid = (request.json or {}).get("session_id", "")
    session = store.get(sid)
    if not session:
        return jsonify({"ok": False, "error": "未连接"}), 400
    um = getattr(session, "upgrade_manager", None)
    if um is None:
        return jsonify({"ok": False, "error": "升级管理器不可用"}), 400
    return jsonify({"ok": True, "status": um.status()})


# ============================================================
# 抓包路由
# ============================================================
@app.route("/api/capture_save", methods=["POST"])
def api_capture_save():
    d = request.json or {}
    sid = d.get("session_id", "")
    session = store.get(sid)
    if not session or not session.connected:
        return jsonify({"ok": False, "error": "未连接"}), 400
    try:
        name = session.capture_manager.save(
            addr=d.get("addr", ""),
            port=d.get("port", ""),
            size_mb=int(d.get("size_mb", config.CAPTURE_DEFAULT_SIZE_MB) or config.CAPTURE_DEFAULT_SIZE_MB),
            time_min=int(d.get("time_min", config.CAPTURE_DEFAULT_TIME_MIN) or config.CAPTURE_DEFAULT_TIME_MIN),
        )
        return jsonify({"ok": True, "name": name})
    except Exception as e:
        logger.error("capture_save failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/capture_start", methods=["POST"])
def api_capture_start():
    d = request.json or {}
    sid = d.get("session_id", "")
    session = store.get(sid)
    if not session or not session.connected:
        return jsonify({"ok": False, "error": "未连接"}), 400
    try:
        name = session.capture_manager.start(
            name=(d.get("name") or "").strip() or None,
            addr=d.get("addr", ""),
            port=d.get("port", ""),
            size_mb=int(d.get("size_mb", config.CAPTURE_DEFAULT_SIZE_MB) or config.CAPTURE_DEFAULT_SIZE_MB),
            time_min=int(d.get("time_min", config.CAPTURE_DEFAULT_TIME_MIN) or config.CAPTURE_DEFAULT_TIME_MIN),
        )
        return jsonify({"ok": True, "name": name})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        logger.error("capture_start failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/capture_list", methods=["POST"])
def api_capture_list():
    d = request.json or {}
    sid = d.get("session_id", "")
    session = store.get(sid)
    if not session or not session.connected:
        return jsonify({"ok": False, "error": "未连接"}), 400
    try:
        recs = session.capture_manager.list()
        return jsonify({"ok": True, "captures": recs})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/capture_end", methods=["POST"])
def api_capture_end():
    d = request.json or {}
    sid = d.get("session_id", "")
    name = (d.get("name") or "").strip()
    session = store.get(sid)
    if not session or not session.connected:
        return jsonify({"ok": False, "error": "未连接"}), 400
    try:
        session.capture_manager.end(name)
        return jsonify({"ok": True})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/capture_delete", methods=["POST"])
def api_capture_delete():
    d = request.json or {}
    sid = d.get("session_id", "")
    name = (d.get("name") or "").strip()
    session = store.get(sid)
    if not session or not session.connected:
        return jsonify({"ok": False, "error": "未连接"}), 400
    try:
        session.capture_manager.delete(name)
        return jsonify({"ok": True})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/capture_download", methods=["POST"])
def api_capture_download():
    d = request.json or {}
    sid = d.get("session_id", "")
    name = (d.get("name") or "").strip()
    session = store.get(sid)
    if not session or not session.connected:
        return jsonify({"ok": False, "error": "未连接"}), 400
    try:
        fpath = session.capture_manager.download_path(name)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    tmp = os.path.join(tempfile.gettempdir(), f"cap_{uuid.uuid4().hex}.pcap")
    try:
        session.file_manager.download(fpath, tmp)
        return send_file(tmp, as_attachment=True, download_name=name)
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "抓包文件不存在（可能已删除）"}), 404
    except Exception as e:
        return jsonify({"ok": False, "error": f"下载失败: {e}"}), 500
    finally:
        def _cleanup():
            time.sleep(5)
            try:
                os.remove(tmp)
            except Exception:
                pass
        threading.Thread(target=_cleanup, daemon=True).start()


# ============================================================
# 自动更新（ParkTerm 自身）
# ============================================================
@app.route("/api/check_update")
def api_check_update():
    """检查更新：从 GitHub 拉取 version.json，比较版本号。"""
    try:
        result = app_updater.check()
        return jsonify({"ok": True, **result})
    except Exception as e:
        logger.error("check_update error: %s", e, exc_info=True)
        return jsonify({"ok": False, "error": str(e),
                        "currentVersion": config.VERSION}), 500


@app.route("/api/start_update", methods=["POST"])
def api_start_update():
    """启动下载：开始后台下载新 exe。"""
    d = request.json or {}
    url = d.get("url", "")
    if not url:
        # 尝试使用上次 check 缓存的 url
        st = app_updater.status()
        url = st.get("url", "")
    if not url:
        return jsonify({"ok": False, "error": "缺少下载地址"}), 400
    if app_updater.is_running():
        return jsonify({"ok": False, "error": "已有更新任务正在进行中"}), 409
    try:
        app_updater.start_download(url)
        return jsonify({"ok": True})
    except Exception as e:
        logger.error("start_update error: %s", e, exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/update_status")
def api_update_status():
    """查询更新状态：返回当前阶段、进度百分比等。"""
    return jsonify({"ok": True, **app_updater.status()})


@app.route("/api/apply_update", methods=["POST"])
def api_apply_update():
    """应用更新：生成 batch 脚本，启动脚本后进程退出。"""
    try:
        result = app_updater.apply()
        if result.get("success"):
            logger.info("apply_update success, process will exit for restart")
        return jsonify({"ok": result.get("success", False),
                        "message": result.get("message", "")})
    except Exception as e:
        logger.error("apply_update error: %s", e, exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500


# ============================================================
# SocketIO 事件
# ============================================================
@socketio.on("connect")
def sio_connect():
    pass


@socketio.on("term-init")
def sio_term_init(data):
    session_id = data.get("session_id", "")
    session = store.get(session_id)
    if not session or not session.connected:
        emit("term-error", "会话不存在或未连接")
        return
    join_room(request.sid)
    # 重连场景：socket_id 可能已变更，重新关联并恢复流式推送
    session.socket_id = request.sid
    session.start_streaming(request.sid, socketio)
    emit("term-ready", {"ok": True})


@socketio.on("term-input")
def sio_term_input(data):
    session = store.get_by_socket(request.sid)
    if session and session.connected:
        try:
            session.send(data.get("data", ""))
        except Exception:
            pass


@socketio.on("term-resize")
def sio_term_resize(data):
    session = store.get_by_socket(request.sid)
    if session and session.connected:
        cols = data.get("cols", config.TERM_DEFAULT_COLS)
        rows = data.get("rows", config.TERM_DEFAULT_ROWS)
        session.resize(cols, rows)


@socketio.on("disconnect")
def sio_disconnect():
    # Socket.IO 断连时仅解除 socket 关联，不关闭 SSH 连接。
    # 给浏览器标签页切换/网络抖动留出重连窗口（SESSION_SOCKET_GRACE 秒）。
    # 真正的资源清理由 cleanup_stale() 在 grace 超时后执行。
    session = store.get_by_socket(request.sid)
    if session and session.connected:
        # 停止抓包任务（抓包依赖 socket 推送，断连后无法继续）
        try:
            cm = getattr(session, "capture_manager", None)
            if cm is not None:
                with cm._lock:
                    for name, st in list(cm._captures.items()):
                        if st.get("running") and st.get("pid"):
                            try:
                                session.run_command(f"kill -TERM {st['pid']} 2>/dev/null; true", timeout=6)
                            except Exception:
                                pass
                        st["running"] = False
                        st["ended"] = st.get("ended") or time.time()
                    cm._invalidate()
        except Exception:
            pass
        # 解除 socket 关联，但保持 SSH 连接活跃
        session.detach_socket()
        logger.info("Socket disconnected, SSH session kept alive (grace=%ds)", config.SESSION_SOCKET_GRACE)
    # 不再调用 store.remove_by_socket()，由 cleanup_stale() 处理超时清理


@socketio.on("heartbeat")
def sio_heartbeat():
    """前端心跳：更新 last_activity，保持会话活跃。"""
    session = store.get_by_socket(request.sid)
    if session:
        session.last_activity = time.time()
