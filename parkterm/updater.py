"""
ParkTerm 自身更新管理器。

流程（后台线程执行，前端轮询 /api/update_status 获取进度）：
  1. check     : 从 GitHub 拉取 version.json，比较版本号
  2. download  : 下载新 exe 到临时目录，带进度
  3. apply     : 生成 batch 脚本，启动脚本后退出当前进程
                 batch 等待旧进程退出 → 覆盖 exe → 启动新 exe → 清理临时文件

version.json 格式：
  {
    "version": "v1.0.6",
    "url": "https://github.com/CalvinGuojian/ParkTerm/releases/download/v1.0.6/ParkTerm_V1.0.6.exe",
    "changelog": "1. 修复xxx\\n2. 新增xxx",
    "forceUpdate": false
  }
"""
import json
import logging
import os
import re
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error

from . import config

logger = logging.getLogger("parkterm")


def _parse_version(ver_str):
    """将版本字符串解析为可比较的元组。

    "v1.0.5" → (1, 0, 5)
    "1.0.5"  → (1, 0, 5)
    "v1.2"   → (1, 2)
    解析失败返回 (0,)。
    """
    nums = re.findall(r"\d+", ver_str or "")
    return tuple(int(n) for n in nums) if nums else (0,)


class UpdateManager:
    """ParkTerm 自身更新管理器（单例）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._state = {
            "stage": "idle",        # idle/checking/downloading/ready/error
            "percent": 0,
            "version": "",
            "changelog": "",
            "forceUpdate": False,
            "url": "",
            "error": "",
            "started": None,
            "finished": None,
        }
        self._thread = None
        self._new_exe_path = ""
        self._check_result = None  # 缓存最近一次 check 结果

    # ============================================================
    # 状态
    # ============================================================
    def status(self):
        with self._lock:
            return dict(self._state)

    def _set(self, **kw):
        with self._lock:
            self._state.update(kw)
            logger.info("updater state: %s", {k: v for k, v in kw.items() if k != "changelog"})

    def is_running(self):
        st = self.status()
        return st["stage"] not in ("idle", "ready", "error")

    # ============================================================
    # 阶段 1：检查更新
    # ============================================================
    def check(self):
        """检查更新：从 GitHub 拉取 version.json，比较版本号。

        返回 dict:
          hasUpdate: bool
          version: str (新版本号)
          changelog: str
          forceUpdate: bool
          url: str (下载地址)
          currentVersion: str (当前版本)
        失败时返回 {hasUpdate: False, error: "..."}
        """
        self._set(stage="checking", percent=0, error="",
                  started=time.time(), finished=None)
        try:
            logger.info("checking updates from %s", config.UPDATE_CHECK_URL)
            req = urllib.request.Request(
                config.UPDATE_CHECK_URL,
                headers={"User-Agent": f"ParkTerm/{config.VERSION}"}
            )
            with urllib.request.urlopen(req, timeout=config.UPDATE_CHECK_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            remote_ver = data.get("version", "")
            url = data.get("url", "")
            changelog = data.get("changelog", "")
            force = bool(data.get("forceUpdate", False))

            current_tuple = _parse_version(config.VERSION)
            remote_tuple = _parse_version(remote_ver)
            has_update = remote_tuple > current_tuple

            result = {
                "hasUpdate": has_update,
                "version": remote_ver,
                "changelog": changelog,
                "forceUpdate": force,
                "url": url,
                "currentVersion": config.VERSION,
            }
            self._check_result = result
            self._set(stage="idle", percent=0, finished=time.time(),
                      version=remote_ver, changelog=changelog,
                      forceUpdate=force, url=url)
            logger.info("update check: current=%s remote=%s hasUpdate=%s force=%s",
                        config.VERSION, remote_ver, has_update, force)
            return result

        except Exception as e:
            logger.error("update check failed: %s", e, exc_info=True)
            self._set(stage="error", error=str(e), finished=time.time())
            return {
                "hasUpdate": False,
                "error": str(e),
                "currentVersion": config.VERSION,
            }

    # ============================================================
    # 阶段 2：下载新 exe（后台线程）
    # ============================================================
    def start_download(self, url):
        """启动后台下载线程。"""
        if self.is_running():
            raise RuntimeError("已有更新任务正在进行中")
        if not url:
            raise ValueError("下载地址不能为空")

        self._set(stage="downloading", percent=0, error="",
                  started=time.time(), finished=None)

        self._thread = threading.Thread(
            target=self._download_run, args=(url,), daemon=True)
        self._thread.start()

    def _download_run(self, url):
        """下载线程主逻辑。"""
        try:
            # 准备临时文件路径
            tmp_dir = tempfile.gettempdir()
            self._new_exe_path = os.path.join(tmp_dir, "ParkTerm_update.exe")

            logger.info("downloading %s -> %s", url, self._new_exe_path)

            req = urllib.request.Request(
                url,
                headers={"User-Agent": f"ParkTerm/{config.VERSION}"}
            )

            with urllib.request.urlopen(req, timeout=config.UPDATE_DOWNLOAD_TIMEOUT) as resp:
                total = int(resp.headers.get("Content-Length", 0))
                downloaded = 0
                chunk_size = 65536  # 64KB
                last_emit = 0.0

                with open(self._new_exe_path, "wb") as f:
                    while True:
                        chunk = resp.read(chunk_size)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        now = time.time()
                        # 每 0.3s 更新一次进度
                        if now - last_emit >= 0.3 or downloaded >= total:
                            last_emit = now
                            pct = int(downloaded * 100 / total) if total else 0
                            self._set(percent=pct)

            # 校验文件大小
            file_size = os.path.getsize(self._new_exe_path)
            if file_size < 1024:
                raise RuntimeError(f"下载文件过小（{file_size} 字节），可能不是有效 exe")

            self._set(stage="ready", percent=100, finished=time.time())
            logger.info("download completed: %s (%d bytes)", self._new_exe_path, file_size)

        except Exception as e:
            logger.error("download failed: %s", e, exc_info=True)
            self._set(stage="error", error=str(e), finished=time.time())
            # 清理临时文件
            try:
                if self._new_exe_path and os.path.exists(self._new_exe_path):
                    os.remove(self._new_exe_path)
            except Exception:
                pass

    # ============================================================
    # 阶段 3：应用更新（生成 batch 脚本，退出进程）
    # ============================================================
    def apply(self):
        """生成 batch 脚本并启动，调用方应在之后退出进程。

        返回 dict:
          success: bool
          message: str
        """
        try:
            if not self._new_exe_path or not os.path.exists(self._new_exe_path):
                return {"success": False, "message": "新版本文件不存在，请重新下载"}

            # 获取当前 exe 路径
            if getattr(sys, "frozen", False):
                current_exe = sys.executable
            else:
                current_exe = os.path.abspath(sys.argv[0])

            new_exe = self._new_exe_path
            batch_path = os.path.join(tempfile.gettempdir(), "parkterm_updater.bat")

            # 生成 batch 脚本
            # 1. 等待旧进程退出（最多等 10 秒）
            # 2. 复制新 exe 覆盖旧 exe
            # 3. 启动新 exe
            # 4. 清理临时文件和自身
            batch_content = f"""@echo off
chcp 65001 >nul
set /a wait=0
:wait_loop
tasklist /fi "pid eq {os.getpid()}" 2>nul | find "{os.getpid()}" >nul
if %errorlevel% equ 0 (
    set /a wait+=1
    if !wait! geq 50 goto force_kill
    timeout /t 1 /nobreak >nul
    goto wait_loop
)
:force_kill
copy /Y "{new_exe}" "{current_exe}" >nul 2>&1
if %errorlevel% neq 0 (
    echo 更新失败：无法替换程序文件，请手动替换。
    echo 新文件位置: {new_exe}
    pause
    exit /b 1
)
start "" "{current_exe}"
del "{new_exe}" 2>nul
del "%~f0" 2>nul
"""

            with open(batch_path, "w", encoding="utf-8") as f:
                f.write(batch_content)

            logger.info("update batch script created: %s", batch_path)
            logger.info("replacing: %s <- %s", current_exe, new_exe)

            # 启动 batch 脚本（不等待）
            import subprocess
            subprocess.Popen(
                ["cmd", "/c", batch_path],
                creationflags=subprocess.CREATE_NO_WINDOW,
                cwd=tempfile.gettempdir()
            )

            return {"success": True, "message": "更新已准备就绪，应用将重启"}

        except Exception as e:
            logger.error("apply update failed: %s", e, exc_info=True)
            return {"success": False, "message": str(e)}


# ============================================================
# 全局单例
# ============================================================
updater = UpdateManager()
