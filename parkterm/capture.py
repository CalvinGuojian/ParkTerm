"""
抓包管理模块：基于 tcpdump 的远程抓包生命周期管理。

核心改进：
  - 抓包状态按会话隔离（CaptureManager 实例绑定到单个 SSHSession），
    消除原全局 _CAPTURES 字典在多会话场景下的数据污染。
  - 状态机封装：pending（待启动）/ running（进行中）/ ended（已结束）。
  - 缓存优化：列表 2.5s、文件大小/PID 存活 5s，避免 2s 轮询每次走 SSH。
  - 输入校验：addr/port 做白名单正则校验，防止命令注入。

抓包文件命名：ParkTerm_YYMMDD_HHMMSS.pcap，最多保留 3 个。
"""
import datetime as _dt
import logging
import os
import re
import threading
import time

from . import config

logger = logging.getLogger("parkterm")

# 输入白名单正则
_ADDR_RE = re.compile(r"^[a-zA-Z0-9.\-_:]+$")
_PORT_RE = re.compile(r"^\d+$")


def _validate_addr(addr):
    """校验抓包过滤地址，返回清洗后的列表或空字符串。"""
    if not addr:
        return ""
    hosts = [a.strip() for a in addr.split(",") if a.strip()]
    valid = [h for h in hosts if _ADDR_RE.match(h)]
    if len(valid) != len(hosts):
        logger.warning("capture: dropped invalid addr filter: %s", addr)
    return ",".join(valid)


def _validate_port(port):
    """校验抓包过滤端口，返回清洗后的列表或空字符串。"""
    if not port:
        return ""
    ports = [p.strip() for p in port.split(",") if p.strip()]
    valid = [p for p in ports if _PORT_RE.match(p)]
    if len(valid) != len(ports):
        logger.warning("capture: dropped invalid port filter: %s", port)
    return ",".join(valid)


def build_filter(addr, port):
    """构造 tcpdump 过滤表达式（已校验输入）。"""
    parts = []
    if addr:
        hosts = [a.strip() for a in addr.split(",") if a.strip()]
        if hosts:
            parts.append(
                "(" + " or ".join(f"host {h}" for h in hosts) + ")"
                if len(hosts) > 1 else f"host {hosts[0]}"
            )
    if port:
        ports = [p.strip() for p in port.split(",") if p.strip()]
        if ports:
            parts.append(
                "(" + " or ".join(f"port {p}" for p in ports) + ")"
                if len(ports) > 1 else f"port {ports[0]}"
            )
    return " ".join(parts)


def gen_name():
    """生成抓包文件名：ParkTerm_YYMMDD_HHMMSS.pcap"""
    ts = _dt.datetime.now().strftime("%y%m%d_%H%M%S")
    return f"ParkTerm_{ts}.pcap"


class CaptureManager:
    """单个 SSH 会话的抓包管理器。状态完全隔离在实例内。"""

    def __init__(self, session):
        self.session = session
        self._lock = threading.RLock()
        # 抓包状态：filename -> state dict
        self._captures = {}
        # 缓存
        self._list_cache = None  # (ts, items)
        self._size_cache = {}    # fpath -> (ts, size)
        self._pid_cache = {}     # pid -> (ts, alive)

    # ============================================================
    # 内部工具
    # ============================================================
    def _invalidate(self):
        self._list_cache = None

    def _ensure_dir(self):
        """确保抓包保存目录存在。"""
        try:
            self.session.run_command(
                f"mkdir -p {config.CAPTURE_DIR}",
                timeout=8,
            )
        except Exception:
            pass

    def _all_names(self):
        return [r["name"] for r in self._build_records()]

    def _pid_alive(self, pid):
        if not pid:
            return False
        try:
            out = self.session.run_command(
                f"kill -0 {pid} 2>/dev/null; echo RC=$?", timeout=6
            )
            return "RC=0" in out
        except Exception:
            return False

    def _pid_alive_cached(self, pid):
        if not pid:
            return False
        now = time.time()
        c = self._pid_cache.get(pid)
        if c and (now - c[0]) < config.CAPTURE_PID_CACHE_TTL:
            return c[1]
        ok = self._pid_alive(pid)
        self._pid_cache[pid] = (now, ok)
        return ok

    def _file_size(self, path):
        now = time.time()
        c = self._size_cache.get(path)
        if c and (now - c[0]) < config.CAPTURE_SIZE_CACHE_TTL:
            return c[1]
        size = 0
        try:
            # 用 stat 获取文件字节数，比 ls 解析更可靠
            out = self.session.run_command(f"stat -c %s {path} 2>/dev/null || wc -c < {path} 2>/dev/null", timeout=6)
            m = re.search(r"(\d+)", out)
            if m:
                size = int(m.group(1))
        except Exception:
            pass
        self._size_cache[path] = (now, size)
        return size

    def _delete_file(self, name):
        fpath = f"{config.CAPTURE_DIR}/{name}"
        st = self._captures.get(name)
        if st and st.get("pid"):
            try:
                self.session.run_command(
                    f"kill -TERM {st['pid']} 2>/dev/null; true", timeout=6
                )
            except Exception:
                pass
        try:
            debug_dir = f"{config.CAPTURE_DIR}/debug"
            self.session.run_command(f"rm -f {fpath} {debug_dir}/{name}.pid {debug_dir}/{name}.err", timeout=6)
        except Exception:
            pass
        self._size_cache.pop(fpath, None)
        self._captures.pop(name, None)

    # ============================================================
    # 状态构建（带缓存）
    # ============================================================
    def _scan_disk(self):
        """扫描远程SSH设备上的 .pcap 文件，添加到内存状态中（用于展示历史抓包）。"""
        try:
            # 使用 SSH 命令扫描远程设备上的抓包文件
            cmd = f"ls -1 {config.CAPTURE_DIR}/ParkTerm_*.pcap 2>/dev/null || true"
            out = self.session.run_command(cmd, timeout=10)
            
            if not out or not out.strip():
                logger.debug("_scan_disk: no capture files found")
                return
            
            for line in out.strip().split("\n"):
                line = line.strip()
                if not line or not line.endswith(".pcap"):
                    continue
                # 从完整路径中提取文件名
                filename = line.split("/")[-1]
                if not filename.startswith("ParkTerm_"):
                    continue
                if filename not in self._captures:
                    self._captures[filename] = {
                        "pid": 0,  # 0表示历史文件，已结束
                        "path": f"{config.CAPTURE_DIR}/{filename}",
                        "addr": "",
                        "port": "",
                        "size_mb": 0,
                        "time_min": 0,
                        "started": None,
                        "ended": time.time(),
                        "running": False,
                    }
                    logger.debug("Found historical capture file: %s", filename)
        except Exception as e:
            logger.warning("_scan_disk failed: %s", e)

    def _build_records(self, force=False):
        """从内存状态构建抓包列表。状态精确为 pending/running/ended。"""
        now = time.time()
        # 每次构建列表前扫描磁盘，补充历史抓包文件
        self._scan_disk()
        if not force:
            if self._list_cache and (now - self._list_cache[0]) < config.CAPTURE_LIST_CACHE_TTL:
                return self._list_cache[1]

        out = {}
        with self._lock:
            for name, st in list(self._captures.items()):
                pid = st.get("pid")
                path = f"{config.CAPTURE_DIR}/{name}"
                # 状态判断逻辑：
                # - pid is None: 待启动（新建任务但未启动）
                # - pid == 0: 历史文件（已结束）
                # - pid > 0: 运行中或已结束（需检查进程存活状态）
                if pid is None:
                    status = "pending"
                    size = 0
                    elapsed = None
                elif pid == 0:
                    # 历史文件，状态为已结束
                    status = "ended"
                    size = self._file_size(path)
                    elapsed = st.get("ended") - st.get("started") if st.get("started") else None
                else:
                    if st.get("ended") is not None:
                        status = "ended"
                    elif self._pid_alive_cached(pid):
                        status = "running"
                    else:
                        status = "ended"
                        st["ended"] = st.get("ended") or now
                        st["running"] = False
                    started = st.get("started") or now
                    if status == "running":
                        elapsed = int(now - started)
                    else:
                        ended = st.get("ended") or now
                        elapsed = int(ended - started) if started else None
                    size = self._file_size(path) if status != "pending" else 0

                out[name] = {
                    "name": name,
                    "path": path,
                    "addr": st.get("addr", ""),
                    "port": st.get("port", ""),
                    "size": size,
                    "size_mb": round(size / 1048576, 1) if size else 0,
                    "duration": elapsed,
                    "status": status,
                }

        items = sorted(out.values(), key=lambda r: r["name"], reverse=True)
        items = items[:config.CAPTURE_MAX_FILES]
        self._list_cache = (now, items)
        return items

    # ============================================================
    # 公共 API
    # ============================================================
    def save(self, addr="", port="", size_mb=100, time_min=5):
        """仅保存：创建一个待启动任务，不启动 tcpdump。"""
        addr = _validate_addr(addr)
        port = _validate_port(port)
        size_mb = int(max(1, size_mb or config.CAPTURE_DEFAULT_SIZE_MB))
        time_min = int(max(1, time_min or config.CAPTURE_DEFAULT_TIME_MIN))

        with self._lock:
            names = self._all_names()
            while len(names) >= config.CAPTURE_MAX_FILES:
                self._delete_file(names[-1])
                names = self._all_names()
            name = gen_name()
            self._captures[name] = {
                "pid": None,
                "path": f"{config.CAPTURE_DIR}/{name}",
                "addr": addr,
                "port": port,
                "size_mb": size_mb,
                "time_min": time_min,
                "started": None,
                "ended": None,
                "running": False,
            }
        self._invalidate()
        return name

    def start(self, name=None, addr="", port="", size_mb=100, time_min=5):
        """启动抓包。若指定 name，则启动已保存的待启动任务；否则新建并立即启动。"""
        addr = _validate_addr(addr)
        port = _validate_port(port)
        size_mb = int(max(1, size_mb or config.CAPTURE_DEFAULT_SIZE_MB))
        time_min = int(max(1, time_min or config.CAPTURE_DEFAULT_TIME_MIN))

        with self._lock:
            if name:
                st = self._captures.get(name)
                if not st:
                    raise ValueError("抓包任务不存在")
                if st.get("pid"):
                    raise ValueError("该抓包已在运行")
                addr = st.get("addr", "") or addr
                port = st.get("port", "") or port
                size_mb = st.get("size_mb", size_mb)
                time_min = st.get("time_min", time_min)
                fpath = f"{config.CAPTURE_DIR}/{name}"
            else:
                self._ensure_dir()
                names = self._all_names()
                while len(names) >= config.CAPTURE_MAX_FILES:
                    self._delete_file(names[-1])
                    names = self._all_names()
                name = gen_name()
                fpath = f"{config.CAPTURE_DIR}/{name}"

            filt = build_filter(addr, port)
            time_sec = int(max(1, time_min * 60))
            debug_dir = f"{config.CAPTURE_DIR}/debug"
            pidfile = f"{debug_dir}/{name}.pid"
            errfile = f"{debug_dir}/{name}.err"

            # 简单可靠的启动方式：不用子 shell，PID 写入 pidfile，不依赖输出捕获
            # tcpdump 参数说明：
            #   -i any    抓取所有网卡
            #   -nn       不解析主机名和端口名
            #   -s0       抓取完整数据包（不截断）
            #   -C N      文件达到 N MB 后旋转
            #   -G N      N 秒后旋转
            #   -W 1      最多保留 1 个文件（达到限制后停止）
            #   -w file   输出文件
            cmd = (
                f"mkdir -p {config.CAPTURE_DIR} {debug_dir}; "
                f"rm -f {fpath} {pidfile} {errfile}; "
                f"nohup tcpdump -i any -nn -s0 -C {size_mb} -G {time_sec} -W 1 "
                f"-w {fpath} {filt} >/dev/null 2>{errfile} </dev/null & "
                f"echo $! > {pidfile}"
            )
            logger.info("capture_start cmd: %s", cmd)
            out = self.session.run_command(cmd, timeout=15)
            logger.info("capture_start raw output: %r", out[:500])

            # 等待 pidfile 写入（最多等2秒）
            import time as _time
            pid = None
            for _ in range(10):
                try:
                    po = self.session.run_command(f"cat {pidfile} 2>/dev/null", timeout=3)
                    if po.strip():
                        pid = int(po.strip().split()[0])
                        break
                except Exception:
                    pass
                _time.sleep(0.2)
            logger.info("capture_start pid: %s", pid)

            if not pid:
                # 读取错误日志帮助诊断
                err_content = ""
                try:
                    err_out = self.session.run_command(f"cat {errfile} 2>&1", timeout=5)
                    err_content = err_out.strip()
                except Exception:
                    pass
                raise RuntimeError(f"tcpdump 启动失败，无法获取 PID。错误日志: {err_content[:300]}")

            # 5. 成功，保存任务状态
            self._captures[name] = {
                "pid": pid,
                "path": fpath,
                "addr": addr,
                "port": port,
                "size_mb": size_mb,
                "time_min": time_min,
                "started": time.time(),
                "ended": None,
                "running": True,
            }
            self._invalidate()
        return name

    def list(self):
        """返回抓包列表（带缓存）。"""
        return self._build_records()

    def end(self, name):
        """结束一个正在运行的抓包，保留文件（状态变为 ended，可下载）。"""
        st = self._captures.get(name)
        if not st:
            raise ValueError("抓包任务不存在")
        if st.get("pid"):
            try:
                self.session.run_command(
                    f"kill -TERM {st['pid']} 2>/dev/null; true", timeout=6
                )
            except Exception:
                pass
        st["running"] = False
        st["ended"] = time.time()
        self._invalidate()

    def delete(self, name):
        """删除抓包（停止进程 + 删除文件）。"""
        if not name or not name.startswith("ParkTerm_") or not name.endswith(".pcap"):
            raise ValueError("非法文件名")
        self._delete_file(name)
        self._invalidate()

    def download_path(self, name):
        """返回抓包文件的远程路径，供下载使用。"""
        if not name or not name.startswith("ParkTerm_") or not name.endswith(".pcap"):
            raise ValueError("非法文件名")
        return f"{config.CAPTURE_DIR}/{name}"
