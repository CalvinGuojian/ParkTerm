"""
VMS-B runtime 清理模块。

流程（后台线程执行，前端轮询 /api/runtime_clean_status 获取进度）：
  1. check   : 执行 df -h 检查 /var/runtime 空间占用百分比
  2. upload  : 根据设备型号（B2xx→/var/coredump, B5xx→/var/fs_disk）上传两个脚本
  3. chmod   : chmod 777 clean_runtime_new.sh db_inout_new.sh
  4. running : 后台执行 sh clean_runtime_new.sh，轮询日志
  5. done    : 清理完成

脚本内置空间检查逻辑（check_runtime_space），此处仅做前置 df -h 预判，
实际清理阈值由脚本内部根据设备型号决定。
"""
import logging
import os
import re
import threading
import time

from . import config

logger = logging.getLogger("parkterm")

# 脚本远程文件名
SCRIPT_CLEAN = "clean_runtime_new.sh"
SCRIPT_DBINOUT = "db_inout_new.sh"

# 清理日志远程路径
CLEAN_LOG_REMOTE = "/var/runtime/manual_clean_runtime.log"

# 清理超时（秒）：脚本可能执行 VACUUM FULL + 导出导入 + reboot，给足时间
RUNTIME_CLEAN_TIMEOUT = 3600
# 进度轮询间隔（秒）
RUNTIME_CLEAN_POLL_INTERVAL = 2.0


class RuntimeCleanManager:
    """单个 SSH 会话的 runtime 清理管理器（VMS-B 专用）。"""

    def __init__(self, session):
        self.session = session
        self._lock = threading.Lock()
        self._state = {
            "stage": "idle",       # idle/checking/uploading/chmod/running/done/error
            "percent": 0,
            "usage": "",           # df -h 解析出的 /var/runtime 占用百分比
            "need_clean": False,   # 是否需要清理
            "log_tail": "",        # 远程脚本日志尾部
            "error": "",
            "started": None,
            "finished": None,
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
            logger.info("runtime_clean state: %s", self._state)

    def is_running(self):
        st = self.status()
        return st["stage"] not in ("idle", "done", "error")

    # ============================================================
    # 空间检查（同步，前端等待结果）
    # ============================================================
    def check_space(self):
        """执行 df -h 检查 /var/runtime 占用百分比。

        返回 dict: {usage: "85%", need_clean: bool, detail: str}
        """
        self._set(stage="checking", error="", started=time.time(), finished=None)
        try:
            out = self._run_cmd("df -h /var/runtime 2>/dev/null", timeout=15)
            if "Not Support Cmd Yet!" in out:
                raise RuntimeError("当前环境不支持 df 命令（一级受限），请先进入二级后重试")

            # 解析 df -h 输出，提取 Use% 列
            # 示例输出:
            #   Filesystem                Size      Used Available Use% Mounted on
            #   /dev/sda3                 3.8G      3.1G      568M  85% /var/runtime
            usage_pct = None
            for line in out.strip().split("\n"):
                if "/var/runtime" in line:
                    parts = line.split()
                    for part in parts:
                        if part.endswith("%"):
                            try:
                                usage_pct = int(part.rstrip("%"))
                            except ValueError:
                                pass
                    break

            if usage_pct is None:
                # 尝试 df -m 方式（某些设备 df -h 输出格式不同）
                out2 = self._run_cmd("df -m /var/runtime 2>/dev/null", timeout=15)
                for line in out2.strip().split("\n"):
                    if "/var/runtime" in line:
                        parts = line.split()
                        if len(parts) >= 5:
                            try:
                                used = int(parts[2])
                                total = int(parts[1])
                                if total > 0:
                                    usage_pct = int(used * 100 / total)
                            except (ValueError, IndexError):
                                pass
                        break

            if usage_pct is None:
                raise RuntimeError("无法解析 /var/runtime 空间占用，请确认已进入二级环境")

            need_clean = usage_pct > 80
            self._set(usage=f"{usage_pct}%", need_clean=need_clean,
                      percent=0, stage="idle", finished=time.time())
            logger.info("runtime space check: %d%%, need_clean=%s", usage_pct, need_clean)
            return {
                "usage": f"{usage_pct}%",
                "need_clean": need_clean,
                "detail": f"/var/runtime 空间占用 {usage_pct}%，" +
                          ("超过80%，建议清理" if need_clean else "空间正常，无需清理"),
            }
        except Exception as e:
            logger.error("runtime check_space error: %s", e, exc_info=True)
            self._set(stage="error", error=str(e), finished=time.time())
            raise

    # ============================================================
    # 启动清理（后台线程）
    # ============================================================
    def start(self):
        if self.is_running():
            raise RuntimeError("已有清理任务正在进行中")

        self._set(stage="uploading", percent=0, error="",
                  log_tail="", started=time.time(), finished=None)

        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    # ============================================================
    # 主流程
    # ============================================================
    def _run(self):
        try:
            # 1. 检测设备型号，确定上传目录
            upload_dir = self._detect_upload_dir()
            logger.info("runtime clean: upload_dir=%s", upload_dir)

            # 2. 上传脚本
            self._upload_scripts(upload_dir)
            self._set(percent=30)

            # 3. chmod
            self._chmod_scripts(upload_dir)
            self._set(percent=40)

            # 4. 执行清理脚本
            self._execute_clean(upload_dir)

            self._set(stage="done", percent=100, finished=time.time())
            logger.info("Runtime clean completed")
        except Exception as e:
            logger.error("Runtime clean failed: %s", e, exc_info=True)
            self._set(stage="error", error=str(e), finished=time.time())

    # ------------------------------------------------------------
    # 检测设备型号，确定上传目录
    # ------------------------------------------------------------
    def _detect_upload_dir(self):
        """根据 /proc/board_type 判断 B2xx 还是 B5xx，返回上传目录。"""
        out = self._run_cmd("cat /proc/board_type 2>/dev/null", timeout=10)
        if "Not Support Cmd Yet!" in out:
            raise RuntimeError("当前环境不支持读取设备型号（一级受限），请先进入二级后重试")

        if "VMS-B500" in out or "VMS-B5" in out:
            upload_dir = "/var/fs_disk"
        else:
            # 默认 B2xx 系列
            upload_dir = "/var/coredump"

        # 确保目录存在
        mkdir_out = self._run_cmd(f"mkdir -p {upload_dir}", timeout=15)
        if "Not Support Cmd Yet!" in mkdir_out:
            raise RuntimeError(f"当前环境不支持创建目录（一级受限），请先进入二级后重试")

        # 校验目录
        chk = self._run_cmd(f"ls -d {upload_dir}", timeout=10)
        if upload_dir not in chk:
            raise RuntimeError(f"目录创建失败: {upload_dir}")

        return upload_dir

    # ------------------------------------------------------------
    # 上传脚本
    # ------------------------------------------------------------
    def _upload_scripts(self, upload_dir):
        """上传 clean_runtime_new.sh 和 db_inout_new.sh 到远程目录。"""
        self._set(stage="uploading", percent=10, log_tail="上传脚本中...")

        # 获取本地脚本路径（兼容 PyInstaller 打包）
        local_clean = config.resource_path(f"parkterm/{SCRIPT_CLEAN}")
        local_dbinout = config.resource_path(f"parkterm/{SCRIPT_DBINOUT}")

        if not os.path.isfile(local_clean):
            raise RuntimeError(f"本地脚本不存在: {local_clean}")
        if not os.path.isfile(local_dbinout):
            raise RuntimeError(f"本地脚本不存在: {local_dbinout}")

        remote_clean = f"{upload_dir}/{SCRIPT_CLEAN}"
        remote_dbinout = f"{upload_dir}/{SCRIPT_DBINOUT}"

        try:
            sftp = self.session.get_sftp()
            try:
                try:
                    sftp.get_channel().settimeout(60)
                except Exception:
                    pass
                # 上传清理脚本
                sftp.put(local_clean, remote_clean)
                self._set(percent=20, log_tail=f"已上传 {SCRIPT_CLEAN} → {remote_clean}")
                # 上传数据库脚本
                sftp.put(local_dbinout, remote_dbinout)
                self._set(percent=30, log_tail=f"已上传 {SCRIPT_DBINOUT} → {remote_dbinout}")
            finally:
                try:
                    sftp.close()
                except Exception:
                    pass
            logger.info("scripts uploaded to %s", upload_dir)
        except Exception as e:
            raise RuntimeError(f"脚本上传失败: {e}")

    # ------------------------------------------------------------
    # chmod
    # ------------------------------------------------------------
    def _chmod_scripts(self, upload_dir):
        """chmod 777 两个脚本。"""
        self._set(stage="chmod", percent=35, log_tail="授予执行权限...")

        remote_clean = f"{upload_dir}/{SCRIPT_CLEAN}"
        remote_dbinout = f"{upload_dir}/{SCRIPT_DBINOUT}"

        out = self._run_cmd(f"chmod 777 {remote_clean} {remote_dbinout}", timeout=15)
        if "Not Support Cmd Yet!" in out:
            raise RuntimeError("当前环境不支持 chmod（一级受限），请先进入二级后重试")

        logger.info("chmod 777 done for scripts")

    # ------------------------------------------------------------
    # 执行清理脚本并轮询
    # ------------------------------------------------------------
    def _execute_clean(self, upload_dir):
        """后台执行 sh clean_runtime_new.sh，轮询日志。"""
        self._set(stage="running", percent=40, log_tail="开始执行清理脚本...")

        remote_clean = f"{upload_dir}/{SCRIPT_CLEAN}"
        log_file = f"{upload_dir}/.parkterm_runtime_clean.log"
        pid_file = f"{upload_dir}/.parkterm_runtime_clean.pid"

        # 后台执行
        out = self._run_cmd(
            f"rm -f {log_file} {pid_file}; cd {upload_dir} && "
            f"nohup sh {SCRIPT_CLEAN} > {log_file} 2>&1 & "
            f"echo $! > {pid_file}",
            timeout=15)
        if "Not Support Cmd Yet!" in out:
            raise RuntimeError("当前环境不支持执行清理命令（一级受限），请先进入二级后重试")

        logger.info("clean script started, log=%s", log_file)

        t0 = time.time()
        no_pid_rounds = 0
        while time.time() - t0 < RUNTIME_CLEAN_TIMEOUT:
            time.sleep(RUNTIME_CLEAN_POLL_INTERVAL)

            # 读取日志尾部展示进度
            tail = self._run_cmd(f"tail -10 {log_file} 2>/dev/null", timeout=10)
            if tail:
                self._set(log_tail=tail.strip()[-500:])

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
                    # 仍在执行
                    continue
                # 进程退出 → 读取完整日志判断结果
                log = self._run_cmd(f"cat {log_file} 2>/dev/null", timeout=15)
                self._run_cmd(f"rm -f {log_file} {pid_file}", timeout=10)

                # 检查成功标志
                if "清理成功完成" in log or "reboot" in log.lower():
                    self._set(log_tail=log.strip()[-500:])
                    return
                # 检查失败标志
                if "脚本执行失败" in log or "ERROR" in log:
                    raise RuntimeError(f"清理脚本执行失败: {log[-400:]}")
                # 退出但无明确标志，检查退出码
                # 如果日志中有 "无需清理" 也算正常完成
                if "无需清理" in log:
                    self._set(log_tail=log.strip()[-500:])
                    return
                # 默认视为完成（脚本最后会 reboot，进程退出可能是因为 reboot）
                logger.info("clean script exited, checking log for result")
                self._set(log_tail=log.strip()[-500:])
                return
            else:
                no_pid_rounds += 1
                if no_pid_rounds >= 5:
                    raise RuntimeError("无法获取清理进程状态（命令被拦截），请确认已进入二级环境")

        raise TimeoutError(f"清理超时（>{RUNTIME_CLEAN_TIMEOUT}s），请检查设备状态")

    # ------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------
    def _run_cmd(self, cmd, timeout=10):
        try:
            return self.session.run_command(cmd, timeout=timeout) or ""
        except Exception as e:
            logger.warning("runtime_clean cmd '%s' error: %s", cmd, e)
            return ""
