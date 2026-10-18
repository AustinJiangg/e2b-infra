#!/bin/bash
# 新 920B（RPM 部署、orchestrator 由 nomad 拉起）上把 5008 端口的 orchestrator 切到 gsd（进程级 CRIU 快照）这套。
# 行为的逐步说明见手册 23 章"切到 gsd 栈"。
#
#   switch-gsd-new920b.sh gsd      切到 gsd 版 orchestrator（KASandbox_0904 deltabox-gsd）
#   switch-gsd-new920b.sh status   只看现状，不改任何东西
#
# 切回另一套栈（整机级回滚）用那一套自己的切栈脚本，不在本目录；它同样每次都从同一份基线 job
# 重新生成，gsd 加的 env 不会残留。
#
# 这个脚本对基线 job 做的改动：
#   - 二进制换成 /fc-gsd/orchestrator
#   - 注入 guest 的 gsd 在 /fc-gsd/gsd，正是 HOST_GSD_PATH 的默认值，不用设 env
#   - task env 追加 HOST_KERNELS_DIR=/fc-gsd/kernels（gsd 补丁内核的根目录）。这台机器
#     两套栈共用，/fc-kernels/vmlinux-6.1.158 是整机级那套的，所以补丁内核另放；
#     只跑 gsd 的机器把补丁内核直接放 /fc-kernels 即可，这个 env 也不用设
# 两点保护与后果：
#   - 还有沙箱在跑就拒绝切换（共用机器上那可能是其他使用者的），不提供强制选项
#   - gsd 栈运行期间建的**所有**模板都会注入 gsd、init 换成 overlay-init，其他使用者这期间不要建模板
#
# 环境变量（都有默认值）：
#   GSD_STACK_DIR   放基线 job 与生成的 job 定义的目录，默认 $HOME/gsd-stack/stack（生成的文件含密钥，600 权限）
#   GSD_JOB_BASE    基线 job 定义，默认 $GSD_STACK_DIR/job-tm-new920b.base.json。事先在**非 gsd 栈**运行时存一份：
#                     nomad job inspect template-manager-system > $GSD_JOB_BASE && chmod 600 $GSD_JOB_BASE
#   E2B_INFRA_ENV   含 NOMAD_ACL_TOKEN 的环境文件，默认 /opt/e2b-infra/.env
#   GSD_ORCH_BIN / GSD_BIN / GSD_KDIR   调试时临时换一套 orchestrator / gsd / 内核目录，见下
set -uo pipefail

S_DIR=${GSD_STACK_DIR:-$HOME/gsd-stack/stack}       # 只放基线 job 与生成的 job 定义
F=/fc-gsd                                           # gsd 栈运行时用的全部文件
GSD_DEFAULT=$F/gsd                                  # = orchestrator 里 HOST_GSD_PATH 的默认值
# 调试时可用环境变量临时换一套（例如带 KASAN 的内核）：
#   GSD_ORCH_BIN=... GSD_BIN=... GSD_KDIR=... switch-gsd-new920b.sh gsd
BIN_GSD=${GSD_ORCH_BIN:-$F/orchestrator}
GSD=${GSD_BIN:-$GSD_DEFAULT}
KDIR=${GSD_KDIR:-$F/kernels}                        # 其下 vmlinux-6.1.158/vmlinux.bin 是补丁内核
KERNEL=$KDIR/vmlinux-6.1.158/vmlinux.bin
JOB=template-manager-system
JOB_BASE=${GSD_JOB_BASE:-$S_DIR/job-tm-new920b.base.json}   # 基线（nomad job inspect 存下的）
OUT=$S_DIR/job-tm-new920b-gsd.json                  # 含密钥，600 权限
PORT=5008
WAIT_MAX=360

sha8() { [ -f "$1" ] && sha256sum "$1" | cut -c1-16 || echo "(缺失)"; }

# 不用 pgrep -f，避免自匹配
cur_pid() {
  local p exe
  for p in /proc/[0-9]*; do
    [ -r "$p/cmdline" ] || continue
    tr '\0' ' ' < "$p/cmdline" 2>/dev/null | grep -q -- "--port $PORT" || continue
    exe=$(readlink "$p/exe" 2>/dev/null) || continue
    case "$exe" in *orchestrator*|*template-manager*) basename "$p"; return 0 ;; esac
  done
  return 1
}
cur_exe() { local p; p=$(cur_pid) || return 1; readlink "/proc/$p/exe" 2>/dev/null; }
penv() { tr '\0' '\n' < "/proc/$1/environ" | sed -n "s/^$2=//p"; }
# 进程实际生效的 gsd 路径：没设 HOST_GSD_PATH 就是默认值
pgsd() { local v; v=$(penv "$1" HOST_GSD_PATH); echo "${v:-$GSD_DEFAULT}"; }
nomad_env() { set -a; . "${E2B_INFRA_ENV:-/opt/e2b-infra/.env}"; set +a; export NOMAD_TOKEN="$NOMAD_ACL_TOKEN"; }

status() {
  local p exe
  p=$(cur_pid) || p=""
  exe=$([ -n "$p" ] && readlink "/proc/$p/exe" || echo "(没有进程占 $PORT)")
  echo "当前二进制 : $exe  ($(sha8 "$exe"))"
  if [ -n "$p" ]; then
    echo "  HOST_GSD_PATH    = $(penv $p HOST_GSD_PATH)（空 = 默认 $GSD_DEFAULT）"
    echo "  HOST_KERNELS_DIR = $(penv $p HOST_KERNELS_DIR)"
  fi
  case "$exe" in "$F"/*|*orchestrator-gsd*) echo "  => gsd 栈" ;; *) echo "  => 不是 gsd 栈" ;; esac
  local g=$GSD k=$KERNEL
  if [ -n "$p" ]; then     # 显示运行中进程实际用的，而不是脚本默认值
    g=$(pgsd $p)
    k=$(penv $p HOST_KERNELS_DIR); k=${k:+$k/vmlinux-6.1.158/vmlinux.bin}; [ -n "$k" ] || k=$KERNEL
  fi
  echo "gsd        : $g  $(sha8 "$g")  commit=$("$g" -commit 2>/dev/null)"
  echo "补丁内核   : $k  $(sha8 "$k")"
  echo "health     : $(curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1:$PORT/health)"
  echo "活 FC 数   : $(pgrep -c -x firecracker 2>/dev/null; true)"
}

S=${1:-}
case "$S" in
  status) status; exit 0 ;;
  gsd) ;;
  *) echo "用法: $(basename "$0") <gsd|status>" >&2; exit 1 ;;
esac

for f in "$BIN_GSD" "$GSD"; do [ -x "$f" ] || { echo "不存在或不可执行: $f" >&2; exit 1; }; done
[ -f "$KERNEL" ] || { echo "缺补丁内核: $KERNEL" >&2; exit 1; }
[ -f "$JOB_BASE" ] || { echo "缺基线 job: $JOB_BASE" >&2; exit 1; }
n=$(pgrep -c -x firecracker 2>/dev/null; true)
[ "$n" = 0 ] || { echo "还有 $n 个 firecracker 在跑（可能是其他使用者的），拒绝切换" >&2; exit 1; }

now=$(cur_exe 2>/dev/null || true)
p=$(cur_pid 2>/dev/null || true)
if [ -n "$now" ] && [ "$(readlink -f "$now")" = "$(readlink -f "$BIN_GSD")" ] \
   && [ "$(pgsd "$p")" = "$GSD" ] && [ "$(penv "$p" HOST_KERNELS_DIR)" = "$KDIR" ] \
   && [ "$(penv "$p" GSD_ORCH_SHA256)" = "$(sha256sum "$BIN_GSD" | cut -d' ' -f1)" ] \
   && [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1:$PORT/health)" = 200 ]; then
  echo "$(date +%T) 已经是 gsd 栈，无需切换"; status; exit 0
fi

echo "=== 切换前 ==="; status; echo

mkdir -p "$(dirname "$OUT")"
( umask 077; python3 - "$JOB_BASE" "$OUT" "$BIN_GSD" "$PORT" "$GSD" "$KDIR" "$GSD_DEFAULT" <<'PY'
import hashlib, json, sys
src, dst, binp, port, gsd, kdir, gsd_default = sys.argv[1:8]
j = json.load(open(src))
t = j["Job"]["TaskGroups"][0]["Tasks"][0]
t["Config"]["args"][1] = "chmod +x %s && %s --port %s" % (binp, binp, port)
t["Env"].pop("HOST_GSD_PATH", None)
if gsd != gsd_default:
    t["Env"]["HOST_GSD_PATH"] = gsd
t["Env"]["HOST_KERNELS_DIR"] = kdir
# 文件名不带版本号，原地换二进制时 job 定义不变、nomad 不会重启进程；把 sha 放进 env 让它变
t["Env"]["GSD_ORCH_SHA256"] = hashlib.sha256(open(binp, "rb").read()).hexdigest()
for k in ("Version", "SubmitTime", "CreateIndex", "ModifyIndex", "JobModifyIndex", "Status", "StatusDescription"):
    j["Job"].pop(k, None)
json.dump(j, open(dst, "w"), indent=2)
print("job 定义已生成: %s" % dst)
PY
) || exit 1

nomad_env
ok=0
for attempt in 1 2 3 4 5 6; do
  if nomad job run -detach -json "$OUT" >"$S_DIR/.run.$$" 2>&1; then ok=1; break; fi
  grep -qi "port collision\|exhausted" "$S_DIR/.run.$$" \
    && { echo "$(date +%T) 5008 还被旧 alloc 占着，等 15s 重试（第 $attempt 次）"; sleep 15; continue; }
  echo "nomad job run 失败：" >&2; cat "$S_DIR/.run.$$" >&2; rm -f "$S_DIR/.run.$$"; exit 1
done
rm -f "$S_DIR/.run.$$"
[ "$ok" = 1 ] || { echo "nomad job run 重试 6 次仍失败" >&2; exit 1; }
echo "$(date +%T) 已提交 job：$(basename "$BIN_GSD")（$(sha8 "$BIN_GSD")）"

for i in $(seq 1 $((WAIT_MAX/5))); do
  e=$(cur_exe 2>/dev/null || true)
  h=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1:$PORT/health)
  p=$(cur_pid 2>/dev/null || true)
  # 只换内核/gsd 时二进制不变，旧进程也会 200：必须等新进程的 env 生效才算切完
  if [ -n "$e" ] && [ "$(readlink -f "$e")" = "$(readlink -f "$BIN_GSD")" ] && [ "$h" = 200 ] \
     && [ "$(pgsd "$p")" = "$GSD" ] && [ "$(penv "$p" HOST_KERNELS_DIR)" = "$KDIR" ] \
     && [ "$(penv "$p" GSD_ORCH_SHA256)" = "$(sha256sum "$BIN_GSD" | cut -d' ' -f1)" ]; then
    sleep 20      # 让 api 侧重新连上 orchestrator
    echo "$(date +%T) 切换完成"; echo "=== 切换后 ==="; status; exit 0
  fi
  sleep 5
done
echo "$(date +%T) 超时 ${WAIT_MAX}s，未确认切换成功" >&2
status
exit 1
