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
import logging
import os
import tempfile
import threading
import time
import uuid

import paramiko
from flask import Flask, render_template, request, jsonify, send_file
from flask_socketio import SocketIO, emit, join_room

from . import config
from .ssh_session import SSHSession
from .file_manager import FileManager
from .capture import CaptureManager

logger = logging.getLogger("parkterm")

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

    def create(self, host, port, username, password):
        sid = str(uuid.uuid4())
        session = SSHSession(host, port, username, password)
        # 附加文件管理器与抓包管理器（按会话隔离）
        session.file_manager = FileManager(session, socketio)
        session.capture_manager = CaptureManager(session)
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

    def remove(self, sid):
        with self.lock:
            s = self.sessions.pop(sid, None)
        if s:
            s.close()

    def cleanup_stale(self, max_idle=None):
        if max_idle is None:
            max_idle = config.SESSION_IDLE_TIMEOUT
        now = time.time()
        stale = []
        with self.lock:
            for sid, s in self.sessions.items():
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
    return render_template("index.html")


@app.route("/api/connect", methods=["POST"])
def api_connect():
    d = request.json or {}
    host = (d.get("host") or "").strip()
    port = d.get("port", 23333)
    username = (d.get("username") or "").strip()
    password = d.get("password", "")
    if not host or not username:
        return jsonify({"ok": False, "error": "请填写主机地址和用户名"}), 400
    store.cleanup_stale()
    logger.info("Connect request: host=%s, port=%s, user=%s", host, port, username)
    try:
        sid, session = store.create(host, port, username, password)
        session.connect()
        return jsonify({"ok": True, "session_id": sid})
    except paramiko.AuthenticationException:
        logger.warning("Auth failed: %s:%s user=%s", host, port, username)
        return jsonify({"ok": False, "error": "认证失败：用户名或密码错误"}), 401
    except paramiko.SSHException as e:
        logger.error("SSH error: %s:%s -> %s", host, port, e)
        return jsonify({"ok": False, "error": f"SSH协议错误: {e}"}), 500
    except OSError as e:
        logger.error("Network error: %s:%s errno=%s -> %s", host, port, e.errno, e)
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
        return jsonify({"ok": True, "info": info})
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
        out = session.run_command_exec("ls", timeout=config.CMD_CHECK_LEVEL1_TIMEOUT)
        if not out:
            # exec channel 不可用时回退
            out = session.run_command("ls", timeout=config.CMD_CHECK_LEVEL1_TIMEOUT)
        logger.debug("check_level1 'ls' output: %r", out[:300])
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
    # 页面关闭时自动结束该会话所有正在运行的抓包任务
    session = store.get_by_socket(request.sid)
    if session and session.connected:
        try:
            cm = session.capture_manager
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
