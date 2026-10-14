"""
VMS-U 版本升级模块。

流程（后台线程执行，前端轮询 /api/upgrade_status 获取进度）：
  1. prepare  : 在 home 路径创建 upgradefiles 目录（存在则跳过）
  2. upload   : SFTP 上传软件包（tar.gz）至 ~/upgradefiles/，带进度
  3. extract  : tar -zxvf 解压，通过日志行数对比总行数计算进度
  4. upgrade  : 进入解压目录执行 ./update -upgrade，解析输出进度
  5. done     : 升级成功

软件包命名：VMS-B2101.1.3.9.P17.260720.tar.gz
待升级版本号从文件名解析：VMS-B2101.1.3.9.P17（去掉末尾 .日期码 与 .tar.gz）
"""
import logging
import os
import re
import threading
import time

from . import config

logger = logging.getLogger("parkterm")

# 升级输出中的百分比模式（如 "35%" / "progress: 35%" / "升级进度: 35%"）
_PCT_RE = re.compile(r"(\d{1,3})\s*%")


def _last_int(text):
    """从命令输出中提取最后一个整数（实际结果在残留 banner 之后）。"""
    nums = re.findall(r"(\d+)", text or "")
    return int(nums[-1]) if nums else None


def _last_pid(text):
    """从命令输出中提取最后一个纯数字 token（PID）。"""
    for tok in reversed((text or "").split()):
        if tok.isdigit():
            return tok
    return ""


def parse_upgrade_version(filename):
    """从软件包文件名解析待升级版本号。

    VMS-B2101.1.3.9.P17.260720.tar.gz -> VMS-B2101.1.3.9.P17
    解析失败返回文件名去掉后缀的原文。
    """
    base = filename
    if base.lower().endswith(config.UPGRADE_PKG_SUFFIX):
        base = base[: -len(config.UPGRADE_PKG_SUFFIX)]
    m = re.match(config.UPGRADE_VERSION_RE, base)
    if m:
        return m.group(1)
    return base


class UpgradeManager:
    """单个 SSH 会话的版本升级管理器（VMS-U 专用）。"""

    def __init__(self, session):
        self.session = session
        self._lock = threading.Lock()
        # 升级状态：stage ∈ idle/preparing/upload/extract/upgrade/done/error
        self._state = {
            "stage": "idle",
            "percent": 0,
            "version": "",
            "filename": "",
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
            logger.info("upgrade state: %s", self._state)

    def is_running(self):
        st = self.status()
        return st["stage"] not in ("idle", "done", "error")

    # ============================================================
    # 入口：启动升级（后台线程）
    # ============================================================
    def start(self, local_path, remote_name):
        if self.is_running():
            raise RuntimeError("已有升级任务正在进行中")
        if not remote_name.lower().endswith(config.UPGRADE_PKG_SUFFIX):
            raise ValueError("软件包格式必须为 .tar.gz")
        version = parse_upgrade_version(remote_name)

        self._set(stage="preparing", percent=0, version=version,
                  filename=remote_name, error="", started=time.time(), finished=None)

        self._thread = threading.Thread(
            target=self._run, args=(local_path, remote_name, version), daemon=True)
        self._thread.start()

    # ============================================================
    # 主流程
    # ============================================================
    def _run(self, local_path, remote_name, version):
        try:
            upgrade_dir = self._prepare()
            remote_path = self._upload(local_path, upgrade_dir, remote_name)
            top_dir = self._extract(upgrade_dir, remote_name, remote_path)
            self._upgrade(upgrade_dir, top_dir)
            self._set(stage="done", percent=100, finished=time.time())
            logger.info("Upgrade completed: %s -> %s", remote_name, version)
        except Exception as e:
            logger.error("Upgrade failed: %s", e, exc_info=True)
            self._set(stage="error", error=str(e), finished=time.time())
        finally:
            # 清理本地临时文件
            try:
                if local_path and os.path.exists(local_path):
                    os.remove(local_path)
            except Exception:
                pass

    # ------------------------------------------------------------
    # 阶段 1：创建目录
    # ------------------------------------------------------------
    def _prepare(self):
        home_out = self._run_cmd("pwd", timeout=10)
        # 从输出中提取路径行（倒序找第一个以 / 开头的行），
        # 避免二级登录后残留的 banner 文本污染 home 路径
        home = ""
        for line in reversed(home_out.strip().split("\n")):
            line = line.strip()
            if line.startswith("/") and " " not in line:
                home = line
                break
        if not home:
            home = "/root"
        upgrade_dir = f"{home}/{config.UPGRADE_DIR_NAME}"
        # mkdir -p 幂等：目录已存在时不报错
        out = self._run_cmd(f"mkdir -p {upgrade_dir}", timeout=15)
        if "Not Support Cmd Yet!" in out:
            raise RuntimeError("当前环境不支持创建目录（一级受限），请先进入二级后重试")
        # 校验目录存在
        chk = self._run_cmd(f"ls -d {upgrade_dir}", timeout=10)
        if upgrade_dir not in chk:
            raise RuntimeError(f"升级目录创建失败: {upgrade_dir}")
        logger.info("upgrade dir ready: %s", upgrade_dir)
        return upgrade_dir

    # ------------------------------------------------------------
    # 阶段 2：上传软件包（SFTP，带进度）
    # ------------------------------------------------------------
    def _upload(self, local_path, upgrade_dir, remote_name):
        remote_path = f"{upgrade_dir}/{remote_name}"
        total = os.path.getsize(local_path)

        # 清理同名旧包
        self._run_cmd(f"rm -f {remote_path}", timeout=10)

        self._set(stage="upload", percent=0)
        logger.info("uploading %s -> %s (%d bytes)", local_path, remote_path, total)

        last_emit = [0.0]

        def _cb(sent, total_size):
            now = time.time()
            # 每 0.3s 更新一次进度，避免高频刷新
            if now - last_emit[0] >= 0.3 or sent >= total_size:
                last_emit[0] = now
                pct = int(sent * 100 / total_size) if total_size else 0
                self._set(percent=pct)

        # 优先 SFTP（带进度回调）
        try:
            sftp = self.session.get_sftp()
            try:
                try:
                    sftp.get_channel().settimeout(config.FILE_SFTP_CHANNEL_TIMEOUT * 10)
                except Exception:
                    pass
                sftp.put(local_path, remote_path, callback=_cb)
            finally:
                try:
                    sftp.close()
                except Exception:
                    pass
            self._set(percent=100)
            return remote_path
        except Exception as e:
            logger.warning("upgrade upload SFTP failed: %s; trying fallback", e)

        # 回退：FileManager 三级降级（无进度回调 → 完成后直接 100%）
        fm = getattr(self.session, "file_manager", None)
        if fm is None:
            raise RuntimeError("文件管理器不可用，无法上传软件包")
        fm.upload(local_path, remote_path)
        self._set(percent=100)
        return remote_path

    # ------------------------------------------------------------
    # 阶段 3：解压（tar -zxvf，按日志行数计进度）
    # ------------------------------------------------------------
    def _extract(self, upgrade_dir, remote_name, remote_path):
        self._set(stage="extract", percent=0)

        # 3.1 统计包内总条目数（避免管道：先落盘再 wc -l <）
        tarlist = f"{upgrade_dir}/.parkterm_tarlist"
        out = self._run_cmd(
            f"tar -tzf {remote_path} > {tarlist} 2>/dev/null", timeout=60)
        if "Not Support Cmd Yet!" in out:
            raise RuntimeError("当前环境不支持 tar 命令（一级受限），请先进入二级后重试")
        total_out = self._run_cmd(f"wc -l < {tarlist}", timeout=10)
        total = _last_int(total_out) or 0
        if total <= 0:
            raise RuntimeError(f"软件包解析失败（tar -tzf 无内容）: {remote_name}")

        # 3.2 顶层目录名（包内第一个条目）
        first = self._run_cmd(
            f"head -1 {tarlist}", timeout=10).strip().split("/")[0].strip("/")
        if not first:
            raise RuntimeError("无法确定解压后的顶层目录")
        top_dir = first

        # 3.3 后台解压，输出写入日志
        extract_log = f"{upgrade_dir}/.parkterm_extract.log"
        out = self._run_cmd(
            f"rm -f {extract_log}; cd {upgrade_dir} && "
            f"nohup tar -zxvf {remote_name} > {extract_log} 2>&1 & "
            f"echo $! > {upgrade_dir}/.parkterm_extract.pid",
            timeout=15)
        if "Not Support Cmd Yet!" in out:
            raise RuntimeError("当前环境不支持后台解压命令（一级受限），请先进入二级后重试")

        # 3.4 轮询进度
        t0 = time.time()
        no_pid_rounds = 0
        while time.time() - t0 < config.UPGRADE_EXTRACT_TIMEOUT:
            time.sleep(config.UPGRADE_POLL_INTERVAL)
            cnt_out = self._run_cmd(f"wc -l < {extract_log}", timeout=10)
            done_lines = _last_int(cnt_out) or 0
            pct = min(99, int(done_lines * 100 / total)) if total else 0
            self._set(percent=pct)
            # 解压进程是否仍在运行
            pid_out = self._run_cmd(
                f"cat {upgrade_dir}/.parkterm_extract.pid 2>/dev/null", timeout=10)
            pid_str = _last_pid(pid_out)
            if pid_str.isdigit():
                no_pid_rounds = 0
                alive = self._run_cmd(
                    f"kill -0 {pid_str} 2>/dev/null; echo RC=$?", timeout=6)
                if "RC=0" not in alive:
                    # 进程退出 → 重新读取最终行数
                    # （此前 wc 可能读到写入过程中的部分值，进程退出后日志已定格）
                    cnt_out = self._run_cmd(f"wc -l < {extract_log}", timeout=10)
                    done_lines = _last_int(cnt_out) or 0
                    if done_lines >= total:
                        self._set(percent=100)
                        # 清理临时文件
                        self._run_cmd(f"rm -f {tarlist} {extract_log} {upgrade_dir}/.parkterm_extract.pid", timeout=10)
                        logger.info("extract done: top_dir=%s (%d lines)", top_dir, done_lines)
                        return top_dir
                    # 行数不足：检查日志尾部是否有错误
                    tail = self._run_cmd(f"tail -3 {extract_log}", timeout=10)
                    if "Not Support Cmd Yet!" in tail:
                        raise RuntimeError("当前环境不支持解压命令（一级受限），请先进入二级后重试")
                    raise RuntimeError(f"解压异常终止: {tail[:200]}")
            else:
                # PID 文件读不到（命令被拦截/环境异常），连续多次则快速失败
                no_pid_rounds += 1
                if no_pid_rounds >= 5:
                    raise RuntimeError("无法获取解压进程状态（命令被拦截），请确认已进入二级环境")
        raise TimeoutError(f"解压超时（>{config.UPGRADE_EXTRACT_TIMEOUT}s）")

    # ------------------------------------------------------------
    # 阶段 4：执行升级（./update -upgrade）
    # ------------------------------------------------------------
    def _upgrade(self, upgrade_dir, top_dir):
        self._set(stage="upgrade", percent=0)

        work_dir = f"{upgrade_dir}/{top_dir}"
        upgrade_log = f"{upgrade_dir}/.parkterm_upgrade.log"

        # 确认 update 可执行文件存在
        chk = self._run_cmd(f"ls {work_dir}/update", timeout=10)
        if "update" not in chk:
            raise RuntimeError(f"解压目录中未找到 update 可执行文件: {work_dir}/update")

        # 后台执行升级
        out = self._run_cmd(
            f"rm -f {upgrade_log}; cd {work_dir} && "
            f"nohup ./update -upgrade > {upgrade_log} 2>&1 & "
            f"echo $! > {upgrade_dir}/.parkterm_upgrade.pid",
            timeout=15)
        if "Not Support Cmd Yet!" in out:
            raise RuntimeError("当前环境不支持升级命令（一级受限），请先进入二级后重试")

        t0 = time.time()
        last_pct = 0
        no_pid_rounds = 0
        while time.time() - t0 < config.UPGRADE_UPGRADE_TIMEOUT:
            time.sleep(config.UPGRADE_POLL_INTERVAL)
            # 解析日志中的最新百分比
            tail = self._run_cmd(f"tail -5 {upgrade_log}", timeout=10)
            pcts = _PCT_RE.findall(tail)
            if pcts:
                last_pct = max(0, min(99, int(pcts[-1])))
            self._set(percent=last_pct)

            # 进程是否仍在运行
            pid_out = self._run_cmd(
                f"cat {upgrade_dir}/.parkterm_upgrade.pid 2>/dev/null", timeout=10)
            pid_str = _last_pid(pid_out)
            if pid_str.isdigit():
                no_pid_rounds = 0
                alive = self._run_cmd(
                    f"kill -0 {pid_str} 2>/dev/null; echo RC=$?", timeout=6)
                if "RC=0" not in alive:
                    # 进程退出 → 检查退出码和结果
                    # 用 wait 获取退出码（0=成功，非0=失败）
                    exit_out = self._run_cmd(
                        f"wait {pid_str} 2>/dev/null; echo EXIT_CODE=$?",
                        timeout=10)
                    exit_match = re.search(r'EXIT_CODE=(\d+)', exit_out)
                    exit_code = int(exit_match.group(1)) if exit_match else -1
                    tail_all = self._run_cmd(f"tail -20 {upgrade_log}", timeout=10)
                    # 退出码非0 → 明确失败
                    if exit_code != 0:
                        raise RuntimeError(f"升级失败（退出码={exit_code}）: {tail_all[:300]}")
                    # 退出码为0但日志含明确失败关键词 → 也判失败
                    # 注意：用整词匹配避免 "No errors" 误判
                    fail_kw = ("failed", "upgrade fail", "升级失败", "异常退出")
                    if any(k in tail_all.lower() for k in fail_kw):
                        raise RuntimeError(f"升级失败: {tail_all[:300]}")
                    self._run_cmd(
                        f"rm -f {upgrade_log} {upgrade_dir}/.parkterm_upgrade.pid",
                        timeout=10)
                    logger.info("update -upgrade finished")
                    return
            else:
                no_pid_rounds += 1
                if no_pid_rounds >= 5:
                    raise RuntimeError("无法获取升级进程状态（命令被拦截），请确认已进入二级环境")
        raise TimeoutError(f"升级超时（>{config.UPGRADE_UPGRADE_TIMEOUT}s），请检查设备状态")

    # ------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------
    def _run_cmd(self, cmd, timeout=10):
        """执行远程命令并返回过滤后输出。

        异常时记录 warning 并返回空字符串，调用方按需检查。
        """
        try:
            out = self.session.run_command(cmd, timeout=timeout)
            return out or ""
        except Exception as e:
            logger.warning("upgrade cmd '%s' error: %s", cmd, e)
            return ""
