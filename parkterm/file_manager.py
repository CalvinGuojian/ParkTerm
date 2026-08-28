"""
文件管理模块：SFTP / SCP / base64 三级降级文件传输与目录操作。

针对宇视 VMS 网关等 BusyBox 设备的常见限制：
  - 无 SFTP 子系统 → 回退 SCP
  - SCP 也不可用 → 回退 base64-over-exec
  - 交互式 shell 是受限 CLI（ls 返回 "Not Support Cmd Yet!"）
    → 目录列表优先用 exec 通道（直达真实 BusyBox shell）

所有 shell 命令拼接统一使用 shlex.quote()，防止命令注入。
"""
import base64
import logging
import os
import re
import shlex
import stat
import threading
import time

from . import config

logger = logging.getLogger("parkterm")

# SCP 可选依赖
try:
    from scp import SCPClient
    _HAS_SCP = True
except Exception:
    SCPClient = None
    _HAS_SCP = False


def _quote(path):
    """shell 安全转义路径。"""
    return shlex.quote(path)


class FileManager:
    """绑定到一个 SSHSession 的文件操作管理器。"""

    def __init__(self, session, socketio=None):
        self.session = session
        self.socketio = socketio
        self._last_ls_method = None

    # ============================================================
    # 进度回调
    # ============================================================
    def _emit_progress(self, transfer_id, transferred, total):
        if transfer_id and self.session.socket_id and self.socketio:
            total = total or 0
            pct = int(transferred * 100 / total) if total > 0 else 0
            self.socketio.emit("transfer-progress", {
                "transfer_id": transfer_id,
                "transferred": transferred,
                "total": total,
                "percent": pct,
            }, room=self.session.socket_id)

    # ============================================================
    # 上传：SFTP → SCP → base64
    # ============================================================
    def upload(self, local_path, remote_path, transfer_id=None):
        try:
            self._sftp_upload(local_path, remote_path, transfer_id)
            return
        except Exception as e:
            logger.warning("upload: SFTP failed (%s); trying SCP", e)
        try:
            self._scp_put(local_path, remote_path, transfer_id)
            return
        except Exception as e:
            logger.warning("upload: SCP failed (%s); trying base64", e)
        self._exec_upload_base64(local_path, remote_path, transfer_id)

    def _sftp_upload(self, local_path, remote_path, transfer_id):
        sftp = self.session.get_sftp()
        try:
            try:
                sftp.get_channel().settimeout(config.FILE_SFTP_CHANNEL_TIMEOUT)
            except Exception:
                pass
            def cb(sent, total_size):
                self._emit_progress(transfer_id, sent, total_size)
            sftp.put(local_path, remote_path, callback=cb)
        finally:
            try:
                sftp.close()
            except Exception:
                pass

    # ============================================================
    # 下载：SFTP → SCP → base64
    # ============================================================
    def download(self, remote_path, local_path, transfer_id=None):
        try:
            size = self._sftp_download(remote_path, local_path, transfer_id)
            return size
        except Exception as e:
            logger.warning("download: SFTP failed (%s); trying SCP", e)
        try:
            self._scp_get(remote_path, local_path, transfer_id)
            return os.path.getsize(local_path)
        except Exception as e:
            logger.warning("download: SCP failed (%s); trying base64", e)
        return self._exec_download_base64(remote_path, local_path, transfer_id)

    def _sftp_download(self, remote_path, local_path, transfer_id):
        sftp = self.session.get_sftp()
        try:
            try:
                sftp.get_channel().settimeout(config.FILE_SFTP_CHANNEL_TIMEOUT)
            except Exception:
                pass
            remote_size = sftp.stat(remote_path).st_size or 0
            def cb(transferred, total):
                self._emit_progress(transfer_id, transferred, total)
            sftp.get(remote_path, local_path, callback=cb)
            return remote_size
        finally:
            try:
                sftp.close()
            except Exception:
                pass

    # ============================================================
    # 目录列表：SFTP → exec → shell 三级
    # ============================================================
    def listdir(self, remote_path="."):
        """列出目录内容，三级降级。每级都有硬超时，防止请求挂死。

        Raises RuntimeError with combined message if all tiers fail.
        Raises RuntimeError('unsupported:...') for restricted level-1 backend.
        """
        last_err = None
        self._last_ls_method = None

        # Tier 1 — SFTP 子系统（带 watchdog）
        try:
            items = self._sftp_listdir_timed(remote_path, timeout=config.FILE_SFTP_LISTDIR_TIMEOUT)
            if items is not None:
                logger.info("listdir: SFTP tier OK (%d items)", len(items))
                self._last_ls_method = "sftp"
                return items
        except Exception as e:
            last_err = f"SFTP子系统: {e}"
            logger.warning("listdir: SFTP tier failed: %s", e)

        # Tier 2 — exec 通道（绕过受限交互 CLI）
        try:
            items = self._exec_listdir(remote_path)
            if items is not None:
                logger.info("listdir: exec tier OK (%d items)", len(items))
                self._last_ls_method = "exec"
                return items
        except Exception as e:
            last_err = f"exec ls: {e}"
            logger.warning("listdir: exec tier failed: %s", e)

        # Tier 3 — 交互 shell ls（最后手段）
        try:
            items = self._shell_listdir(remote_path)
            logger.info("listdir: shell tier OK (%d items)", len(items))
            self._last_ls_method = "shell"
            return items
        except RuntimeError as e:
            if str(e) == "unsupported":
                raise RuntimeError(
                    "unsupported:当前环境不支持文件列表（多为后台一级受限环境），"
                    "请点击工具栏\"进入二级\"后再试"
                )
            last_err = f"交互shell ls: {e}"
            logger.warning("listdir: shell tier failed: %s", e)
        except Exception as e:
            last_err = f"交互shell ls: {e}"
            logger.warning("listdir: shell tier failed: %s", e)

        raise RuntimeError(f"文件列表获取失败：{last_err or '未知错误'}")

    def _sftp_listdir_timed(self, remote_path, timeout=15):
        """带 watchdog 线程的 SFTP listdir，防止服务器接受子系统但不响应。"""
        result = {}

        def _worker():
            try:
                sftp = self.session.get_sftp()
                try:
                    try:
                        sftp.get_channel().settimeout(timeout)
                    except Exception:
                        pass
                    items = []
                    for attr in sftp.listdir_attr(remote_path):
                        is_dir = stat.S_ISDIR(attr.st_mode) if attr.st_mode else False
                        items.append({
                            "name": attr.filename,
                            "size": attr.st_size if attr.st_size is not None else 0,
                            "mtime": int(attr.st_mtime) if attr.st_mtime else 0,
                            "is_dir": is_dir,
                            "is_link": stat.S_ISLNK(attr.st_mode) if attr.st_mode else False,
                            "mode": stat.filemode(attr.st_mode) if attr.st_mode else "----------",
                        })
                    items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
                    result["items"] = items
                finally:
                    try:
                        sftp.close()
                    except Exception:
                        pass
            except Exception as e:
                result["error"] = e

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        t.join(timeout + 3)
        if "items" in result:
            return result["items"]
        if "error" in result:
            raise result["error"]
        raise RuntimeError("SFTP子系统响应超时（可能该设备不支持SFTP），已自动切换其他方式")

    def _exec_listdir(self, remote_path):
        """通过 exec 通道列目录。优先 GNU --time-style（精确 mtime），回退 BusyBox。"""
        try:
            transport = self.session.get_fs_client().get_transport()
        except Exception:
            transport = None
        if not transport:
            return None

        safe_path = _quote(remote_path)
        candidates = [
            f"ls -la --time-style=+%s {safe_path}",
            f"ls -la {safe_path}",
            f"ls -l {safe_path}",
        ]
        last_err = None
        for cmd in candidates:
            try:
                stdout, stderr = self.session.exec_capture(cmd, timeout=config.FILE_EXEC_TIMEOUT)
            except Exception as e:
                last_err = e
                logger.debug("_exec_listdir: '%s' raised %s", cmd, e)
                continue
            logger.debug("_exec_listdir stdout: %r", stdout[:300])
            if stderr:
                logger.debug("_exec_listdir stderr: %r", stderr[:200])
            if (not stdout.strip()) and re.search(
                r"unrecognized option|invalid option|BusyBox|Usage:", stderr, re.I
            ):
                logger.debug("_exec_listdir: '%s' rejected by shell, trying simpler form", cmd)
                continue
            if "No such file or directory" in stdout or "No such file or directory" in stderr:
                raise FileNotFoundError(f"路径不存在: {remote_path}")
            if "Not Support Cmd Yet!" in stdout:
                raise RuntimeError("unsupported")
            if stdout.strip():
                return self._parse_ls_output(stdout)
            last_err = "empty output"
        logger.debug("_exec_listdir: all candidates failed (last_err=%s)", last_err)
        return None

    def _shell_listdir(self, remote_path):
        """最后手段：通过交互 PTY shell 列目录。"""
        cmd = "ls -la --time-style=+%s " + _quote(remote_path)
        out = self.session.run_command(cmd, timeout=8)
        logger.debug("_shell_listdir '%s': %r", cmd, out[:300])
        if "Not Support Cmd Yet!" in out or not out.strip():
            raise RuntimeError("unsupported")
        return self._parse_ls_output(out)

    # ============================================================
    # ls 输出解析（GNU + BusyBox 两种格式）
    # ============================================================
    @staticmethod
    def _parse_ls_output(text):
        """解析 `ls -la` 输出为 item 列表。

        支持两种格式：
          GNU --time-style=+%s : 第6字段是 epoch 秒
          BusyBox / 默认        : mon day time [year] name，mtime 未知
        """
        items = []
        for raw in text.split("\n"):
            line = raw.rstrip("\r").strip()
            if not line or line.startswith("total "):
                continue
            parts = line.split()
            if len(parts) < 5:
                continue
            mode_str = parts[0]
            if not (mode_str[0] in "bcdlsp-"):
                continue
            is_dir = mode_str.startswith("d")
            is_link = mode_str.startswith("l")
            try:
                size = int(parts[4])
            except (ValueError, IndexError):
                size = 0
            mtime = 0
            if len(parts) >= 6 and parts[5].isdigit():
                try:
                    mtime = int(parts[5])
                except ValueError:
                    mtime = 0
                name = " ".join(parts[6:])
            else:
                name = " ".join(parts[8:]) if len(parts) >= 9 else " ".join(parts[5:])
            name = name.strip()
            if not name or name in (".", ".."):
                continue
            if is_link and " -> " in name:
                name = name.split(" -> ")[0]
            items.append({
                "name": name,
                "size": size,
                "mtime": mtime,
                "is_dir": is_dir,
                "is_link": is_link,
                "mode": mode_str,
            })
        items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
        return items

    # ============================================================
    # 删除 / 创建目录：SFTP → exec → shell
    # ============================================================
    def delete(self, remote_path):
        # Tier 1: SFTP
        try:
            sftp = self.session.get_sftp()
            try:
                try:
                    sftp.get_channel().settimeout(config.FILE_DELETE_TIMEOUT)
                except Exception:
                    pass
                try:
                    sftp.remove(remote_path)
                except IOError:
                    sftp.rmdir(remote_path)
                return
            finally:
                sftp.close()
        except Exception as e:
            logger.debug("delete: SFTP failed (%s), trying exec channel", e)
        # Tier 2: exec 通道（已修复注入：使用 shlex.quote）
        if self.session.exec_run(f"rm -rf {_quote(remote_path)}"):
            return
        # Tier 3: 交互 shell（已修复注入）
        self.session.run_command("rm -rf " + _quote(remote_path), timeout=8)

    def mkdir(self, remote_path):
        try:
            sftp = self.session.get_sftp()
            try:
                try:
                    sftp.get_channel().settimeout(config.FILE_MKDIR_TIMEOUT)
                except Exception:
                    pass
                sftp.mkdir(remote_path)
                return
            finally:
                sftp.close()
        except Exception as e:
            logger.debug("mkdir: SFTP failed (%s), trying exec channel", e)
        if self.session.exec_run(f"mkdir -p {_quote(remote_path)}"):
            return
        self.session.run_command("mkdir -p " + _quote(remote_path), timeout=8)

    # ============================================================
    # SCP 传输回退
    # ============================================================
    def _scp_put(self, local_path, remote_path, transfer_id=None):
        if not _HAS_SCP:
            raise RuntimeError("SCP 组件不可用（请确认依赖已安装）")
        transport = self.session.cmd_transport()
        if not transport:
            raise RuntimeError("无可用文件连接")

        def progress(filename, size, sent):
            self._emit_progress(transfer_id, sent, size)

        recursive = os.path.isdir(local_path)
        with SCPClient(transport, progress=progress) as scp:
            scp.put(local_path, remote_path, recursive=recursive)

    def _scp_get(self, remote_path, local_path, transfer_id=None):
        if not _HAS_SCP:
            raise RuntimeError("SCP 组件不可用（请确认依赖已安装）")
        transport = self.session.cmd_transport()
        if not transport:
            raise RuntimeError("无可用文件连接")

        def progress(filename, size, sent):
            self._emit_progress(transfer_id, sent, size)

        local_dir = os.path.dirname(local_path) or "."
        os.makedirs(local_dir, exist_ok=True)
        want_name = os.path.basename(local_path)
        remote_name = os.path.basename(remote_path.rstrip("/")) or "download.bin"
        try:
            with SCPClient(transport, progress=progress) as scp:
                scp.get(remote_path, local_dir, recursive=False)
        except Exception:
            with SCPClient(transport, progress=progress) as scp:
                scp.get(remote_path, local_dir, recursive=True)
        downloaded = os.path.join(local_dir, remote_name)
        if want_name and want_name != remote_name and os.path.exists(downloaded):
            target = os.path.join(local_dir, want_name)
            if os.path.exists(target):
                os.remove(target)
            os.rename(downloaded, target)
            downloaded = target
        if not os.path.exists(downloaded):
            raise RuntimeError("SCP 下载完成但未找到文件")

    # ============================================================
    # base64-over-exec 传输回退（最后手段）
    # ============================================================
    def _exec_upload_base64(self, local_path, remote_path, transfer_id=None):
        """分块上传：避免大文件一次性加载到内存。"""
        transport = self.session.cmd_transport()
        if not transport:
            raise RuntimeError("无可用文件连接")
        total = os.path.getsize(local_path)
        if total > config.BASE64_UPLOAD_WARN_SIZE:
            logger.warning("_exec_upload_base64: %d bytes > 30MB, may be slow", total)

        chan = transport.open_session()
        chan.settimeout(300)  # 增大超时以适应大文件
        chan.exec_command(f"base64 -d > {_quote(remote_path)}")

        # 分块读取并编码传输，避免内存溢出
        chunk_size = 64 * 1024  # 64KB 原始数据块
        step = config.BASE64_UPLOAD_CHUNK
        sent_bytes = 0
        try:
            with open(local_path, "rb") as f:
                while True:
                    raw_chunk = f.read(chunk_size)
                    if not raw_chunk:
                        break
                    b64_chunk = base64.b64encode(raw_chunk)
                    chan.sendall(b64_chunk)
                    sent_bytes += len(raw_chunk)
                    self._emit_progress(transfer_id, sent_bytes, total)
            chan.shutdown_write()
        finally:
            chan.close()
        self._emit_progress(transfer_id, total, total)

    def _exec_download_base64(self, remote_path, local_path, transfer_id=None):
        """分块下载：边接收边解码写入文件，避免大文件占用大量内存。"""
        transport = self.session.cmd_transport()
        if not transport:
            raise RuntimeError("无可用文件连接")

        chan = transport.open_session()
        chan.settimeout(config.BASE64_DOWNLOAD_TIMEOUT)
        chan.exec_command(f"base64 {_quote(remote_path)}")

        # 边接收边解码，避免大文件内存溢出
        local_dir = os.path.dirname(local_path) or "."
        os.makedirs(local_dir, exist_ok=True)

        received_bytes = 0
        with open(local_path, "wb") as f:
            buffer = b""
            while True:
                chunk = chan.recv(65536)
                if not chunk:
                    break
                buffer += chunk
                received_bytes += len(chunk)

                # 当缓冲区积累足够数据时批量解码（约 256KB 原始数据对应约 340KB base64）
                while len(buffer) >= 340000:
                    # 找到最近的换行符位置进行对齐
                    nl_pos = buffer.rfind(b"\n") + 1
                    if nl_pos > 0:
                        decode_chunk = base64.b64decode(buffer[:nl_pos])
                        f.write(decode_chunk)
                        buffer = buffer[nl_pos:]
                    else:
                        break

            # 处理剩余的缓冲区数据
            if buffer:
                clean_buffer = buffer.replace(b"\r", b"").replace(b"\n", b"")
                if clean_buffer:
                    decode_chunk = base64.b64decode(clean_buffer)
                    f.write(decode_chunk)
                    received_bytes += len(decode_chunk)

        chan.close()
        self._emit_progress(transfer_id, received_bytes, received_bytes)
        return received_bytes
