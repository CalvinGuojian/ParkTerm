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
    log_message "#              脚本执行失败,即将退出!             #"
    log_message "##################################################"
    log_message "错误原因: $1"
    log_message "日志文件: $CLEAN_RUNTIME_LOG"
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
            log_message "ERROR: 脚本已在运行(PID=$lock_pid), 请稍后重试"
            return 1
        else
            # 锁文件过期,删除旧锁文件
            log_message "WARNING: 发现过期锁文件,正在清理..."
            rm -f "$LOCK_FILE"
        fi
    fi

    # 创建锁文件
    echo "$$" > "$LOCK_FILE"
    if [ $? -ne 0 ]; then
        log_message "ERROR: 无法创建锁文件"
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
    log_message "开始初始化环境..."
    log_message "=========================================="

    # 检测设备类型
    if grep -q "VMS-B500" /proc/board_type 2>/dev/null; then
        log_message "检测到设备类型: VMS-B500"
        export LD_LIBRARY_PATH=/lib:/program/lib:/usr/lib/:$LD_LIBRARY_PATH
        export PATH=./:/bin:/usr/bin:/sbin:/usr/sbin:/program/bin:/usr/local/drbd-utils-9.1.0/sbin
        DB_THRESHOLD=$DB_THRESHOLD_B500
        BACKUP_ROOT_DIR=/var/fs_disk
    else
        log_message "检测到设备类型: B200"
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

    log_message "环境初始化完成"
    log_message "  - 备份目录: $BACKUP_ROOT_DIR"
    log_message "  - 清理阈值: ${DB_THRESHOLD}MB"
    log_message "  - 备份数据库: $BACKUP_DATABASE"

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
    log_message "开始检查前置条件..."
    log_message "=========================================="

    # 检查root权限
    if [ "$(id -u)" -ne 0 ]; then
        log_message "ERROR: 需要root权限执行此脚本"
        return 1
    fi

    # 检查关键命令是否存在
    local cmd_list="psql pg_ctl server.sh tar su find"
    for cmd in $cmd_list; do
        if ! command -v "$cmd" > /dev/null 2>&1; then
            log_message "ERROR: 关键命令 '$cmd' 不存在"
            return 1
        fi
    done

    # 检查运行时数据库目录是否存在
    if [ ! -d "$RUNTIME_DATABASE" ]; then
        log_message "ERROR: 运行时数据库目录 $RUNTIME_DATABASE 不存在"
        return 1
    fi

    # 检查备份目录是否存在且可写
    if [ ! -d "$BACKUP_ROOT_DIR" ]; then
        log_message "ERROR: 备份目录 $BACKUP_ROOT_DIR 不存在"
        return 1
    fi

    # 检查/proc/board_type是否存在
    if [ ! -f /proc/board_type ]; then
        log_message "ERROR: /proc/board_type 文件不存在,无法识别设备类型"
        return 1
    fi

    log_message "前置条件检查通过"
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
        log_message "INFO: 强制清理模式,跳过空间检查"
        return 0
    fi

    log_message "=========================================="
    log_message "开始检查运行时空间..."
    log_message "=========================================="

    local mem_runtime
    mem_runtime=$(df -m | grep '/var/runtime$' | awk '{print $3}' | head -1)

    if [ -z "$mem_runtime" ]; then
        log_message "WARNING: 无法获取/var/runtime空间,将继续执行"
        return 0
    fi

    log_message "当前运行时空间: ${mem_runtime}MB"
    log_message "清理阈值: ${DB_THRESHOLD}MB"

    if [ "$mem_runtime" -lt "$DB_THRESHOLD" ]; then
        log_message "INFO: 运行时空间(${mem_runtime}MB)小于阈值(${DB_THRESHOLD}MB),无需清理"
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
    log_message "开始停止服务..."
    log_message "=========================================="

    # 停止服务进程
    server.sh stop > /dev/null 2>&1

	# 检查服务状态
    log_message "检查服务状态..."
    local core_status sdkadpt_status maintain_status anr_status
    core_status=$(server.sh status 2>/dev/null | grep "^core" | awk '{print $3}')
    sdkadpt_status=$(server.sh status 2>/dev/null | grep "^sdkadpt" | awk '{print $3}')
    maintain_status=$(server.sh status 2>/dev/null | grep "^maintain" | awk '{print $3}')
    anr_status=$(server.sh status 2>/dev/null | grep "^anr" | awk '{print $3}')

    if [ "$core_status" = "running" ] || [ "$sdkadpt_status" = "running" ] || [ "$maintain_status" = "running" ] || [ "$anr_status" = "running" ]; then
        log_message "WARNING: 服务仍在运行,正在执行 server.sh stop..."
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
		
        log_message "重新检查服务状态:"
        log_message "  - core: $core_status"
        log_message "  - sdkadpt: $sdkadpt_status"
        log_message "  - maintain: $maintain_status"
        log_message "  - anr_status: $anr_status"
        if [ "$core_status" = "running" ] || [ "$sdkadpt_status" = "running" ] || [ "$maintain_status" = "running" ] || [ "$anr_status" = "running" ]; then
            log_message "ERROR: 服务仍未停止,清理操作中止"
            return 1
        fi

        log_message "服务已停止,继续执行清理"
    fi
	
    # B500设备特殊处理
    if grep -q "VMS-B500" /proc/board_type; then
        log_message "INFO: 停止 B500 服务..."

        # 停止cip-starter进程
        stop_cip_services

        # 保险起见停止所有Java服务
        log_message "INFO: B500设备,停止所有Java服务..."
        pkill -9 -f "java" 2>/dev/null
    fi

    # 等待通用服务停止(postgres由stop_postgres单独处理,不在此等待)
    wait_for_services_stopped
    if [ $? -ne 0 ]; then
        log_message "WARNING: 业务服务停止超时"
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
        log_message "INFO: 停止 $service ..."

        # 强制终止进程
        pkill -9 -f "$service" 2>/dev/null

        # 等待进程停止
        while pgrep -f "$service" > /dev/null 2>&1; do
            sleep 1
            count=$((count + 1))
            if [ "$count" -ge "$max_wait" ]; then
                log_message "WARNING: $service 停止超时"
                return 1
            fi
        done

        log_message "INFO: $service 已停止"
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

    log_message "INFO: 等待服务停止(最多${max_wait}秒)..."

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
            log_message "INFO: 所有服务已停止"
            return 0
        fi

        sleep "$interval"
        elapsed=$((elapsed + interval))
        log_message "INFO: 等待中...(${elapsed}s/${max_wait}s)"
    done

    log_message "WARNING: 服务停止超时"
    return 1
}

# 描述: 打印服务状态信息
# 参数: 无
# 返回: 无
print_server_status()
{
    log_message "=========================================="
    log_message "服务状态信息:"
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

    log_message "INFO: 停止 PostgreSQL (路径: $db_path)..."

    # 检查目标数据库是否在运行,未运行则无需停止(以pg_ctl status为准)
    if ! su - postgres -c "/program/bin/pg_ctl -D $db_path status" > /dev/null 2>&1; then
        log_message "INFO: PostgreSQL 已停止"

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
            log_message "INFO: PostgreSQL 停止成功"
            # 打印服务状态信息
            print_server_status
            return 0
        fi
        sleep 1
        count=$((count + 1))
        log_message "INFO: 等待 PostgreSQL 停止...(${count}s/${max_wait}s)"
    done

    # 超时后尝试强制终止
    log_message "WARNING: PostgreSQL 停止超时,尝试强制终止..."
    pkill -9 -f "postgres:" 2>/dev/null
    sleep 2

    # 再次检查
    if ! su - postgres -c "/program/bin/pg_ctl -D $db_path status" > /dev/null 2>&1; then
        log_message "INFO: PostgreSQL 强制停止成功"
        # 打印服务状态信息
        print_server_status
        return 0
    fi

    log_message "ERROR: PostgreSQL 停止失败"
    return 1
}

# 描述: 等待PostgreSQL就绪
# 参数: $1 - 最大等待时间(秒)
# 返回: 0-就绪, 1-超时
wait_postgres_ready()
{
    local max_wait=${1:-600}
    local count=0

    while [ "$count" -lt "$max_wait" ]; do
        if /program/bin/psql nddb -c "\q" > /dev/null 2>&1; then
            log_message "INFO: PostgreSQL 启动成功"
            return 0
        fi
        sleep 1
        count=$((count + 1))

        # 每3秒打印一次状态
        if [ $((count % 3)) -eq 0 ]; then
            log_message "INFO: 等待数据库就绪...(${count}s/${max_wait}s)"
        fi
    done

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
    log_message "WARNING: 准备执行 pg_resetxlog,先确保数据库已停止..."
    stop_postgres "$db_path" 30
    if [ $? -ne 0 ]; then
        log_message "ERROR: 停止数据库失败,无法执行 pg_resetxlog"
        return 1
    fi

    # 检测可用的重置工具(PG10前为pg_resetxlog)
    local reset_cmd=""
    if [ -x /program/bin/pg_resetxlog ]; then
        reset_cmd="/program/bin/pg_resetxlog"
    else
        log_message "ERROR: 未找到 pg_resetxlog 工具"
        return 1
    fi

    # 以数据目录属主(postgres)身份执行重置,-f 表示强制执行
    log_message "WARNING: 执行 $reset_cmd 重置WAL(可能导致数据不一致)..."
    su - postgres -c "$reset_cmd -f -D $db_path" >> "$CLEAN_RUNTIME_LOG" 2>&1
    if [ $? -ne 0 ]; then
        log_message "ERROR: pg_resetxlog 执行失败"
        return 1
    fi
    log_message "INFO: pg_resetxlog 执行完成"

    # 重置后重新启动数据库
    log_message "INFO: pg_resetxlog 后重新启动数据库..."
    su - postgres -c "/program/bin/pg_ctl -D $db_path start" 2>/dev/null
    if [ $? -ne 0 ]; then
        log_message "ERROR: pg_resetxlog 后启动数据库失败"
        return 1
    fi

    # 再次等待就绪
    if wait_postgres_ready 600; then
        log_message "INFO: pg_resetxlog 后数据库启动成功"
        return 0
    fi

    log_message "ERROR: pg_resetxlog 后数据库仍无法就绪"
    return 1
}

# 描述: 启动PostgreSQL数据库(启动失败或超时后使用 pg_resetxlog 兜底)
# 参数: $1 - 数据库路径, $2 - 最大等待时间(秒,默认600)
# 返回: 0-成功, 1-失败
start_postgres()
{
    local db_path=${1:-$BACKUP_DATABASE}
    local max_wait=${2:-600}  # 默认10分钟

    log_message "INFO: 启动 PostgreSQL (路径: $db_path)..."

    # 检查数据库目录是否存在
    if [ ! -d "$db_path" ]; then
        log_message "ERROR: 数据库目录不存在: $db_path"
        return 1
    fi

    # 若数据库已在运行,直接返回成功(避免重复启动导致pg_ctl报错)
    if su - postgres -c "/program/bin/pg_ctl -D $db_path status" > /dev/null 2>&1; then
        log_message "INFO: PostgreSQL 已在运行,无需启动"
        return 0
    fi

    # 启动数据库
    su - postgres -c "/program/bin/pg_ctl -D $db_path start" 2>/dev/null
    if [ $? -ne 0 ]; then
        # 启动失败(如WAL/pg_control损坏),尝试用 pg_resetxlog 兜底
        log_message "WARNING: PostgreSQL 启动失败,尝试使用 pg_resetxlog 修复..."
        reset_wal_and_restart "$db_path"
        return $?
    fi

    # 等待数据库就绪
    if wait_postgres_ready "$max_wait"; then
        return 0
    fi

    # 超时未就绪,使用 pg_resetxlog 兜底后再尝试
    log_message "WARNING: PostgreSQL 启动超时(${max_wait}s),尝试使用 pg_resetxlog 修复..."
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
        log_message "ERROR: 启动数据库失败"
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
        log_message "WARNING: 无法查询数据库连接数,跳过连接占用检查"
        return 0
    fi

    if [ "$conn_count" -gt 0 ]; then
        log_message "WARNING: 检测到 $conn_count 个连接占用数据库 $db_name,尝试终止..."
        su - postgres -c "PGUSER=postgres PGPASSWORD=123456 /program/bin/psql -d postgres -c \"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='$db_name' AND pid <> pg_backend_pid();\"" > /dev/null 2>&1
        sleep 2

        conn_count=$(su - postgres -c "PGUSER=postgres PGPASSWORD=123456 /program/bin/psql -t -A -d postgres -c \"SELECT count(*) FROM pg_stat_activity WHERE datname='$db_name' AND pid <> pg_backend_pid();\"" 2>/dev/null | tr -d '[:space:]')
        if [ -n "$conn_count" ] && [ "$conn_count" -gt 0 ]; then
            log_message "ERROR: 仍有 $conn_count 个连接占用数据库 $db_name"
            return 1
        fi
    fi

    log_message "INFO: 数据库 $db_name 无其他连接占用"
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
    log_message "开始备份数据库..."
    log_message "=========================================="

    # 备份前必须停止数据库,确保复制到静止状态的数据库目录(运行中复制会导致备份损坏)
    stop_postgres "$RUNTIME_DATABASE" 30
    if [ $? -ne 0 ]; then
        log_message "ERROR: 停止数据库失败,无法执行备份"
        return 1
    fi

    # 统计清理前空间
    local before_size
    before_size=$(du -sm "$RUNTIME_DATABASE" 2>/dev/null | awk '{print $1}')
    log_message "清理前数据库大小: ${before_size}MB"

    # 生成带日期的备份文件名
    local backup_date
    backup_date=$(date '+%Y%m%d_%H%M%S')
    local db_backup_file="database_bk_${backup_date}.tar.gz"
    local cfg_backup_file="cfg_bk_${backup_date}.tar.gz"

    # 删除旧备份
    log_message "INFO: 删除旧备份..."
    rm -rf "$BACKUP_DATABASE"
    rm -f "$BACKUP_ROOT_DIR"/database_bk_*.tar.gz
    rm -f "$BACKUP_ROOT_DIR"/cfg_bk_*.tar.gz

    # 备份数据库
    log_message "INFO: 备份数据库到 $BACKUP_ROOT_DIR/$db_backup_file..."
    cd "$BACKUP_ROOT_DIR" || {
        log_message "ERROR: 无法进入备份目录"
        return 1
    }

    # 相对打包(包内为数据库内容,便于解压还原)
    tar -zcvf "$db_backup_file" -C "$RUNTIME_DATABASE" . 2>/dev/null
    if [ $? -ne 0 ]; then
        log_message "ERROR: 数据库备份失败"
        return 1
    fi

    # 备份配置
    log_message "INFO: 备份配置到 $BACKUP_ROOT_DIR/$cfg_backup_file..."
    tar -zcvf "$cfg_backup_file" /var/runtime/cfg 2>/dev/null

    # 复制生成工作副本(直接复制原始目录)
    log_message "INFO: 复制生成工作副本 $BACKUP_DATABASE..."
    su - postgres -c "chmod 700 $RUNTIME_DATABASE"
    rm -rf "$BACKUP_DATABASE"
    cp -a "$RUNTIME_DATABASE" "$BACKUP_DATABASE"
    if [ $? -ne 0 ]; then
        log_message "ERROR: 复制生成工作副本失败"
        return 1
    fi

    # 修正属主(tar以root解压,确保工作副本归属postgres)
	log_message "INFO: 复制生成工作副本成功"
    chown -R postgres:postgres "$BACKUP_DATABASE"
    su - postgres -c "chmod 700 $BACKUP_DATABASE"

    # 验证备份
    if [ ! -d "$BACKUP_DATABASE" ]; then
        log_message "ERROR: 备份验证失败"
        return 1
    fi

    local backup_size
    backup_size=$(du -sm "$BACKUP_DATABASE" 2>/dev/null | awk '{print $1}')
    log_message "备份完成, 备份大小: ${backup_size}MB"

    # 打印备份目录信息
    log_message "=========================================="
    log_message "备份目录信息:"
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
    log_message "恢复数据库..."
    log_message "=========================================="

    # 确保postgres已停止
    stop_postgres "$BACKUP_DATABASE" 30

    # 将原数据库重命名为临时备份(而非直接删除),复制失败时可回滚,避免原库丢失
    local old_database="${RUNTIME_DATABASE}_old"
    rm -rf "$old_database"
    if [ -d "$RUNTIME_DATABASE" ]; then
        log_message "INFO: 暂存原数据库到 $old_database..."
        mv "$RUNTIME_DATABASE" "$old_database"
        if [ $? -ne 0 ]; then
            log_message "ERROR: 暂存原数据库失败"
            return 1
        fi
    fi

    # 复制备份数据库
    log_message "INFO: 复制数据库到 $RUNTIME_DATABASE..."
    su - postgres -c "cp -rf $BACKUP_DATABASE $RUNTIME_DATABASE"

    if [ $? -ne 0 ]; then
        log_message "ERROR: 数据库恢复失败,回滚原数据库..."
        rm -rf "$RUNTIME_DATABASE"
        mv "$old_database" "$RUNTIME_DATABASE"
        return 1
    fi

    su - postgres -c "chmod 700 $RUNTIME_DATABASE"

    # 验证恢复
    if [ ! -d "$RUNTIME_DATABASE" ]; then
        log_message "ERROR: 数据库恢复验证失败,回滚原数据库..."
        rm -rf "$RUNTIME_DATABASE"
        mv "$old_database" "$RUNTIME_DATABASE"
        return 1
    fi

    # 恢复成功,删除临时备份
    log_message "INFO: 数据库恢复成功,清理临时备份..."
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
    log_message "第一阶段清理: VACUUM FULL + db_clean.sh..."
    log_message "=========================================="

    # 确保数据库已启动(若已运行则跳过,避免重复启动失败)
    ensure_postgres_running "$BACKUP_DATABASE"
    if [ $? -ne 0 ]; then
        log_message "ERROR: 启动数据库失败"
        return 1
    fi

    # 执行 VACUUM FULL
    log_message "INFO: 执行 VACUUM FULL..."
    /program/bin/psql nddb << EOF
\connect nddb;
SET client_encoding to 'UTF8';
VACUUM FULL;
EOF

    if [ $? -ne 0 ]; then
        log_message "WARNING: VACUUM FULL 执行失败"
    else
        log_message "INFO: VACUUM FULL 执行成功"
    fi

    sleep 3

    # 执行db_clean.sh
    log_message "INFO: 执行 db_clean.sh..."
    db_clean.sh
    if [ $? -ne 0 ]; then
        log_message "WARNING: db_clean.sh 执行失败"
    else
        log_message "INFO: db_clean.sh 执行成功"
    fi

    # 统计清理后空间
    local after_size
    after_size=$(du -sm "$BACKUP_DATABASE" 2>/dev/null | awk '{print $1}')
    local before_size
    before_size=$(du -sm "$RUNTIME_DATABASE" 2>/dev/null | awk '{print $1}')

    log_message "第一阶段清理完成"
    log_message "  - 清理前: ${before_size}MB"
    log_message "  - 清理后: ${after_size}MB"
    log_message "  - 阈值: ${DB_THRESHOLD}MB"

    # 判断是否需要第二阶段清理
    if [ "$after_size" -gt "$DB_THRESHOLD" ]; then
        log_message "INFO: 第一阶段清理后空间仍超过阈值,需要第二阶段清理"
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
    log_message "第二阶段清理: 数据库导出导入..."
    log_message "=========================================="

    # 检查db_inout_new.sh是否存在
    if [ ! -f "$BACKUP_ROOT_DIR/db_inout_new.sh" ]; then
        log_message "ERROR: $BACKUP_ROOT_DIR/db_inout_new.sh 不存在"
        return 1
    fi

    # 导出前确保 PostgreSQL 正在运行
    ensure_postgres_running "$BACKUP_DATABASE"
    if [ $? -ne 0 ]; then
        log_message "ERROR: 启动数据库失败,无法执行导出"
        return 1
    fi

    # 检查空间
    local free_space
    free_space=$(df -m | grep "$BACKUP_ROOT_DIR$" | awk '{print $4}' | head -1)
    local db_size
    db_size=$(du -sm "$BACKUP_DATABASE" 2>/dev/null | awk '{print $1}')

    log_message "可用空间: ${free_space}MB, 数据库大小: ${db_size}MB"

    if [ "$free_space" -lt "$db_size" ]; then
        log_message "ERROR: 空间不足,无法执行导出"
        return 1
    fi

    # 导出数据库
    log_message "INFO: 导出数据库..."
    local export_file="vms_db_export"
    rm -rf "$BACKUP_ROOT_DIR/$export_file"

    cd "$BACKUP_ROOT_DIR" || return 1
    sh "$BACKUP_ROOT_DIR/db_inout_new.sh" export "$BACKUP_ROOT_DIR" "$export_file"
    if [ $? -ne 0 ]; then
        log_message "ERROR: 数据库导出失败"
        return 1
    fi

    # 检查导出文件
    log_message "INFO: 检查导出文件是否存在..."
    if [ ! -f "$BACKUP_ROOT_DIR/$export_file.tar.gz" ]; then
        log_message "ERROR: 导出文件不存在"
        return 1
    fi
    log_message "INFO: 导出文件检查通过"

    # 导入同样依赖数据库连接(DROP/CREATE DATABASE、pg_restore),确保postgres正在运行
    # 注意: 此处绝对不能停止数据库,否则导入必然失败
    log_message "INFO: 确保数据库正在运行..."
    ensure_postgres_running "$BACKUP_DATABASE"
    if [ $? -ne 0 ]; then
        log_message "ERROR: 启动数据库失败,无法执行导入"
        return 1
    fi
    log_message "INFO: 数据库已在运行"

    # 确认数据库无其他连接占用(DROP DATABASE要求无活动连接,否则导入会失败)
    log_message "INFO: 检查数据库连接占用..."
    ensure_no_db_connections "nddb"
    if [ $? -ne 0 ]; then
        log_message "ERROR: 数据库仍被占用,中止导入(备份数据已保留)"
        return 2
    fi

    # 导入数据库
    log_message "INFO: 导入数据库..."
    sh "$BACKUP_ROOT_DIR/db_inout_new.sh" import "$BACKUP_ROOT_DIR/$export_file.tar.gz"
    if [ $? -ne 0 ]; then
        log_message "ERROR: 数据库导入失败"
        return 2  # 返回2表示导入失败但保留备份
    fi

    # 统计清理后空间
    local after_size
    after_size=$(du -sm "$BACKUP_DATABASE" 2>/dev/null | awk '{print $1}')

    log_message "第二阶段清理完成"
    log_message "  - 清理后: ${after_size}MB"
    log_message "  - 阈值: ${DB_THRESHOLD}MB"

    if [ "$after_size" -gt "$DB_THRESHOLD" ]; then
        log_message "WARNING: 第二阶段清理后空间仍超过阈值"
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
    log_message "#           运行时数据库手动清理脚本 V2.4                      #"
    log_message "################################################################"
    log_message ""

    # 检查锁文件
    create_lock_file
    if [ $? -ne 0 ]; then
        log_message "ERROR: 无法获取锁文件,脚本可能已在运行"
        exit 1
    fi

    # 注册退出清理函数
    trap 'cleanup_lock_file' EXIT

    # 初始化环境
    init_environment
    if [ $? -ne 0 ]; then
        script_error_exit "环境初始化失败"
    fi

    # 检查前置条件
    check_prerequisites
    if [ $? -ne 0 ]; then
        script_error_exit "前置条件检查失败"
    fi

    # 检查运行时空间
    check_runtime_space
    if [ $? -ne 0 ]; then
        log_message "INFO: 无需清理,退出脚本"
        cleanup_lock_file
        exit 0
    fi

    # 停止服务
    stop_services
    if [ $? -ne 0 ]; then
        script_error_exit "停止业务服务失败"
    fi

    # 检查空间是否足够进行备份
    local mem_runtime_db
    mem_runtime_db=$(du -sm "$RUNTIME_DATABASE" 2>/dev/null | awk '{print $1}')
    local mem_backup
    mem_backup=$(df -m | grep "$BACKUP_ROOT_DIR$" | awk '{print $4}' | head -1)

    log_message "数据库大小: ${mem_runtime_db}MB, 备份空间: ${mem_backup}MB"

    if [ "$mem_runtime_db" -gt "$mem_backup" ]; then
        log_message "INFO: 备份空间不足,清理历史备份(保留脚本文件)..."
        rm -rf "$BACKUP_DATABASE"
        find "$BACKUP_ROOT_DIR" -maxdepth 1 -type f ! -name "*.sh" -exec rm -f {} \;

        mem_backup=$(df -m | grep "$BACKUP_ROOT_DIR$" | awk '{print $4}' | head -1)
        if [ "$mem_runtime_db" -gt "$mem_backup" ]; then
            script_error_exit "备份空间不足,无法执行清理"
        fi
    fi

    # 备份数据库
    backup_database
    if [ $? -ne 0 ]; then
        # 备份失败时数据库处于停止状态,需重新拉起原库,保证系统可用(原库数据未被修改)
        log_message "WARNING: 数据库备份失败,尝试重新拉起数据库以保证系统可用..."
        # 打印备份目录信息
        log_message "=========================================="
        log_message "备份目录信息(拉起数据库前):"
        ls -lh "$BACKUP_ROOT_DIR" 2>/dev/null | while read line; do log_message "$line"; done
        log_message "=========================================="
        start_postgres "$RUNTIME_DATABASE"
        script_error_exit "数据库备份失败"
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
                log_message "WARNING: 导入失败,备份数据已保留"
            fi
            log_message "=========================================="
            script_error_exit "ERROR: 第二阶段清理失败，请保持现状，千万不要重启，并联系开发处理", 1
        fi
    elif [ "$first_stage_result" -ne 0 ]; then
        script_error_exit "第一阶段清理失败"
    fi

    # 停止数据库
    stop_postgres "$BACKUP_DATABASE" 30

    # 恢复数据库
    restore_database
    if [ $? -ne 0 ]; then
        # 恢复失败时已回滚原库,重新拉起数据库保证系统可用
        log_message "WARNING: 数据库恢复失败,尝试重新拉起数据库..."
        # 打印备份目录信息
        log_message "=========================================="
        log_message "备份目录信息(拉起数据库前):"
        ls -lh "$BACKUP_ROOT_DIR" 2>/dev/null | while read line; do log_message "$line"; done
        log_message "=========================================="
        start_postgres "$RUNTIME_DATABASE"
        script_error_exit "数据库恢复失败"
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
        log_message "#         清理成功完成!                           #"
        log_message "##################################################"
        log_message "最终数据库大小: ${final_size}MB"
        log_message "备份位置: $BACKUP_DATABASE"

        # 清理备份数据库(清理成功且已恢复到/var/runtime,备份可删除)
        log_message "INFO: 清理备份数据库..."
        rm -rf "$BACKUP_DATABASE"
        log_message "INFO: 备份数据库已清理"

        log_message "即将重启系统..."
        log_message ""

        cleanup_lock_file
        sync
        reboot
    else
        script_error_exit "数据库恢复验证失败"
    fi
}

# 执行主流程
main
