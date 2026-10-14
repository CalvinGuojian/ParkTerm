"""
抓包管理模块：基于 tcpdump 的远程抓包生命周期管理。

核心改进：
  - 抓包状态按会话隔离（CaptureManager 实例绑定到单个 SSHSession），
    消除原全局 _CAPTURES 字典在多会话场景下的数据污染。
  - 状态机封装：pending（待启动）/ running（进行中）/ ended（已结束）。
  - 缓存优化：列表 0.5s、文件大小 0.5s、PID 存活 1s，避免 3s 轮询每次走 SSH。
  - 输入校验：addr/port 做白名单正则校验，防止命令注入。
  - 线程安全：end()/delete() 加锁，_build_records() 网络I/O 移出锁外。

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
# PID 存活退出码精确匹配（避免 "RC=1" 误匹配 "RC=10"）
_RC_RE = re.compile(r"RC=(\d+)")


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
    """构造 tcpdump 过滤表达式（已校验输入）。

    IP 与端口条件之间用 "and" 连接（tcpdump 语法要求连接词，
    空格连接如 "host X port Y" 会导致 filter expression 语法错误）。
    """
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
    return " and ".join(parts)


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

    def _names_on_disk(self):
        """轻量列出磁盘上的抓包文件名（单条命令，不做 stat）。

        供 start()/save() 的数量控制使用，替代昂贵的 _build_records()。
        """
        try:
            out = self.session.run_command(
                f"ls -1 {config.CAPTURE_DIR}/ParkTerm_*.pcap 2>/dev/null", timeout=8)
            names = []
            for line in (out or "").split("\n"):
                n = line.strip().split("/")[-1]
                if n.startswith("ParkTerm_") and n.endswith(".pcap"):
                    names.append(n)
            return names
        except Exception as e:
            logger.debug("_names_on_disk failed: %s", e)
            return []

    def _stat_all(self):
        """一次性获取目录下所有抓包文件的名称与大小（单条命令）。

        性能优化：替代「ls 扫描 + 每文件一条 stat」的多命令模式，
        列表刷新从 N+1 条命令降为 1 条。
        返回 {文件名: 字节数}。
        """
        cmd = (f"stat -c '%n|%s' {config.CAPTURE_DIR}/ParkTerm_*.pcap 2>/dev/null")
        out = self.session.run_command(cmd, timeout=10)
        result = {}
        for line in (out or "").split("\n"):
            line = line.strip()
            if "|" not in line:
                continue
            path, _, size = line.rpartition("|")
            name = path.strip().split("/")[-1]
            if not (name.startswith("ParkTerm_") and name.endswith(".pcap")):
                continue
            try:
                result[name] = int(size.strip())
            except ValueError:
                result[name] = 0
        return result

    def _pid_alive(self, pid):
        """检查进程是否存活。返回 True / False / None（无法判断）。

        部分受限 shell（一级环境）下 kill -0 + echo RC=$? 不产生任何输出，
        此时返回 None 表示"无法判断"，调用方应保持原状态而非判定为已结束。
        """
        if not pid:
            return False
        try:
            out = self.session.run_command(
                f"kill -0 {pid} 2>/dev/null; echo RC=$?", timeout=6
            )
            m = _RC_RE.search(out)
            if not m:
                # 输出中无 RC= 标记（受限环境或命令被拦截）→ 无法判断
                logger.debug("_pid_alive(%s): no RC marker in output: %r", pid, out[:100])
                return None
            rc = int(m.group(1))
            if rc == 0:
                return True
            return False
        except Exception:
            return None

    def _pid_alive_cached(self, pid):
        if not pid:
            return False
        now = time.time()
        c = self._pid_cache.get(pid)
        if c and (now - c[0]) < config.CAPTURE_PID_CACHE_TTL:
            return c[1]
        ok = self._pid_alive(pid)
        # None（无法判断）不写入缓存，避免把"未知"缓存为持久状态
        if ok is not None:
            self._pid_cache[pid] = (now, ok)
            # 进程已死 → 清理 PID 缓存条目
            if ok is False:
                self._pid_cache.pop(pid, None)
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
        # 仅缓存成功获取的大小（size > 0），避免缓存失败时的 0 值
        if size > 0:
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

    def _cleanup_old_files(self):
        """清理旧文件直到数量达标。最多重试 3 次防止无限循环。"""
        names = self._names_on_disk()
        retries = 0
        while len(names) >= config.CAPTURE_MAX_FILES and retries < 3:
            self._delete_file(names[0])
            new_names = self._names_on_disk()
            if len(new_names) == len(names):
                # 删除失败（权限/网络），跳出避免无限循环
                logger.warning("_cleanup_old_files: delete failed, %d files remain", len(new_names))
                break
            names = new_names
            retries += 1
        return names

    # ============================================================
    # 状态构建（带缓存）
    # ============================================================
    def _build_records(self):
        """从内存状态构建抓包列表。状态精确为 pending/running/ended。

        性能优化：
        - 用单条 stat 命令批量获取全部文件名+大小（N+1→1）
        - PID 存活检查在锁外执行，避免持锁期间阻塞网络 I/O
        - 清理已不在磁盘上的历史条目，防止 _captures 无界增长
        """
        now = time.time()
        if self._list_cache and (now - self._list_cache[0]) < config.CAPTURE_LIST_CACHE_TTL:
            return self._list_cache[1]

        # ---- 锁外：网络 I/O（stat + PID 检查）----
        file_sizes = self._stat_all()
        # stat 命令瞬时失败但内存中已有文件时，回退到（带缓存的）单文件查询
        if not file_sizes and self._captures:
            file_sizes = {}
            for n in list(self._captures.keys()):
                if n.endswith(".pcap"):
                    file_sizes[n] = self._file_size(f"{config.CAPTURE_DIR}/{n}")

        # 收集需要检查 PID 的任务（锁内快照 → 锁外检查）
        pids_to_check = {}
        with self._lock:
            for name, st in self._captures.items():
                pid = st.get("pid")
                if pid and pid > 0 and st.get("ended") is None:
                    pids_to_check[pid] = name
        # 锁外执行 PID 存活检查（网络 I/O）
        pid_alive_map = {}
        for pid in pids_to_check:
            pid_alive_map[pid] = self._pid_alive_cached(pid)

        # ---- 锁内：构建输出 ----
        out = {}
        with self._lock:
            # 用磁盘文件补充历史记录
            for fname, fsize in file_sizes.items():
                if fname not in self._captures:
                    self._captures[fname] = {
                        "pid": 0,  # 0表示历史文件，已结束
                        "path": f"{config.CAPTURE_DIR}/{fname}",
                        "addr": "",
                        "port": "",
                        "size_mb": 0,
                        "time_min": 0,
                        "started": None,
                        "ended": now,
                        "running": False,
                    }

            # 清理已不在磁盘上的已结束条目（防止 _captures 无界增长）
            disk_names = set(file_sizes.keys())
            stale = [
                n for n, st in self._captures.items()
                if n not in disk_names
                and st.get("pid") is not None  # 保留 pending（pid=None）
                and st.get("ended") is not None  # 仅清理已结束的
            ]
            for n in stale:
                self._captures.pop(n, None)
                fpath = f"{config.CAPTURE_DIR}/{n}"
                self._size_cache.pop(fpath, None)

            for name, st in list(self._captures.items()):
                pid = st.get("pid")
                path = st.get("path") or f"{config.CAPTURE_DIR}/{name}"
                # 状态判断逻辑：
                # - pid is None: 待启动（新建任务但未启动）
                # - pid == 0: 历史文件（已结束）
                # - pid > 0: 运行中或已结束（需检查进程存活状态）
                if pid is None:
                    status = "pending"
                    size = 0
                    elapsed = None
                elif pid == 0:
                    status = "ended"
                    size = file_sizes.get(name)
                    if size is None:
                        size = self._file_size(path)
                    elapsed = st.get("ended") - st.get("started") if st.get("started") else None
                else:
                    if st.get("ended") is not None:
                        # 用户手动结束（end() 设置 ended）→ 已结束
                        status = "ended"
                    else:
                        alive = pid_alive_map.get(pid)
                        if alive is None:
                            # PID 无法判断（受限环境）→ 保持上次状态，避免误判
                            status = "running" if st.get("running") else "ended"
                        elif alive:
                            status = "running"
                            st["running"] = True
                        else:
                            # 进程确认已退出 → 进一步用文件大小变化兜底：
                            # 若文件仍在增长，说明抓包仍在进行（PID 判断失败）
                            new_size = file_sizes.get(name)
                            if new_size is None:
                                new_size = self._file_size(path)
                            last_size = st.get("last_size")
                            if last_size is not None and new_size > last_size and new_size > 0:
                                status = "running"
                                st["running"] = True
                            else:
                                status = "ended"
                                st["running"] = False
                                st["ended"] = st.get("ended") or now
                    started = st.get("started") or now
                    if status == "running":
                        elapsed = int(now - started)
                        size = file_sizes.get(name)
                        if size is None:
                            size = self._file_size(path)
                        st["last_size"] = size
                    else:
                        ended = st.get("ended") or now
                        elapsed = int(ended - started) if started else None
                        size = file_sizes.get(name)
                        if size is None:
                            size = self._file_size(path)

                out[name] = {
                    "name": name,
                    "path": path,
                    "addr": st.get("addr", ""),
                    "port": st.get("port", ""),
                    "size": size or 0,
                    "size_mb": round((size or 0) / 1048576, 1) if size else 0,
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
            self._cleanup_old_files()
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
                self._cleanup_old_files()
                name = gen_name()
                fpath = f"{config.CAPTURE_DIR}/{name}"

            # 先注册任务（pid=None → 待启动），防止并发竞态
            # 导致文件先于状态被扫描为"历史已结束"
            self._captures[name] = {
                "pid": None,
                "path": fpath,
                "addr": addr,
                "port": port,
                "size_mb": size_mb,
                "time_min": time_min,
                "started": None,
                "ended": None,
                "running": False,
            }
            self._invalidate()

            filt = build_filter(addr, port)
            # 过滤表达式含括号/and/or，必须用单引号包裹，
            # 否则裸括号会被 shell 解释为子 shell 语法导致整条命令被拒绝
            filt_part = f"'{filt}'" if filt else ""
            time_sec = int(max(1, time_min * 60))
            debug_dir = f"{config.CAPTURE_DIR}/debug"
            pidfile = f"{debug_dir}/{name}.pid"
            errfile = f"{debug_dir}/{name}.err"

            # 启动方式：PID 同时写入 pidfile（诊断用）并 echo 到输出（直接解析），
            # 消除"启动后轮询 cat pidfile"的多条命令往返
            cmd = (
                f"mkdir -p {config.CAPTURE_DIR} {debug_dir}; "
                f"rm -f {fpath} {pidfile} {errfile}; "
                f"nohup tcpdump -i any -nn -s0 -C {size_mb} -G {time_sec} -W 1 "
                f"-w {fpath} {filt_part} >/dev/null 2>{errfile} </dev/null & "
                f"echo $! > {pidfile}; echo PARKTERM_PID=$!"
            )
            logger.info("capture_start cmd: %s", cmd)
            out = self.session.run_command(cmd, timeout=15)
            logger.info("capture_start raw output: %r", (out or "")[:500])

            # 受限环境（一级）检测：tcpdump/nohup 等命令被拦截
            if out and "Not Support Cmd Yet!" in out:
                self._captures.pop(name, None)  # 移除刚注册的任务
                self._invalidate()
                raise RuntimeError(
                    "当前环境（一级）不支持抓包命令（tcpdump/nohup 被拦截），"
                    "请先进入二级后重试"
                )

            # 直接从命令输出解析 PID（消除 pidfile 读取循环，省 1~10 条命令）
            pid = None
            if out:
                m = re.search(r"PARKTERM_PID=(\d+)", out)
                if m:
                    pid = int(m.group(1))
            logger.info("capture_start pid: %s", pid)

            def _read_err():
                try:
                    return self.session.run_command(f"cat {errfile} 2>&1", timeout=5).strip()
                except Exception:
                    return ""

            if not pid:
                err_content = _read_err()
                self._captures.pop(name, None)  # 移除刚注册的任务
                self._invalidate()
                if "Not Support Cmd Yet!" in err_content:
                    raise RuntimeError("当前环境（一级）不支持抓包命令，请先进入二级后重试")
                raise RuntimeError(f"tcpdump 启动失败，无法获取 PID。错误日志: {err_content[:300]}")

        # ---- 锁外：验证进程存活（网络 I/O 不持锁）----
        time.sleep(0.5)
        verify = self._pid_alive(pid)
        if verify is False:
            err_content = _read_err()
            with self._lock:
                self._captures.pop(name, None)
                self._invalidate()
            if "parse filter" in err_content or "syntax error" in err_content:
                raise RuntimeError(
                    "抓包过滤条件无效（请检查 IP 地址/端口格式，"
                    "多个地址或端口用英文逗号分隔）: " + err_content[:200])
            if err_content:
                raise RuntimeError(f"tcpdump 启动后立即退出: {err_content[:300]}")
            raise RuntimeError(
                "tcpdump 启动后立即退出，请确认设备已安装 tcpdump"
                "（进入二级后执行 which tcpdump 检查）"
            )

        # 成功，保存任务状态
        with self._lock:
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
                "last_size": 0,
            }
            self._invalidate()
        return name

    def list(self):
        """返回抓包列表（带缓存）。"""
        return self._build_records()

    def end(self, name):
        """结束一个正在运行的抓包，保留文件（状态变为 ended，可下载）。"""
        with self._lock:
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
        with self._lock:
            self._delete_file(name)
            self._invalidate()

    def download_path(self, name):
        """返回抓包文件的远程路径，供下载使用。"""
        if not name or not name.startswith("ParkTerm_") or not name.endswith(".pcap"):
            raise ValueError("非法文件名")
        return f"{config.CAPTURE_DIR}/{name}"
