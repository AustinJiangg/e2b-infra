#!/bin/bash
# Harbor 自愈看门狗：由 harbor-watchdog.timer 每分钟拉起一次，docker 每次启动后也会立刻跑一次。
#
# 为什么不是 compose 里的 restart: always：Harbor 生成的 docker-compose.yml 本来就带它，
# 但 /etc/docker/daemon.json 开着 live-restore 时，dockerd 重启期间容器进程不退出，
# restart 策略根本不会触发——容器一直显示 "Up"，2900 的端口转发却没恢复，
# nomad 拉 api / client-proxy 镜像全部 connection refused（2026-10-31 的事故）。
# 所以这里按“2900 能不能访问到”判断，不看容器状态。
#
# 处置分两级：先 compose up -d（补起退出的容器），还不通再 down + up -d 整组重建
# （不带 -v，镜像仓库数据在 harbor.yml 的 data_volume 里，不受影响）。
set -uo pipefail

HARBOR_DIR="${HARBOR_DIR:-/opt/e2b-infra/harbor}"
HARBOR_PORT="${HARBOR_PORT:-2900}"
# 每次判定前最多等这么久：Harbor 刚起时 core 要几十秒才回 Pong，太快动手会来回重建
WAIT_SECONDS="${WAIT_SECONDS:-90}"
LOCK_FILE="/run/lock/e2b-harbor.lock"
PING_URL="http://127.0.0.1:${HARBOR_PORT}/api/v2.0/ping"

log() { echo "[harbor-watchdog] $*"; }

ping_ok() {
    [[ "$(curl -s --noproxy '*' --connect-timeout 2 --max-time 5 "$PING_URL" 2>/dev/null)" == "Pong" ]]
}

wait_healthy() {
    local deadline=$(( $(date +%s) + $1 ))
    while (( $(date +%s) < deadline )); do
        ping_ok && return 0
        sleep 5
    done
    ping_ok
}

compose() {
    if docker compose version >/dev/null 2>&1; then
        docker compose -f "$HARBOR_DIR/docker-compose.yml" "$@"
    else
        docker-compose -f "$HARBOR_DIR/docker-compose.yml" "$@"
    fi
}

# 还没执行过 harbor/install.sh（没有 compose 文件）就不归看门狗管
[[ -f "$HARBOR_DIR/docker-compose.yml" ]] || { log "$HARBOR_DIR/docker-compose.yml 不存在，跳过"; exit 0; }
systemctl is-active --quiet docker || { log "docker 未运行，跳过"; exit 0; }

# build.sh -s 跑 harbor/install.sh 时持有同一把锁，避免两边同时 down/up
exec 9>"$LOCK_FILE"
flock -n 9 || { log "Harbor 正在被其它流程操作（$LOCK_FILE 被占用），本轮跳过"; exit 0; }

ping_ok && exit 0

if wait_healthy "$WAIT_SECONDS"; then
    log "Harbor 在等待期内恢复，无需处置"
    exit 0
fi

log "$PING_URL 持续 ${WAIT_SECONDS}s 不可用，执行 compose up -d"
compose up -d
if wait_healthy "$WAIT_SECONDS"; then
    log "compose up -d 后已恢复"
    exit 0
fi

log "仍不可用（容器可能显示 Up 但端口已丢），执行 down + up -d 整组重建"
compose down
compose up -d
if wait_healthy "$WAIT_SECONDS"; then
    log "整组重建后已恢复"
    exit 0
fi

log "整组重建后 Harbor 仍不可用，需人工排查：cd $HARBOR_DIR && docker compose ps / logs"
exit 1
