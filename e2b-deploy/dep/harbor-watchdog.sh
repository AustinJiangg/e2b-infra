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
#
# Harbor 恢复后再对 RESCHEDULE_JOBS 做一次 nomad job eval -force-reschedule：
# client-proxy 的 restart 是 attempts=2/10m、mode="fail"，Harbor 宕机期间两次拉镜像失败就
# 判 alloc failed，之后要等 nomad 的退避（越拖越长）才重调度，Harbor 好了它也不会马上回来。
# api 的 restart 会一直重试，不需要。
# “Harbor 宕过”记在 /run 下的标记文件里，而不是只看本轮：这样即使 Harbor 是人工修好的、
# 或本轮重建失败下一轮才恢复，重调度也不会漏；nomad 暂时连不上就保留标记，下一轮再试。
set -uo pipefail

HARBOR_DIR="${HARBOR_DIR:-/opt/e2b-infra/harbor}"
HARBOR_PORT="${HARBOR_PORT:-2900}"
# 每次判定前最多等这么久：Harbor 刚起时 core 要几十秒才回 Pong，太快动手会来回重建
WAIT_SECONDS="${WAIT_SECONDS:-90}"
LOCK_FILE="/run/lock/e2b-harbor.lock"
PING_URL="http://127.0.0.1:${HARBOR_PORT}/api/v2.0/ping"
DOWN_MARKER="/run/e2b-harbor-was-down"
# 空格分隔的 nomad job ID（不是 hcl 文件名：client-proxy 的 hcl 是 nomad/edge.hcl）
RESCHEDULE_JOBS="${RESCHEDULE_JOBS:-client-proxy}"
NOMAD_ADDR="${NOMAD_ADDR:-http://127.0.0.1:4646}"
NOMAD_ACL_TOKEN_FILE="${NOMAD_ACL_TOKEN_FILE:-/data/nomad/acl.token}"
export NOMAD_ADDR

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

reschedule_jobs() {
    [[ -f "$DOWN_MARKER" ]] || return 0
    [[ -n "$RESCHEDULE_JOBS" ]] || { rm -f "$DOWN_MARKER"; return 0; }
    command -v nomad >/dev/null 2>&1 || { log "找不到 nomad 命令，跳过重调度"; rm -f "$DOWN_MARKER"; return 0; }
    if [[ -z "${NOMAD_TOKEN:-}" && -r "$NOMAD_ACL_TOKEN_FILE" ]]; then
        NOMAD_TOKEN="$(tr -d '[:space:]' < "$NOMAD_ACL_TOKEN_FILE")"
        export NOMAD_TOKEN
    fi

    local job failed=0
    for job in $RESCHEDULE_JOBS; do
        if ! nomad job inspect "$job" >/dev/null 2>&1; then
            # 区分“job 不存在”和“nomad 连不上 / token 不对”：后者要留着标记下一轮重试
            if nomad status >/dev/null 2>&1; then
                log "nomad 里没有 job $job，跳过"
            else
                log "nomad 暂时不可用（$NOMAD_ADDR），下一轮再重调度 $job"
                failed=1
            fi
            continue
        fi
        if nomad job eval -force-reschedule -detach "$job" >/dev/null; then
            log "Harbor 已恢复，已对 $job 执行 force-reschedule（跳过 nomad 退避，立即重新拉镜像）"
        else
            log "对 $job 执行 force-reschedule 失败，下一轮再试"
            failed=1
        fi
    done
    (( failed )) || rm -f "$DOWN_MARKER"
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

if ping_ok; then
    reschedule_jobs
    exit 0
fi

touch "$DOWN_MARKER"

if wait_healthy "$WAIT_SECONDS"; then
    log "Harbor 在等待期内恢复，无需处置"
    reschedule_jobs
    exit 0
fi

log "$PING_URL 持续 ${WAIT_SECONDS}s 不可用，执行 compose up -d"
compose up -d
if wait_healthy "$WAIT_SECONDS"; then
    log "compose up -d 后已恢复"
    reschedule_jobs
    exit 0
fi

log "仍不可用（容器可能显示 Up 但端口已丢），执行 down + up -d 整组重建"
compose down
compose up -d
if wait_healthy "$WAIT_SECONDS"; then
    log "整组重建后已恢复"
    reschedule_jobs
    exit 0
fi

log "整组重建后 Harbor 仍不可用，需人工排查：cd $HARBOR_DIR && docker compose ps / logs"
exit 1
