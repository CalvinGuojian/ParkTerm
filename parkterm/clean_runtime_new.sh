#!/bin/sh
#
#===============================================================================
# 脚本名称: clean_runtime_new.sh
# 功能描述: 手动清理运行时数据库脚本
# 适用平台: B200 / VMS-B500
# 版本信息: V2.4
#===============================================================================

#===============================================================================
# 配置区域
#===============================================================================
# 日志文件路径
CLEAN_RUNTIME_LOG="/var/runtime/manual_clean_runtime.log"

# 锁文件路径(防止脚本重复执行)
LOCK_FILE="/var/runtime/clean_runtime.lock"

# 备份目录(根据设备类型动态设置)
BACKUP_ROOT_DIR=""

# 数据库清理阈值(MB)
# B500设备: 2560MB, B200设备: 600MB
DB_THRESHOLD_B500=2560
DB_THRESHOLD_DEFAULT=600
DB_THRESHOLD=""

# 运行时数据库路径
RUNTIME_DATABASE="/var/runtime/database"

# 备份数据库路径(清理过程中使用)
BACKUP_DATABASE=""

#===============================================================================
# 公共变量(勿修改)
#===============================================================================
SCRIPT_EXIT_CODE=0
FORCE_CLEAN=0    # 强制清理标志(绕过空间检查)

#===============================================================================
# 日志函数
#===============================================================================
# 描述: 记录日志到文件和控制台
# 参数: $1 - 日志内容
# 返回: 无
log_message()
{
    local timestamp
    timestamp=$(date '+%Y-%m-%d %H:%M:%S')

    # 限制日志文件大小(最大10万行,超过则删除前200行)
    if [ -f "$CLEAN_RUNTIME_LOG" ]; then
        local line_count
        line_count=$(sed -n '$=' "$CLEAN_RUNTIME_LOG")
        if [ "$line_count" -ge 100000 ]; then
            sed -i '1,200d' "$CLEAN_RUNTIME_LOG"
        fi
    fi

    echo "${timestamp}:$1" >> "$CLEAN_RUNTIME_LOG"
    echo "${timestamp}:$1"
}

#===============================================================================
# 错误处理函数
#===============================================================================
# 描述: 脚本错误退出处理
# 参数: $1 - 错误信息, $2 - 退出码(默认1)
# 返回: 无
script_error_exit()
{
    SCRIPT_EXIT_CODE=${2:-1}

    log_message "##################################################"
    log_message "#         Script execution failed, exiting now!          #"
    log_message "##################################################"
    log_message "Error reason: $1"
    log_message "Log file: $CLEAN_RUNTIME_LOG"
    log_message "##################################################"

    # 清理锁文件
    cleanup_lock_file

    exit "$SCRIPT_EXIT_CODE"
}

#===============================================================================
# 锁文件管理函数
#===============================================================================
# 描述: 创建锁文件
# 参数: 无
# 返回: 0-成功, 1-失败(脚本已在运行)
create_lock_file()
{
    # 检查锁文件是否存在
    if [ -f "$LOCK_FILE" ]; then
        # 读取锁文件中的PID
        local lock_pid
        lock_pid=$(cat "$LOCK_FILE" 2>/dev/null)

        # 检查进程是否存在
        if [ -n "$lock_pid" ] && [ -d "/proc/$lock_pid" ]; then
            log_message "ERROR: Script is already running (PID=$lock_pid), please try again later"
            return 1
        else
            # 锁文件过期,删除旧锁文件
            log_message "WARNING: Found a stale lock file, cleaning it up..."
            rm -f "$LOCK_FILE"
        fi
    fi

    # 创建锁文件
    echo "$$" > "$LOCK_FILE"
    if [ $? -ne 0 ]; then
        log_message "ERROR: Failed to create the lock file"
        return 1
    fi

    return 0
}

# 描述: 清理锁文件
# 参数: 无
# 返回: 无
cleanup_lock_file()
{
    if [ -f "$LOCK_FILE" ]; then
        rm -f "$LOCK_FILE"
    fi
}

#===============================================================================
# 环境初始化函数
#===============================================================================
# 描述: 根据设备类型初始化环境变量
# 参数: 无
# 返回: 0-成功, 1-失败
init_environment()
{
    log_message "=========================================="
    log_message "Start initializing environment..."
    log_message "=========================================="

    # 检测设备类型
    if grep -q "VMS-B500" /proc/board_type 2>/dev/null; then
        log_message "Detected device type: VMS-B500"
        export LD_LIBRARY_PATH=/lib:/program/lib:/usr/lib/:$LD_LIBRARY_PATH
        export PATH=./:/bin:/usr/bin:/sbin:/usr/sbin:/program/bin:/usr/local/drbd-utils-9.1.0/sbin
        DB_THRESHOLD=$DB_THRESHOLD_B500
        BACKUP_ROOT_DIR=/var/fs_disk
    else
        log_message "Detected device type: B200"
        export PATH=/program/bin/:${PATH}
        export LD_LIBRARY_PATH=/program/lib/:${LD_LIBRARY_PATH}
        DB_THRESHOLD=$DB_THRESHOLD_DEFAULT
        BACKUP_ROOT_DIR=/var/coredump
    fi

    # 设置备份数据库路径
    BACKUP_DATABASE="$BACKUP_ROOT_DIR/database_VMSBK"

    # 设置数据库环境变量
    export PGUSER=postgres
    export PGPASSWORD=123456

    log_message "Environment initialization completed"
    log_message "  - Backup directory: $BACKUP_ROOT_DIR"
    log_message "  - Clean threshold: ${DB_THRESHOLD}MB"
    log_message "  - Backup database: $BACKUP_DATABASE"

    return 0
}

#===============================================================================
# 前置条件检查函数
#===============================================================================
# 描述: 检查脚本执行的前置条件
# 参数: 无
# 返回: 0-通过, 1-不通过
check_prerequisites()
{
    log_message "=========================================="
    log_message "Start checking prerequisites..."
    log_message "=========================================="

    # 检查root权限
    if [ "$(id -u)" -ne 0 ]; then
        log_message "ERROR: Root privileges are required to run this script"
        return 1
    fi

    # 检查关键命令是否存在
    local cmd_list="psql pg_ctl server.sh tar su find"
    for cmd in $cmd_list; do
        if ! command -v "$cmd" > /dev/null 2>&1; then
            log_message "ERROR: Required command '$cmd' not found"
            return 1
        fi
    done

    # 检查运行时数据库目录是否存在
    if [ ! -d "$RUNTIME_DATABASE" ]; then
        log_message "ERROR: Runtime database directory $RUNTIME_DATABASE does not exist"
        return 1
    fi

    # 检查备份目录是否存在且可写
    if [ ! -d "$BACKUP_ROOT_DIR" ]; then
        log_message "ERROR: Backup directory $BACKUP_ROOT_DIR does not exist"
        return 1
    fi

    # 检查/proc/board_type是否存在
    if [ ! -f /proc/board_type ]; then
        log_message "ERROR: /proc/board_type does not exist, unable to detect device type"
        return 1
    fi

    log_message "Prerequisite check passed"
    return 0
}

#===============================================================================
# 空间检查函数
#===============================================================================
# 描述: 检查运行时空间是否满足清理条件
# 参数: 无
# 返回: 0-满足条件, 1-不满足条件
check_runtime_space()
{
    # 强制清理模式,跳过空间检查
    if [ "$FORCE_CLEAN" -eq 1 ]; then
        log_message "INFO: Force clean mode enabled, skipping space check"
        return 0
    fi

    log_message "=========================================="
    log_message "Start checking runtime space usage..."
    log_message "=========================================="

    local mem_runtime
    mem_runtime=$(df -m | grep '/var/runtime$' | awk '{print $3}' | head -1)

    if [ -z "$mem_runtime" ]; then
        log_message "WARNING: Unable to get /var/runtime space usage, continuing"
        return 0
    fi

    log_message "Current runtime space usage: ${mem_runtime}MB, threshold: ${DB_THRESHOLD}MB"

    if [ "$mem_runtime" -lt "$DB_THRESHOLD" ]; then
        log_message "INFO: Runtime space usage (${mem_runtime}MB) is below the threshold (${DB_THRESHOLD}MB), no clean needed"
        return 1
    fi

    return 0
}

#===============================================================================
# 服务停止函数
#===============================================================================
# 描述: 停止所有相关服务
# 参数: 无
# 返回: 0-成功, 1-失败
stop_services()
{
    log_message "=========================================="
    log_message "Start stopping services..."
    log_message "=========================================="

    # 停止服务进程
    server.sh stop > /dev/null 2>&1

	# 检查服务状态
    log_message "Checking service status..."
    local core_status sdkadpt_status maintain_status anr_status
    core_status=$(server.sh status 2>/dev/null | grep "^core" | awk '{print $3}')
    sdkadpt_status=$(server.sh status 2>/dev/null | grep "^sdkadpt" | awk '{print $3}')
    maintain_status=$(server.sh status 2>/dev/null | grep "^maintain" | awk '{print $3}')
    anr_status=$(server.sh status 2>/dev/null | grep "^anr" | awk '{print $3}')

    if [ "$core_status" = "running" ] || [ "$sdkadpt_status" = "running" ] || [ "$maintain_status" = "running" ] || [ "$anr_status" = "running" ]; then
        log_message "WARNING: Services are still running, executing server.sh stop..."
        log_message "  - core: $core_status"
        log_message "  - sdkadpt: $sdkadpt_status"
        log_message "  - maintain: $maintain_status"
        log_message "  - anr_status: $anr_status"

        # 再次执行服务停止
        server.sh stop > /dev/null 2>&1

        # 再次检查服务状态
        core_status=$(server.sh status 2>/dev/null | grep "^core" | awk '{print $3}')
        sdkadpt_status=$(server.sh status 2>/dev/null | grep "^sdkadpt" | awk '{print $3}')
        maintain_status=$(server.sh status 2>/dev/null | grep "^maintain" | awk '{print $3}')
        anr_status=$(server.sh status 2>/dev/null | grep "^anr" | awk '{print $3}')
		
        log_message "Re-checking service status:"
        log_message "  - core: $core_status"
        log_message "  - sdkadpt: $sdkadpt_status"
        log_message "  - maintain: $maintain_status"
        log_message "  - anr_status: $anr_status"
        if [ "$core_status" = "running" ] || [ "$sdkadpt_status" = "running" ] || [ "$maintain_status" = "running" ] || [ "$anr_status" = "running" ]; then
            log_message "ERROR: Services are still not stopped, clean aborted"
            return 1
        fi

        log_message "Services stopped, continuing with clean"
    fi
	
    # B500设备特殊处理
    if grep -q "VMS-B500" /proc/board_type; then
        log_message "INFO: Stopping B500 services..."

        # 停止cip-starter进程
        stop_cip_services

        # 保险起见停止所有Java服务
        log_message "INFO: B500 device detected, stopping all Java services..."
        pkill -9 -f "java" 2>/dev/null
    fi

    # 等待通用服务停止(postgres由stop_postgres单独处理,不在此等待)
    wait_for_services_stopped
    if [ $? -ne 0 ]; then
        log_message "WARNING: Timed out waiting for business services to stop"
        return 1
    fi

    # 打印服务状态信息
    print_server_status

    return 0
}

# 描述: 停止cip-starter相关服务
# 参数: 无
# 返回: 0-成功, 1-超时
stop_cip_services()
{
    local services="cip-starter-das cip-starter-ram cip-starter-vis"
    local max_wait=30
    local count=0

    for service in $services; do
        log_message "INFO: Stopping $service ..."

        # 强制终止进程
        pkill -9 -f "$service" 2>/dev/null

        # 等待进程停止
        while pgrep -f "$service" > /dev/null 2>&1; do
            sleep 1
            count=$((count + 1))
            if [ "$count" -ge "$max_wait" ]; then
                log_message "WARNING: Timed out stopping $service"
                return 1
            fi
        done

        log_message "INFO: $service stopped"
    done

    return 0
}

# 描述: 等待所有服务停止
# 参数: 无
# 返回: 0-成功, 1-超时
wait_for_services_stopped()
{
    # 注意: postgres 不在此列表,数据库的停止/启动由 stop_postgres/start_postgres 专门负责
    local services="core sdkadpt anr maintain daemon"
    local max_wait=30
    local interval=2
    local elapsed=0

    log_message "INFO: Waiting for services to stop (max ${max_wait}s)..."

    while [ "$elapsed" -lt "$max_wait" ]; do
        local all_stopped=true

        for service in $services; do
            local status
            status=$(server.sh status 2>/dev/null | grep "^$service" | awk '{print $3}')
            if [ "$status" != "stopped" ]; then
                all_stopped=false
                break
            fi
        done

        if [ "$all_stopped" = true ]; then
            # 检查特殊进程
            if pgrep -f "cip-starter" > /dev/null 2>&1; then
                all_stopped=false
            fi
        fi

        if [ "$all_stopped" = true ]; then
            log_message "INFO: All services stopped"
            return 0
        fi

        sleep "$interval"
        elapsed=$((elapsed + interval))
        log_message "INFO: Waiting...(${elapsed}s/${max_wait}s)"
    done

    log_message "WARNING: Timed out waiting for services to stop"
    return 1
}

# 描述: 打印服务状态信息
# 参数: 无
# 返回: 无
print_server_status()
{
    log_message "=========================================="
    log_message "Service status information:"
    log_message "========================================== server.sh status"
    server.sh status 2>/dev/null | while read line; do log_message "$line"; done
    log_message "========================================== ps -ef |grep java"
    ps -ef |grep java 2>/dev/null | while read line; do log_message "$line"; done
    log_message "=========================================="
}

#===============================================================================
# 数据库操作函数
#===============================================================================
# 描述: 停止PostgreSQL数据库
# 参数: $1 - 数据库路径, $2 - 最大等待时间(秒)
# 返回: 0-成功, 1-失败
stop_postgres()
{
    local db_path=${1:-$RUNTIME_DATABASE}
    local max_wait=${2:-30}

    log_message "INFO: Stopping PostgreSQL (path: $db_path)..."

    # 检查目标数据库是否在运行,未运行则无需停止(以pg_ctl status为准)
    if ! su - postgres -c "/program/bin/pg_ctl -D $db_path status" > /dev/null 2>&1; then
        log_message "INFO: PostgreSQL is already stopped"

		# 打印服务状态信息
        print_server_status
        return 0
    fi

    # 停止所有可能的数据库实例
    su - postgres -c "/program/bin/pg_ctl -D $db_path stop -m fast" 2>/dev/null
    su - postgres -c "/program/bin/pg_ctl -D /var/fs_disk/database stop -m fast" 2>/dev/null
    su - postgres -c "/program/bin/pg_ctl -D /var/coredump/database_VMSBK stop -m fast" 2>/dev/null
    su - postgres -c "/program/bin/pg_ctl -D /var/fs_disk/database_VMSBK stop -m fast" 2>/dev/null

    # 等待数据库停止
    local count=0
    while [ "$count" -lt "$max_wait" ]; do
        if ! su - postgres -c "/program/bin/pg_ctl -D $db_path status" > /dev/null 2>&1; then
            log_message "INFO: PostgreSQL stopped successfully"
            # 打印服务状态信息
            print_server_status
            return 0
        fi
        sleep 1
        count=$((count + 1))
        log_message "INFO: Waiting for PostgreSQL to stop...(${count}s/${max_wait}s)"
    done

    # 超时后尝试强制终止
    log_message "WARNING: Timed out stopping PostgreSQL, trying to force kill..."
    pkill -9 -f "postgres:" 2>/dev/null
    sleep 2

    # 再次检查
    if ! su - postgres -c "/program/bin/pg_ctl -D $db_path status" > /dev/null 2>&1; then
        log_message "INFO: PostgreSQL force stopped successfully"
        # 打印服务状态信息
        print_server_status
        return 0
    fi

    log_message "ERROR: Failed to stop PostgreSQL"
    return 1
}

# 描述: 等待PostgreSQL就绪(针对指定实例,避免误判其他运行中的实例)
# 说明: 历史版本仅用 psql nddb 探测,若设备上同时存在多个实例(如备份库/运行库),
#       可能连接到非目标实例而误判就绪。此处从目标实例的 postgresql.conf 读取端口
#       与socket目录,并要求目标实例处于运行状态且能通过其端口真正建立连接
# 参数: $1 - 数据库路径, $2 - 最大等待时间(秒,默认600)
# 返回: 0-就绪, 1-超时
wait_postgres_ready()
{
    local db_path=${1:-$BACKUP_DATABASE}
    local max_wait=${2:-600}
    local count=0
    local port socket_dir conn_opts

    # 从目标实例配置中解析端口(支持带引号/注释写法),缺省5432
    port=$(sed -n "s/^[[:space:]]*port[[:space:]]*=[[:space:]]*//p" "$db_path/postgresql.conf" 2>/dev/null | head -1 | awk '{print $1}' | tr -d "'\"")
    [ -z "$port" ] && port=5432

    # 解析socket目录(兼容 unix_socket_directories / unix_socket_directory),未配置则用默认位置
    socket_dir=$(sed -n -e "s/^[[:space:]]*unix_socket_directories[[:space:]]*=[[:space:]]*//p" -e "s/^[[:space:]]*unix_socket_directory[[:space:]]*=[[:space:]]*//p" "$db_path/postgresql.conf" 2>/dev/null | head -1 | awk '{print $1}' | tr -d "'\"")
    conn_opts="-p $port"
    [ -n "$socket_dir" ] && conn_opts="$conn_opts -h $socket_dir"

    log_message "INFO: Waiting for PostgreSQL readiness (path: $db_path, port: $port)..."

    while [ "$count" -lt "$max_wait" ]; do
        # 双重校验: 目标实例处于运行状态 且 通过该实例端口/socket能真正建立连接
        if su - postgres -c "/program/bin/pg_ctl -D $db_path status" > /dev/null 2>&1 && \
           /program/bin/psql $conn_opts nddb -c "\q" > /dev/null 2>&1; then
            log_message "INFO: PostgreSQL started successfully (path: $db_path)"
            return 0
        fi
        sleep 1
        count=$((count + 1))

        # 每3秒打印一次状态
        if [ $((count % 3)) -eq 0 ]; then
            log_message "INFO: Waiting for the database to become ready...(${count}s/${max_wait}s)"
        fi
    done

    log_message "WARNING: PostgreSQL not ready within ${max_wait}s (path: $db_path)"
    return 1
}

# 描述: 使用 pg_resetxlog 重置WAL控制信息后重新启动数据库
# 说明: 仅当数据库无法正常启动时作为最后手段使用。该操作会丢弃未重放的WAL,
#       可能导致数据不一致,重置成功后应尽快导出数据并重建
# 参数: $1 - 数据库路径
# 返回: 0-成功, 1-失败
reset_wal_and_restart()
{
    local db_path=$1

    # pg_resetxlog 要求数据库必须处于停止状态
    log_message "WARNING: About to run pg_resetxlog, making sure the database is stopped first..."
    stop_postgres "$db_path" 30
    if [ $? -ne 0 ]; then
        log_message "ERROR: Failed to stop the database, cannot run pg_resetxlog"
        return 1
    fi

    # 二次确认: pg_ctl status 判定未运行不代表进程已退出,残留进程会让 pg_resetxlog 拒绝执行
    if pgrep -f "postgres.*-D *$db_path" > /dev/null 2>&1; then
        log_message "WARNING: Found residual postgres processes for $db_path, trying to kill them..."
        pkill -9 -f "postgres.*-D *$db_path" 2>/dev/null

        local kill_wait=0
        while pgrep -f "postgres.*-D *$db_path" > /dev/null 2>&1; do
            sleep 1
            kill_wait=$((kill_wait + 1))
            if [ "$kill_wait" -ge 10 ]; then
                log_message "ERROR: Residual postgres processes are still alive, cannot run pg_resetxlog"
                return 1
            fi
        done
        log_message "INFO: Residual postgres processes terminated"
    fi

    # 清理残留锁文件: pg_resetxlog 会读取 postmaster.pid, 该PID被系统复用时会误判服务仍在运行
    if [ -f "$db_path/postmaster.pid" ]; then
        log_message "WARNING: Found stale postmaster.pid, removing it..."
        su - postgres -c "rm -f $db_path/postmaster.pid" 2>/dev/null
        if [ -f "$db_path/postmaster.pid" ]; then
            log_message "ERROR: Failed to remove stale postmaster.pid"
            return 1
        fi
        log_message "INFO: Stale postmaster.pid removed"
    fi

    # 检测可用的重置工具(PG10前为pg_resetxlog)
    local reset_cmd=""
    if [ -x /program/bin/pg_resetxlog ]; then
        reset_cmd="/program/bin/pg_resetxlog"
    else
        log_message "ERROR: pg_resetxlog tool not found"
        return 1
    fi

    # 以数据目录属主(postgres)身份执行重置,-f 表示强制执行
    # 注意: 不同PG版本的工具对数据目录参数形式不同,部分版本仅支持位置参数
    log_message "WARNING: Trying $reset_cmd with positional data directory($db_path) argument..."
    su - postgres -c "$reset_cmd -f $db_path" >> "$CLEAN_RUNTIME_LOG" 2>&1
    if [ $? -ne 0 ]; then
        log_message "ERROR: pg_resetxlog failed"
        return 1
    fi
    log_message "INFO: pg_resetxlog completed"

    # 重置后重新启动数据库
    log_message "INFO: Restarting the database after pg_resetxlog (path: $db_path)..."
    su - postgres -c "/program/bin/pg_ctl -D $db_path start" >> "$CLEAN_RUNTIME_LOG" 2>&1
    if [ $? -ne 0 ]; then
        log_message "ERROR: Failed to start the database($db_path) after pg_resetxlog"
        return 1
    fi

    # 再次等待就绪
    if wait_postgres_ready "$db_path" 600; then
        log_message "INFO: Database started successfully after pg_resetxlog"
        return 0
    fi

    log_message "ERROR: Database is still not ready after pg_resetxlog"
    return 1
}

# 描述: 启动PostgreSQL数据库(启动失败或超时后使用 pg_resetxlog 兜底)
# 参数: $1 - 数据库路径, $2 - 最大等待时间(秒,默认600)
# 返回: 0-成功, 1-失败
start_postgres()
{
    local db_path=${1:-$BACKUP_DATABASE}
    local max_wait=${2:-600}  # 默认10分钟

    log_message "INFO: Starting PostgreSQL (path: $db_path)..."

    # 检查数据库目录是否存在
    if [ ! -d "$db_path" ]; then
        log_message "ERROR: Database directory does not exist: $db_path"
        return 1
    fi

    # 若数据库已在运行,直接返回成功(避免重复启动导致pg_ctl报错)
    if su - postgres -c "/program/bin/pg_ctl -D $db_path status" > /dev/null 2>&1; then
        log_message "INFO: PostgreSQL is already running, no need to start"
        return 0
    fi

    # 启动数据库(stderr写入日志,便于定位启动失败的真实原因)
    # 先打印路径标记,便于在日志中区分该段 pg_ctl 原始输出对应的数据库实例
    log_message "INFO: pg_ctl start output for the database (path: $db_path):"
    su - postgres -c "/program/bin/pg_ctl -D $db_path start" >> "$CLEAN_RUNTIME_LOG" 2>&1
    if [ $? -ne 0 ]; then
        # 启动失败(如WAL/pg_control损坏),尝试用 pg_resetxlog 兜底
        log_message "WARNING: Failed to start PostgreSQL, trying to repair with pg_resetxlog..."
        reset_wal_and_restart "$db_path"
        return $?
    fi

    # 等待数据库就绪
    if wait_postgres_ready "$db_path" "$max_wait"; then
        return 0
    fi

    # 超时未就绪,使用 pg_resetxlog 兜底后再尝试
    log_message "WARNING: Timed out starting PostgreSQL (${max_wait}s), trying to repair with pg_resetxlog..."
    reset_wal_and_restart "$db_path"
    return $?
}

# 描述: 确保PostgreSQL正在运行
# 说明: 导出(pg_dump)和导入(DROP/CREATE DATABASE、pg_restore)均依赖数据库连接,
#       执行这些操作前必须保证数据库服务处于运行状态
# 参数: $1 - 数据库路径(可选,默认$BACKUP_DATABASE)
# 返回: 0-成功, 1-失败
ensure_postgres_running()
{
    local db_path=${1:-$BACKUP_DATABASE}
    # start_postgres 内部会先检查数据库是否已运行,已运行则直接返回成功
    start_postgres "$db_path"
    if [ $? -ne 0 ]; then
        log_message "ERROR: Failed to start the database"
        return 1
    fi
    return 0
}

# 描述: 确保没有其他进程连接数据库
# 说明: 导入流程会执行DROP DATABASE,要求数据库无活动连接,否则DROP会失败
# 参数: $1 - 目标数据库名(可选,默认nddb)
# 返回: 0-无连接(或无法检测), 1-仍有连接
ensure_no_db_connections()
{
    local db_name=${1:-nddb}
    local conn_count

    # 查询活动连接数(排除当前查询自身);查询失败(如默认库不存在)时不阻断
    conn_count=$(su - postgres -c "PGUSER=postgres PGPASSWORD=123456 /program/bin/psql -t -A -d postgres -c \"SELECT count(*) FROM pg_stat_activity WHERE datname='$db_name' AND pid <> pg_backend_pid();\"" 2>/dev/null | tr -d '[:space:]')

    if [ -z "$conn_count" ]; then
        log_message "WARNING: Unable to query the database connection count, skipping connection check"
        return 0
    fi

    if [ "$conn_count" -gt 0 ]; then
        log_message "WARNING: Detected $conn_count connection(s) holding database $db_name, trying to terminate..."
        su - postgres -c "PGUSER=postgres PGPASSWORD=123456 /program/bin/psql -d postgres -c \"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='$db_name' AND pid <> pg_backend_pid();\"" > /dev/null 2>&1
        sleep 2

        conn_count=$(su - postgres -c "PGUSER=postgres PGPASSWORD=123456 /program/bin/psql -t -A -d postgres -c \"SELECT count(*) FROM pg_stat_activity WHERE datname='$db_name' AND pid <> pg_backend_pid();\"" 2>/dev/null | tr -d '[:space:]')
        if [ -n "$conn_count" ] && [ "$conn_count" -gt 0 ]; then
            log_message "ERROR: Still $conn_count connection(s) holding database $db_name"
            return 1
        fi
    fi

    log_message "INFO: No other connections holding database $db_name"
    return 0
}

#===============================================================================
# 备份管理函数
#===============================================================================
# 描述: 备份运行时数据库和配置
# 参数: 无
# 返回: 0-成功, 1-失败
backup_database()
{
    log_message "=========================================="
    log_message "Start backing up the database..."
    log_message "=========================================="

    # 备份前必须停止数据库,确保复制到静止状态的数据库目录(运行中复制会导致备份损坏)
    stop_postgres "$RUNTIME_DATABASE" 30
    if [ $? -ne 0 ]; then
        log_message "ERROR: Failed to stop the database, cannot perform backup"
        return 1
    fi

    # 统计清理前空间
    local before_size
    before_size=$(du -sm "$RUNTIME_DATABASE" 2>/dev/null | awk '{print $1}')
    log_message "Database size before clean: ${before_size}MB"

    # 生成带日期的备份文件名
    local backup_date
    backup_date=$(date '+%Y%m%d_%H%M%S')
    local db_backup_file="$BACKUP_ROOT_DIR/database_bk_${backup_date}.tar.gz"
    local cfg_backup_file="$BACKUP_ROOT_DIR/cfg_bk_${backup_date}.tar.gz"

    cd "$BACKUP_ROOT_DIR" || {
        log_message "ERROR: Failed to enter the backup directory"
        return 1
    }

    # 备份数据库(先生成新备份,成功后再清理旧备份,避免备份失败时新旧备份同时丢失)
    log_message "INFO: Backing up database to $db_backup_file..."

    # 相对打包(包内为数据库内容,便于解压还原)
    tar -zcvf "$db_backup_file" -C "$RUNTIME_DATABASE" . 2>/dev/null
    if [ $? -ne 0 ]; then
        log_message "ERROR: Database backup failed"
        return 1
    fi
    log_message "INFO: Database backup created: $db_backup_file"

    # 备份配置
    log_message "INFO: Backing up configuration to $cfg_backup_file..."
    if [ -d /var/runtime/cfg ]; then
        tar -zcvf "$cfg_backup_file" /var/runtime/cfg 2>/dev/null
        if [ $? -ne 0 ]; then
            log_message "WARNING: Configuration backup failed"
        else
            log_message "INFO: Configuration backup created: $cfg_backup_file"
        fi
    else
        log_message "WARNING: Configuration directory /var/runtime/cfg does not exist, skipping configuration backup"
    fi

    # 删除旧备份(仅保留本次新生成的备份文件)
    log_message "INFO: Deleting old backups..."
    rm -rf "$BACKUP_DATABASE"
    find "$BACKUP_ROOT_DIR" -maxdepth 1 -type f -name 'database_bk_*.tar.gz' ! -name "database_bk_${backup_date}.tar.gz" -exec rm -f {} \;
    find "$BACKUP_ROOT_DIR" -maxdepth 1 -type f -name 'cfg_bk_*.tar.gz' ! -name "cfg_bk_${backup_date}.tar.gz" -exec rm -f {} \;

    # 复制生成工作副本(直接复制原始目录)
    log_message "INFO: Creating working copy $BACKUP_DATABASE..."
    su - postgres -c "chmod 700 $RUNTIME_DATABASE"
    rm -rf "$BACKUP_DATABASE"
    cp -a "$RUNTIME_DATABASE" "$BACKUP_DATABASE"
    if [ $? -ne 0 ]; then
        log_message "ERROR: Failed to create the working copy"
        return 1
    fi

    # 修正属主(tar以root解压,确保工作副本归属postgres)
    log_message "INFO: Working copy created successfully"
    chown -R postgres:postgres "$BACKUP_DATABASE"
    su - postgres -c "chmod 700 $BACKUP_DATABASE"

    # 验证备份
    if [ ! -d "$BACKUP_DATABASE" ]; then
        log_message "ERROR: Backup verification failed"
        return 1
    fi

    local backup_size
    backup_size=$(du -sm "$BACKUP_DATABASE" 2>/dev/null | awk '{print $1}')
    log_message "Backup completed, backup size: ${backup_size}MB"

    # 打印备份目录信息
    log_message "=========================================="
    log_message "Backup directory information:"
    ls -lh "$BACKUP_ROOT_DIR" 2>/dev/null | while read line; do log_message "$line"; done
    log_message "=========================================="

    return 0
}

# 描述: 恢复数据库
# 参数: 无
# 返回: 0-成功, 1-失败
restore_database()
{
    log_message "=========================================="
    log_message "Restoring database..."
    log_message "=========================================="

    # 确保postgres已停止
    stop_postgres "$BACKUP_DATABASE" 30

    # 将原数据库备份到临时备份目录,复制失败时可回滚,避免原库丢失
    # 注意: 暂存目录必须位于备份分区(空间已在主流程校验过)。历史版本放在
    #       /var/runtime/database_old,该分区本就空间不足,导致恢复时报
    #       "No space left on device"而失败
    local old_database="$BACKUP_ROOT_DIR/database_old"
    rm -rf "$old_database"
    if [ -d "$RUNTIME_DATABASE" ]; then
        log_message "INFO: Staging the original database to $old_database..."
        mv "$RUNTIME_DATABASE" "$old_database"
        if [ $? -ne 0 ]; then
            log_message "ERROR: Failed to stage the original database"
            return 1
        fi
    fi

    # 复制备份数据库
    log_message "INFO: Copying database to $RUNTIME_DATABASE..."
    su - postgres -c "cp -rf $BACKUP_DATABASE $RUNTIME_DATABASE"

    if [ $? -ne 0 ]; then
        log_message "ERROR: Failed to restore the database, rolling back the original database..."
        rm -rf "$RUNTIME_DATABASE"
        mv "$old_database" "$RUNTIME_DATABASE"
        return 1
    fi

    su - postgres -c "chmod 700 $RUNTIME_DATABASE"

    # 验证恢复
    if [ ! -d "$RUNTIME_DATABASE" ]; then
        log_message "ERROR: Database restore verification failed, rolling back the original database..."
        rm -rf "$RUNTIME_DATABASE"
        mv "$old_database" "$RUNTIME_DATABASE"
        return 1
    fi

    # 恢复成功,删除临时备份
    log_message "INFO: Database restored successfully, cleaning the temporary backup..."
    rm -rf "$old_database"

    return 0
}

#===============================================================================
# 清理执行函数
#===============================================================================
# 描述: 执行第一阶段清理(依次执行 VACUUM FULL 和 db_clean.sh)
# 参数: 无
# 返回: 0-清理成功, 1-失败, 2-清理后仍超阈值需第二阶段
execute_first_stage_clean()
{
    log_message "=========================================="
    log_message "Stage 1 clean: VACUUM FULL + db_clean.sh..."
    log_message "=========================================="

    # 确保数据库已启动(若已运行则跳过,避免重复启动失败)
    ensure_postgres_running "$BACKUP_DATABASE"
    if [ $? -ne 0 ]; then
        log_message "ERROR: Failed to start the database"
        return 1
    fi

    # 执行 VACUUM FULL
    log_message "INFO: Running VACUUM FULL..."
    /program/bin/psql nddb << EOF
\connect nddb;
SET client_encoding to 'UTF8';
VACUUM FULL;
EOF

    if [ $? -ne 0 ]; then
        log_message "WARNING: VACUUM FULL failed"
    else
        log_message "INFO: VACUUM FULL completed successfully"
    fi

    sleep 3

    # 执行db_clean.sh
    log_message "INFO: Running db_clean.sh..."
    db_clean.sh
    if [ $? -ne 0 ]; then
        log_message "WARNING: db_clean.sh failed"
    else
        log_message "INFO: db_clean.sh completed successfully"
    fi

    # 统计清理后空间
    local after_size
    after_size=$(du -sm "$BACKUP_DATABASE" 2>/dev/null | awk '{print $1}')
    local before_size
    before_size=$(du -sm "$RUNTIME_DATABASE" 2>/dev/null | awk '{print $1}')

    log_message "Stage 1 clean completed"
    log_message "  - Before clean: ${before_size}MB"
    log_message "  - After clean: ${after_size}MB"
    log_message "  - Threshold: ${DB_THRESHOLD}MB"

    # 判断是否需要第二阶段清理
    if [ "$after_size" -gt "$DB_THRESHOLD" ]; then
        log_message "INFO: Space usage still exceeds the threshold after stage 1 clean, stage 2 clean required"
        return 2
    fi

    return 0
}

# 描述: 执行第二阶段清理(数据库导出导入)
# 参数: 无
# 返回: 0-成功, 1-失败, 2-失败但可继续
execute_second_stage_clean()
{
    log_message "=========================================="
    log_message "Stage 2 clean: database export/import..."
    log_message "=========================================="

    # 导出前确保 PostgreSQL 正在运行
    ensure_postgres_running "$BACKUP_DATABASE"
    if [ $? -ne 0 ]; then
        log_message "ERROR: Failed to start the database, cannot perform export"
        return 1
    fi

    # 检查空间
    local free_space
    free_space=$(df -m | grep "$BACKUP_ROOT_DIR$" | awk '{print $4}' | head -1)
    local db_size
    db_size=$(du -sm "$BACKUP_DATABASE" 2>/dev/null | awk '{print $1}')

    log_message "Free space: ${free_space}MB, database size: ${db_size}MB"

    if [ "$free_space" -lt "$db_size" ]; then
        log_message "ERROR: Insufficient space, cannot perform export"
        return 1
    fi

    # 导出数据库
    log_message "INFO: Exporting database..."
    local export_file="vms_db_export"
    rm -rf "$BACKUP_ROOT_DIR/$export_file"

    cd "$BACKUP_ROOT_DIR" || return 1
    dbinout.sh export "$BACKUP_ROOT_DIR" "$export_file"
    if [ $? -ne 0 ]; then
        log_message "ERROR: Database export failed"
        return 1
    fi

    # 检查导出文件
    log_message "INFO: Checking whether the export file exists..."
    if [ ! -f "$BACKUP_ROOT_DIR/$export_file.tar.gz" ]; then
        log_message "ERROR: Export file does not exist"
        return 1
    fi
    log_message "INFO: Export file check passed"

    # 导入同样依赖数据库连接(DROP/CREATE DATABASE、pg_restore),确保postgres正在运行
    # 注意: 此处绝对不能停止数据库,否则导入必然失败
    log_message "INFO: Ensuring the database is running..."
    ensure_postgres_running "$BACKUP_DATABASE"
    if [ $? -ne 0 ]; then
        log_message "ERROR: Failed to start the database, cannot perform import"
        return 1
    fi
    log_message "INFO: Database is already running"

    # 确认数据库无其他连接占用(DROP DATABASE要求无活动连接,否则导入会失败)
    log_message "INFO: Checking database connections..."
    ensure_no_db_connections "nddb"
    if [ $? -ne 0 ]; then
        log_message "ERROR: Database is still in use, import aborted (backup data preserved)"
        return 2
    fi

    # 导入数据库
    log_message "INFO: Importing database..."
    dbinout.sh import "$BACKUP_ROOT_DIR/$export_file.tar.gz"
    if [ $? -ne 0 ]; then
        log_message "ERROR: Database import failed"
        return 2  # 返回2表示导入失败但保留备份
    fi

    # 统计清理后空间
    local after_size
    after_size=$(du -sm "$BACKUP_DATABASE" 2>/dev/null | awk '{print $1}')

    log_message "Stage 2 clean completed"
    log_message "  - After clean: ${after_size}MB"
    log_message "  - Threshold: ${DB_THRESHOLD}MB"

    if [ "$after_size" -gt "$DB_THRESHOLD" ]; then
        log_message "WARNING: Space usage still exceeds the threshold after stage 2 clean"
        return 1
    fi

    return 0
}

#===============================================================================
# 主流程
#===============================================================================
main()
{
    log_message ""
    log_message "################################################################"
    log_message "#           Runtime Database Manual Clean Script V2.4          #"
    log_message "################################################################"
    log_message ""

    # 检查锁文件
    create_lock_file
    if [ $? -ne 0 ]; then
        log_message "ERROR: Failed to acquire the lock file, the script may already be running"
        exit 1
    fi

    # 注册退出清理函数
    trap 'cleanup_lock_file' EXIT

    # 初始化环境
    init_environment
    if [ $? -ne 0 ]; then
        script_error_exit "Environment initialization failed"
    fi

    # 检查前置条件
    check_prerequisites
    if [ $? -ne 0 ]; then
        script_error_exit "Prerequisite check failed"
    fi

    # 检查运行时空间
    check_runtime_space
    if [ $? -ne 0 ]; then
        log_message "INFO: No clean needed, exiting script"
        cleanup_lock_file
        exit 0
    fi

    # 停止服务
    stop_services
    if [ $? -ne 0 ]; then
        script_error_exit "Failed to stop business services"
    fi

    # 检查空间是否足够进行备份
    local mem_runtime_db
    mem_runtime_db=$(du -sm "$RUNTIME_DATABASE" 2>/dev/null | awk '{print $1}')
    local mem_backup
    mem_backup=$(df -m | grep "$BACKUP_ROOT_DIR$" | awk '{print $4}' | head -1)

    log_message "Database size: ${mem_runtime_db}MB, backup space: ${mem_backup}MB"

    if [ "$mem_runtime_db" -gt "$mem_backup" ]; then
        log_message "INFO: Insufficient backup space, cleaning historical backups (keeping script files)..."
        rm -rf "$BACKUP_DATABASE"
        find "$BACKUP_ROOT_DIR" -maxdepth 1 -type f ! -name "*.sh" -exec rm -f {} \;

        mem_backup=$(df -m | grep "$BACKUP_ROOT_DIR$" | awk '{print $4}' | head -1)
        if [ "$mem_runtime_db" -gt "$mem_backup" ]; then
            script_error_exit "Insufficient backup space, cannot perform clean"
        fi
    fi

    # 备份数据库
    backup_database
    if [ $? -ne 0 ]; then
        # 备份失败时数据库处于停止状态,需重新拉起原库,保证系统可用(原库数据未被修改)
        log_message "WARNING: Database backup failed, trying to bring the database back up to keep the system available..."
        # 打印备份目录信息
        log_message "=========================================="
        log_message "Backup directory information (before bringing the database up):"
        ls -lh "$BACKUP_ROOT_DIR" 2>/dev/null | while read line; do log_message "$line"; done
        log_message "=========================================="
        start_postgres "$RUNTIME_DATABASE"
        script_error_exit "Database backup failed"
    fi

    # 第一阶段清理
    execute_first_stage_clean
    local first_stage_result=$?

    if [ "$first_stage_result" -eq 2 ]; then
        # 第一阶段后仍超阈值,进入第二阶段(数据库导出导入)
        execute_second_stage_clean
        local second_stage_result=$?

        if [ "$second_stage_result" -ne 0 ]; then
            if [ "$second_stage_result" -eq 2 ]; then
                log_message "WARNING: Import failed, backup data preserved"
            fi
            log_message "=========================================="
            script_error_exit "ERROR: Stage 2 clean failed. Please keep the current state, DO NOT reboot, and contact the developer for support", 1
        fi
    elif [ "$first_stage_result" -ne 0 ]; then
        # 第一阶段失败时,原库(/var/runtime/database)未被修改但处于停止状态,
        # 必须重新拉起,否则脚本退出后设备将处于无数据库可用状态
        log_message "WARNING: Stage 1 clean failed, trying to bring the original database back up..."
        start_postgres "$RUNTIME_DATABASE"
        script_error_exit "Stage 1 clean failed"
    fi

    # 停止数据库
    stop_postgres "$BACKUP_DATABASE" 30

    # 恢复数据库
    restore_database
    if [ $? -ne 0 ]; then
        # 恢复失败时已回滚原库,重新拉起数据库保证系统可用
        log_message "WARNING: Database restore failed, trying to bring the database back up..."
        # 打印备份目录信息
        log_message "=========================================="
        log_message "Backup directory information (before bringing the database up):"
        ls -lh "$BACKUP_ROOT_DIR" 2>/dev/null | while read line; do log_message "$line"; done
        log_message "=========================================="
        start_postgres "$RUNTIME_DATABASE"
        script_error_exit "Database restore failed"
    fi

    # 清理环境变量
    unset PGUSER
    unset PGPASSWORD

    # 验证最终结果
    if [ -d "$RUNTIME_DATABASE" ]; then
        local final_size
        final_size=$(du -sm "$RUNTIME_DATABASE" 2>/dev/null | awk '{print $1}')
        log_message ""
        log_message "##################################################"
        log_message "#         Clean completed successfully!                        #"
        log_message "##################################################"
        log_message "Final database size: ${final_size}MB"
        log_message "Backup location: $BACKUP_DATABASE"

        # 清理备份数据库(清理成功且已恢复到/var/runtime,备份可删除)
        log_message "INFO: Cleaning the backup database..."
        rm -rf "$BACKUP_DATABASE"
        log_message "INFO: Backup database cleaned"

        log_message "The system will reboot soon..."
        log_message ""

        cleanup_lock_file
        sync
        reboot
    else
        script_error_exit "Database restore verification failed"
    fi
}

# 执行主流程
main
