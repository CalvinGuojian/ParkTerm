"""
VMS-U 数据库备份模块。

功能：
  - 列出 /Config_DB 目录下的备份文件（.tar.gz），支持下载
  - 手动备份：同步拷贝内置 ums_db_backup.sh 到 /Config_DB 并 chmod 777，
    后台执行并轮询进度，完成后前端自动刷新文件列表
  - 不支持删除备份文件（前端无删除入口，后端也不提供）

备份脚本输出末行为：备份的数据库包路径: /Config_DB/xxx.tar.gz
以此为成功标志。
"""
import logging
import re
import stat
import threading
import time

from . import config

logger = logging.getLogger("parkterm")

# 内置数据库备份脚本（同步拷贝到 /Config_DB 后执行）
BACKUP_SCRIPT = """#!/bin/bash
mkdir -p /Config_DB/backupManual
sh /program/bin/backup_all_db_manual.sh backupManual && version=$(grep -oP '\\d+\\.\\d+\\.\\d+\\.P\\d+' /program/version 2>/dev/null | head -1) && version_file=/Config_DB/backupManual/version.xml && build_date=$(date +'%Y%m%d%H%M%S') && model_name=$(update -v | grep BOARD_TYPE | awk -F: '{print $2}') && cat > "$version_file" <<<"<versionInfo><version>${version}</version><model>${model_name}</model><buildDate>${build_date}</buildDate></versionInfo>" && sh /program/bin/redis_backup.sh /Config_DB "${version}_${build_date}_2.tar.gz" backupManual && echo "备份的数据库包路径: /Config_DB/${version}_${build_date}_2.tar.gz"
"""

# 备份成功标志（脚本末行输出）
_BACKUP_DONE_MARKER = "备份的数据库包路径"
# 备份结果文件路径前缀
_BACKUP_PATH_PREFIX = "/Config_DB/"


class BackupManager:
    """单个 SSH 会话的数据库备份管理器（VMS-U 专用）。"""

    def __init__(self, session):
        self.session = session
        self._lock = threading.Lock()
        # 状态：stage ∈ idle/preparing/running/done/error
        self._state = {
            "stage": "idle",
            "error": "",
            "started": None,
            "finished": None,
            "result_file": "",
            "log_tail": "",
        }
        self._thread = None

    # ============================================================
    # 状态
    # ============================================================
    def status(self):
        with self._lock:
            return dict(self._state)

    def _set(self, **kw):
        with self._lock:
            self._state.update(kw)
            logger.info("backup state: %s", self._state)

    def is_running(self):
        st = self.status()
        return st["stage"] not in ("idle", "done", "error")

    # ============================================================
    # 文件列表
    # ============================================================
    def list_files(self):
        """列出 /Config_DB 下的 .tar.gz 备份文件：[{name, size, mtime}]"""
        files = []
        try:
            sftp = self.session.get_sftp()
            try:
                try:
                    sftp.get_channel().settimeout(15)
                except Exception:
                    pass
                for attr in sftp.listdir_attr(config.BACKUP_DIR):
                    if attr.filename.endswith(".tar.gz"):
                        files.append({
                            "name": attr.filename,
                            "size": attr.st_size or 0,
                            "mtime": int(attr.st_mtime or 0),
                        })
            finally:
                try:
                    sftp.close()
                except Exception:
                    pass
        except Exception as e:
            logger.warning("backup list_files SFTP failed: %s; trying shell", e)
            # 回退：shell ls 解析
            try:
                out = self.session.run_command(
                    f"ls -la --time-style=+%s {config.BACKUP_DIR}/*.tar.gz 2>/dev/null",
                    timeout=10)
                for line in out.split("\n"):
                    parts = line.split()
                    if len(parts) >= 7 and parts[0].startswith("-"):
                        try:
                            size = int(parts[4])
                            mtime = int(parts[5])
                        except ValueError:
                            continue
                        name = " ".join(parts[6:])
                        if name.endswith(".tar.gz"):
                            files.append({"name": name, "size": size, "mtime": mtime})
            except Exception as e2:
                logger.warning("backup list_files shell failed: %s", e2)
                raise RuntimeError(f"备份文件列表获取失败: {e2}")
        files.sort(key=lambda f: f["mtime"], reverse=True)
        return files

    # ============================================================
    # 手动备份
    # ============================================================
    def start(self):
        if self.is_running():
            raise RuntimeError("已有备份任务正在进行中")

        self._set(stage="preparing", error="", result_file="",
                  started=time.time(), finished=None)

        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        try:
            self._sync_script()
            result_file = self._execute()
            self._set(stage="done", result_file=result_file, finished=time.time())
            logger.info("Backup completed: %s", result_file)
        except Exception as e:
            logger.error("Backup failed: %s", e, exc_info=True)
            self._set(stage="error", error=str(e), finished=time.time())

    # ------------------------------------------------------------
    # 同步脚本到 /Config_DB + chmod 777
    # ------------------------------------------------------------
    def _sync_script(self):
        script_remote = f"{config.BACKUP_DIR}/ums_db_backup.sh"
        # 写入脚本内容（本地写临时文件 → SFTP 上传）
        import os
        import tempfile
        import uuid
        tmp = os.path.join(tempfile.gettempdir(), f"ums_db_backup_{uuid.uuid4().hex}.sh")
        # 必须 UTF-8 编码写入：设备 bash 按 UTF-8 回显脚本内容，
        # GBK 编码会导致中文成功标志变乱码无法匹配
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(BACKUP_SCRIPT)
        try:
            try:
                sftp = self.session.get_sftp()
                try:
                    try:
                        sftp.get_channel().settimeout(30)
                    except Exception:
                        pass
                    sftp.put(tmp, script_remote)
                    # chmod 777
                    try:
                        sftp.chmod(script_remote, 0o777)
                    except Exception:
                        pass
                finally:
                    try:
                        sftp.close()
                    except Exception:
                        pass
                logger.info("backup script synced to %s (chmod 777)", script_remote)
            except Exception as e:
                raise RuntimeError(f"备份脚本上传失败: {e}")
            # chmod 兜底（SFTP chmod 不可用时）
            out = self._run_cmd(f"chmod 777 {script_remote}", timeout=10)
            if "Not Support Cmd Yet!" in out:
                raise RuntimeError("当前环境不支持 chmod（一级受限），请先进入二级后重试")
        finally:
            try:
                os.remove(tmp)
            except Exception:
                pass

    # ------------------------------------------------------------
    # 执行备份脚本并轮询
    # ------------------------------------------------------------
    def _execute(self):
        self._set(stage="running")
        log_file = f"{config.BACKUP_DIR}/.parkterm_backup.log"
        pid_file = f"{config.BACKUP_DIR}/.parkterm_backup.pid"

        out = self._run_cmd(
            f"rm -f {log_file} {pid_file}; cd {config.BACKUP_DIR} && "
            f"nohup sh ums_db_backup.sh > {log_file} 2>&1 & "
            f"echo $! > {pid_file}",
            timeout=15)
        if "Not Support Cmd Yet!" in out:
            raise RuntimeError("当前环境不支持备份命令（一级受限），请先进入二级后重试")

        t0 = time.time()
        no_pid_rounds = 0
        while time.time() - t0 < config.BACKUP_TIMEOUT:
            time.sleep(config.BACKUP_POLL_INTERVAL)
            # 读取日志尾部展示进度
            tail = self._run_cmd(f"tail -5 {log_file} 2>/dev/null", timeout=10)
            if tail:
                self._set(log_tail=tail.strip()[-300:])
            # 进程存活检查
            pid_out = self._run_cmd(f"cat {pid_file} 2>/dev/null", timeout=10)
            pid_str = ""
            for tok in reversed((pid_out or "").split()):
                if tok.isdigit():
                    pid_str = tok
                    break
            if pid_str:
                no_pid_rounds = 0
                alive = self._run_cmd(
                    f"kill -0 {pid_str} 2>/dev/null; echo RC=$?", timeout=6)
                if "RC=0" in alive:
                    continue  # 仍在备份中
                # 进程退出 → 读取完整日志判断结果
                log = self._run_cmd(f"cat {log_file} 2>/dev/null", timeout=10)
                self._run_cmd(f"rm -f {log_file} {pid_file}", timeout=10)
                if _BACKUP_DONE_MARKER in log:
                    # 提取结果文件名（去除终端折行引入的 \r\n 与空白）
                    m = re.search(
                        r"备份的数据库包路径[:：]\s*(\S+)", log)
                    result = m.group(1) if m else ""
                    result = re.sub(r"[\r\n\s]+", "", result)
                    return result
                # 兜底：日志中出现 /Config_DB/*.tar.gz 路径也视为成功
                m2 = re.search(r"(/Config_DB/\S*\.tar\.gz)", log)
                if m2:
                    result = re.sub(r"[\r\n\s]+", "", m2.group(1))
                    return result
                # 无成功标志 → 失败
                raise RuntimeError(f"备份脚本执行失败: {log[-300:]}")
            else:
                no_pid_rounds += 1
                if no_pid_rounds >= 5:
                    raise RuntimeError("无法获取备份进程状态（命令被拦截），请确认已进入二级环境")
        raise TimeoutError(f"备份超时（>{config.BACKUP_TIMEOUT}s）")

    # ------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------
    def _run_cmd(self, cmd, timeout=10):
        try:
            return self.session.run_command(cmd, timeout=timeout) or ""
        except Exception as e:
            logger.debug("backup cmd '%s' error: %s", cmd, e)
            return ""
