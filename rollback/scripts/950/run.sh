#!/usr/bin/env bash
# rollback/scripts/950/run.sh —— 950（鲲鹏 + HDBSS，RPM 部署）上的一站式测试入口。
#
#   bash run.sh smoke     部署完先跑这个，≤ 3 分钟，回答「这台机器的 checkpoint 能用吗」
#   bash run.sh func      功能与健壮性全量，≤ 40 分钟
#   bash run.sh perf      性能分档 + 长尾 + 并发，≤ 40 分钟
#   bash run.sh hdbss     HDBSS 专项：证据 + 写密集基准（920B kvm-wp 上流程实测约 4 分钟，基准
#                         自带 14 分钟时限）；加 --orders 扫 buffer 大小，每个 order 多一次约 1 分钟的重启
#   bash run.sh long      只打印长测清单与命令，不自动跑
#
# 每次跑出一个 results/<YYYYmmdd-HHMMSS>-<档>/，里面一份 SUMMARY.md、每项一份
# .log、脚本自己的 .json 产物。任一项 FAIL 则退出码非 0（= FAIL 项数）。
#
# 常用可选参数（都有能直接用的默认值，不需要占位符）：
#   --env-file F      客户端凭据的 dotenv；默认 $CRTEST_ENV_FILE，再默认仓库里的
#                     benchmark/.env（sync-env.sh 生成，带 E2B_API_KEY），最后才是 $DEPLOY/.env
#   --deploy-dir D    RPM 部署根，默认 /opt/e2b-infra
#   --python P        跑脚本的解释器，默认 $CRTEST_PY，再默认 python3。要用装了 SDK
#                     覆盖层的那一个（build.sh -i 装在哪就是哪），例如在那个环境里
#                     传 --python "$(command -v python3)"
#   --results-dir D   结果根目录，默认 <本目录>/results
#   --template T      模板 id，默认 base
#   --sdk-install P   SDK 覆盖层 install.py 的路径，默认 $DEPLOY/dep/e2b-sdk-checkpoint/install.py
#   --store S         checkpoint 产物目录（预检用；smoke 也在它的父目录找能力文件），
#                     默认从 orchestrator 环境自动取
#   --keep-going      有 FAIL 也把剩下的项跑完（默认也是跑完，这里保留给将来改默认）
#   --allow-restart   func 档把 T18 也跑了（会重启 orchestrator，全机沙箱都会没）
#                     必须同时给 --restart-cmd 或设 CRTEST_RESTART_CMD；
#                     hdbss 档的 --orders 也要它（每个 order 重启一次，不用 --restart-cmd）
#   --restart-cmd C   T18 用的重启命令
#   --fault-cases     func 档把 T21/T38 也跑了（要求服务端带注入 env / 带注入特性的 FC）
#   --quota-cases     func 档把 T37 也跑了（要求服务端带配额 env）
#   -n N              perf 档串行 restore 的次数，默认 100
#   --quick           perf 档只做「短试跑」：验证三个脚本跑得通，不是正式采数
#                     （2 个档各 n=5、串行长尾 n=10、并发只 A 段两个 N），≤ 10 分钟；
#                     hdbss 档同理（脏页量只 16 / 64 MB、每个组合 1 遍），920B 上实测约 20 秒
#   --orders L        hdbss 档扫 FC_HDBSS_ORDER，例如 --orders 1,2,4,6,9。每个 order 都要改
#                     nomad job 的 env 并重启 orchestrator（全机沙箱都会没），所以必须同时给
#                     --allow-restart；扫完（含中途失败、Ctrl-C）把 job 恢复成扫描前的定义。
#                     不给就只测当前 order，不重启
#   --kill-sandboxes-ok  hdbss 档扫描时，机器上还有活着的 firecracker（= 还有沙箱在跑，可能是
#                     别人的）默认拒绝扫描；确认这 N 个沙箱可以消失才加这个参数
#   --force-non-hdbss hdbss 档在脏页后端不是 hdbss 的机器上也照跑（仅流程验证，数字无意义）；
#                     默认整档判 SKIP
#   --iters N         hdbss 档每个组合跑几遍，默认 3（--quick 时 1）
#   --job J           hdbss 档扫描时改哪个 nomad job，默认取 orchestrator 进程环境里的
#                     NOMAD_JOB_ID，再默认 template-manager-system
#
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
SCRIPTS=$(cd "$HERE/.." && pwd)
CRTEST="$SCRIPTS/crtest"
ACCEPT="$SCRIPTS/acceptance"

TIER=""
DEPLOY="${E2B_DEPLOY_DIR:-/opt/e2b-infra}"
ENV_FILE="${CRTEST_ENV_FILE:-}"
PY="${CRTEST_PY:-python3}"
RESULTS_ROOT="$HERE/results"
TEMPLATE="base"
SDK_INSTALL=""
STORE=""
ALLOW_RESTART=0
RESTART_CMD="${CRTEST_RESTART_CMD:-}"
FAULT_CASES=0
QUOTA_CASES=0
SERIAL_N=100
QUICK=0
ORDERS=""
FORCE_NON_HDBSS=0
KILL_SANDBOXES_OK=0
HDBSS_ITERS=""
JOB=""
RUN_ARGS="$*"

die() { echo "错误：$*" >&2; exit 2; }

while [ $# -gt 0 ]; do
	case "$1" in
	smoke | func | perf | hdbss | long) TIER=$1; shift ;;
	--env-file) ENV_FILE=$2; shift 2 ;;
	--deploy-dir) DEPLOY=$2; shift 2 ;;
	--python) PY=$2; shift 2 ;;
	--results-dir) RESULTS_ROOT=$2; shift 2 ;;
	--template) TEMPLATE=$2; shift 2 ;;
	--sdk-install) SDK_INSTALL=$2; shift 2 ;;
	--store) STORE=$2; shift 2 ;;
	--keep-going) shift ;;
	--allow-restart) ALLOW_RESTART=1; shift ;;
	--restart-cmd) RESTART_CMD=$2; shift 2 ;;
	--fault-cases) FAULT_CASES=1; shift ;;
	--quota-cases) QUOTA_CASES=1; shift ;;
	-n) SERIAL_N=$2; shift 2 ;;
	--quick) QUICK=1; shift ;;
	--orders) ORDERS=$2; shift 2 ;;
	--force-non-hdbss) FORCE_NON_HDBSS=1; shift ;;
	--kill-sandboxes-ok) KILL_SANDBOXES_OK=1; shift ;;
	--iters) HDBSS_ITERS=$2; shift 2 ;;
	--job) JOB=$2; shift 2 ;;
	-h | --help) sed -n '2,48p' "$0"; exit 0 ;;
	*) die "未知参数 $1（-h 看用法）" ;;
	esac
done
[ -n "$TIER" ] || die "要给一个档：smoke / func / perf / hdbss / long"
if [ -n "$ORDERS" ]; then
	[ "$TIER" = hdbss ] || die "--orders 只用于 hdbss 档"
	echo "$ORDERS" | grep -qE '^[0-9]+(,[0-9]+)*$' || die "--orders 要逗号分隔的整数，例如 --orders 1,2,4,6,9"
	[ "$ALLOW_RESTART" = 1 ] ||
		die "--orders 每个 order 都要重启 orchestrator，这台机器上所有沙箱都会没；确认要做就加 --allow-restart"
	# 第二道闸：机器上还有沙箱在跑就不扫（--allow-restart 只表示「知道要重启」，不表示
	# 「知道现在有人在用」）。在建结果目录、碰 job 之前就判，用 pidof 数，不用 pgrep -f。
	N_LIVE_FC=$(pidof firecracker | wc -w)
	if [ "$N_LIVE_FC" -gt 0 ] && [ "$KILL_SANDBOXES_OK" != 1 ]; then
		ORCH_EXE="（没找到 orchestrator / template-manager 进程）"
		for p in /proc/[0-9]*; do
			exe=$(readlink "$p/exe" 2>/dev/null) || continue
			case ${exe##*/} in orchestrator* | template-manager*) ORCH_EXE=$exe ;; esac
		done
		die "机器上有 $N_LIVE_FC 个活着的 firecracker（沙箱），当前 orchestrator 是 $ORCH_EXE。扫描会让这 $N_LIVE_FC 个沙箱消失；先把它们关掉，或者确认可以消失后加 --kill-sandboxes-ok"
	fi
fi

# env 默认值：优先用仓库里的 benchmark/.env —— 它由 benchmark/sync-env.sh 生成，带
# E2B_API_KEY / E2B_ACCESS_TOKEN；而 $DEPLOY/.env 是服务端配置，通常不含客户端凭据。
REPO_ROOT=$(cd "$SCRIPTS/../.." && pwd)
if [ -z "$ENV_FILE" ]; then
	if [ -r "$REPO_ROOT/benchmark/.env" ]; then
		ENV_FILE="$REPO_ROOT/benchmark/.env"
	else
		ENV_FILE="$DEPLOY/.env"
	fi
fi
[ -n "$SDK_INSTALL" ] || SDK_INSTALL="$DEPLOY/dep/e2b-sdk-checkpoint/install.py"

# ---------------------------------------------------------------- long：只打清单
if [ "$TIER" = "long" ]; then
	sed -n '/^## 长测清单/,/^## 长测清单结束/p' "$HERE/README.md" |
		sed '1d;$d'
	echo ""
	echo "（long 档不自动跑：这些都是小时级、要单独授权的。命令照抄上面，"
	echo " 结果自己 tee 到 $RESULTS_ROOT 下的目录里。）"
	exit 0
fi

command -v "$PY" >/dev/null 2>&1 || [ -x "$PY" ] || die "找不到解释器 $PY（--python 指定）"

# ---------------------------------------------------------------- 凭据
# 两条都设：`.sh` 与 acceptance/ 下那几个 `load_dotenv()`（不带路径、按 CWD 找）
# 靠已 export 的环境变量，crtest 与 bench 靠 --env-file / CRTEST_ENV_FILE。
if [ -r "$ENV_FILE" ]; then
	set -a
	# shellcheck disable=SC1090
	. "$ENV_FILE"
	set +a
else
	echo "!! 读不到 $ENV_FILE —— 客户端凭据只能来自已经 export 的环境变量" >&2
fi
export CRTEST_ENV_FILE="$ENV_FILE"
export E2B_DEPLOY_DIR="$DEPLOY"
export CRTEST_PY="$PY"
[ -n "$RESTART_CMD" ] && export CRTEST_RESTART_CMD="$RESTART_CMD"

for v in E2B_API_KEY E2B_ACCESS_TOKEN; do
	eval "val=\${$v:-}"
	[ -n "$val" ] || echo "!! $v 没有值（$ENV_FILE 里没有，环境里也没有）——建沙箱会失败" >&2
done

# ---------------------------------------------------------------- 结果目录
TS=$(date +%Y%m%d-%H%M%S)
OUT="$RESULTS_ROOT/$TS-$TIER"
[ -e "$OUT" ] && die "$OUT 已存在，不覆盖"
mkdir -p "$OUT" || die "建不出 $OUT"
SUMMARY="$OUT/SUMMARY.md"
RUNLOG="$OUT/run.log"

exec 3>&1
say() { echo "$*" | tee -a "$RUNLOG" >&3; }

N_PASS=0; N_FAIL=0; N_SKIP=0
ROWS=""
ITEM_PID=""  # item 正在跑的那一项（timeout 进程）的 pid
ITEM_RC=0    # item 刚跑完那一项的退出码（hdbss 档据此决定后面的项跑不跑）
INTERRUPTED=0

# Ctrl-C / TERM：停掉正在跑的那一项（让它把自己的沙箱收掉），后面的项记 SKIP，照常写 SUMMARY。
# 不处理的话 run.sh 会当场退出，而那一项因为在 timeout 自己的进程组里会留在后台继续跑。
on_signal() {
	INTERRUPTED=1
	say ""
	say "!! 收到中断：停掉当前这一项，后面的项不再跑，照常收尾写 SUMMARY"
	[ -n "$ITEM_PID" ] && kill -INT "$ITEM_PID" 2>/dev/null
}
trap on_signal INT TERM

row() { # row <名字> <结果> <耗时s> <关键数字> <产物>
	ROWS="$ROWS| $1 | $2 | $3 | $4 | $5 |
"
}

skip() { # skip <名字> <原因>
	N_SKIP=$((N_SKIP + 1))
	say "  SKIP  $1 —— $2"
	row "$1" "SKIP" "-" "$2" "-"
}

# item <名字> <日志基名> <超时秒> <关键数字提取函数名> -- <命令...>
item() {
	local name=$1 base=$2 tmo=$3 keyfn=$4
	shift 5 # 跳过 --
	local log="$OUT/$base.log"
	[ -e "$log" ] && { say "  !! $log 已存在，跳过以免覆盖"; skip "$name" "日志文件已存在"; return; }
	[ "$INTERRUPTED" = 1 ] && { skip "$name" "被中断，没有跑"; return; }
	say ""
	say "== $name =="
	say "   \$ $*"
	local t0 rc t1
	t0=$(date +%s)
	# 放后台再 wait：bash 等前台命令时收到的信号要等命令结束才处理，等 wait 时则立刻处理。
	# on_signal 靠 ITEM_PID 把信号转给这一项（timeout 自成一个进程组，终端的 Ctrl-C 到不了它；
	# 它收到 INT 再转给脚本，脚本收尾时 kill 掉自己建的沙箱）。
	timeout --signal=INT --kill-after=60 "$tmo" "$@" >"$log" 2>&1 &
	ITEM_PID=$!
	wait "$ITEM_PID"
	rc=$?
	# 被信号打断的 wait 会先返回（> 128），这时那一项还在收尾（kill 沙箱），再等它真结束
	while kill -0 "$ITEM_PID" 2>/dev/null; do
		wait "$ITEM_PID"
		rc=$?
	done
	ITEM_PID=""
	ITEM_RC=$rc
	t1=$(date +%s)
	local dt=$((t1 - t0))
	local key=""
	[ -n "$keyfn" ] && key=$("$keyfn" "$log" 2>/dev/null | head -1)
	[ -n "$key" ] || key="见 $base.log"
	case $rc in
	0) N_PASS=$((N_PASS + 1)); say "  PASS  $name（${dt}s）  $key"; row "$name" "PASS" "${dt}s" "$key" "$base.log" ;;
	3) N_SKIP=$((N_SKIP + 1)); say "  SKIP  $name（${dt}s）前置不满足  $key"; row "$name" "SKIP" "${dt}s" "前置不满足：$key" "$base.log" ;;
	124 | 130 | 137) N_FAIL=$((N_FAIL + 1)); say "  FAIL  $name（${dt}s）超时/中断（上限 ${tmo}s）"; row "$name" "FAIL" "${dt}s" "超时（上限 ${tmo}s）" "$base.log" ;;
	*) N_FAIL=$((N_FAIL + 1)); say "  FAIL  $name（${dt}s）退出码 $rc  $key"; row "$name" "FAIL" "${dt}s" "退出码 $rc：$key" "$base.log" ;;
	esac
	tail -3 "$log" | sed 's/^/      /' | tee -a "$RUNLOG" >&3
}

# ---------------------------------------------------------------- 关键数字提取
key_preflight() { grep -a '^汇总:' "$1" | tail -1 | sed 's/\x1b\[[0-9;]*m//g'; }
key_tail() { tail -1 "$1" | sed 's/\x1b\[[0-9;]*m//g' | cut -c1-160; }
key_verify() {
	grep -aE '项校验全部通过|项校验，其中|校验未通过' "$1" | tail -1 | sed 's/\x1b\[[0-9;]*m//g' | cut -c1-160 ||
		key_tail "$1"
}
key_crtest() {
	grep -aE '^== .* (通过|失败|前置不满足)' "$1" | tail -1 | cut -c1-160 ||
		key_tail "$1"
}
key_pytest() { grep -aE '[0-9]+ (passed|failed|error)' "$1" | tail -1 | sed 's/^=*//;s/=*$//' | cut -c1-160; }
key_cap() { grep -a 'track_dirty_pages' "$1" | tail -1 | sed 's/\x1b\[[0-9;]*m//g' | cut -c1-200; }
# 能力文件（与日志行同一套键，另加 pid、started_at、version、commit）里挑出和
# key_cap 同样要紧的几项。先压成一行，数组（fault_inject）也就成了一段
key_capfile() {
	local k v one out=""
	one=$(tr -d '\n' <"$1" | sed 's/[[:space:]][[:space:]]*/ /g')
	for k in track_dirty_pages track_dirty_pages_reason fault_inject pid version commit; do
		v=$(sed -nE "s/.*\"$k\": (\[[^]]*\]|\"[^\"]*\"|[^,}]*).*/\1/p" <<<"$one" | sed 's/^"//; s/"$//; s/ *$//')
		[ -n "$v" ] && out="$out$k=$v "
	done
	echo "${out% }" | cut -c1-200
}
key_serial() { grep -a '客户端墙钟' "$1" | tail -1 | cut -c1-160; }
key_compliance() { grep -a '^\*\*' "$1" | tail -1 | cut -c1-160; }
key_hdbss() { grep -aE '^(关键:|前置不满足：|断言失败：)' "$1" | tail -1 | sed 's/^关键: *//; s/^前置不满足：//' | cut -c1-200; }

# ---------------------------------------------------------------- 开跑
{
	echo "档       : $TIER"
	echo "时间     : $(date -Is)"
	echo "主机     : $(hostname)  $(uname -m)  $(uname -r)"
	echo "部署根   : $DEPLOY"
	echo "env 文件 : $ENV_FILE"
	echo "解释器   : $PY  ($("$PY" -V 2>&1))"
	echo "模板     : $TEMPLATE"
	echo "结果目录 : $OUT"
} | tee -a "$RUNLOG" >&3

CRT=("$PY" -m crtest)
export PYTHONPATH="$CRTEST${PYTHONPATH:+:$PYTHONPATH}"
COMMON=(--env-file "$ENV_FILE" --template "$TEMPLATE")

crtest_case() { # crtest_case <用例> <超时秒> [额外参数...]
	local c=$1 tmo=$2
	shift 2
	item "crtest $c" "crtest-$c" "$tmo" key_crtest -- \
		"${CRT[@]}" "$c" "${COMMON[@]}" --out "$OUT/$c.json" "$@"
}

# ---------------------------------------------------------------- hdbss 档：改 job / 恢复
# order 扫描要给 orchestrator 所在的 nomad job 加一个环境变量并重启。做法是「拿运行中的
# 定义改一个字段再提交」，不碰 $DEPLOY 下任何文件、不重新 render：
#   nomad job inspect → job-before.json（原样留底）
#   → 只在 task 的 Env 里加 / 改 FC_HDBSS_ORDER → nomad job run -json
#   → 扫完把 job-before.json 原样提交回去（定义和扫描前逐项相同，只是 job 版本号往前走）。
# 这样无论这台机器上的 job 是 RPM 渲染出来的，还是之后手工改过的，恢复的都是扫描前那一份。
HDBSS_BANNER=""
HDBSS_STATE=""       # 追加进 SUMMARY 的「order 扫描与 job 恢复」一节（文件路径）
JOB_TOUCHED=0        # 提交过改动的 job 定义
JOB_SUBMITS=0        # 一共提交了几次（每次都换一个 orchestrator 进程）
JOB_RESTORED=0
ORDERS_DONE=""
BACKEND=""

# orch_pid [job]：正在运行的 orchestrator / template-manager 的 pid（按 exe 认，不用 pgrep -f）。
# 给了 job 名就只认进程环境里 NOMAD_JOB_ID 是它的那一个 —— 机器上不止一个这样的进程时，
# 等重启要盯的是被改的那个 job 的进程。
orch_pid() {
	local p exe
	for p in /proc/[0-9]*; do
		exe=$(readlink "$p/exe" 2>/dev/null) || continue
		case ${exe##*/} in orchestrator* | template-manager*) ;; *) continue ;; esac
		[ -n "${1:-}" ] && [ "$(penv "${p#/proc/}" NOMAD_JOB_ID)" != "$1" ] && continue
		echo "${p#/proc/}"
		return 0
	done
	return 1
}
penv() { tr '\0' '\n' <"/proc/$1/environ" 2>/dev/null | sed -n "s/^$2=//p" | head -1; }
orch_port() { # orchestrator 的 gRPC / health 端口：取自它的命令行 --port，取不到按 5008
	local port
	port=$(tr '\0' ' ' <"/proc/$1/cmdline" 2>/dev/null | sed -n 's/.*--port[ =]\([0-9][0-9]*\).*/\1/p')
	echo "${port:-5008}"
}
orch_health() { curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:$1/health" 2>/dev/null; }
live_fc() { pidof firecracker | wc -w; } # 活着的 firecracker 进程数

# job_view <inspect.json>：把 job 定义里会影响进程的部分（启动命令 + Env）排好序打成文本，
# 供扫描前后 diff。名字像密钥的变量只打值的 sha256 前 12 位 —— 这份文本会进 SUMMARY。
job_view() {
	"$PY" - "$1" <<'PYEOF'
import hashlib, json, re, sys
j = json.load(open(sys.argv[1]))["Job"]
for g in j.get("TaskGroups") or []:
    for t in g.get("Tasks") or []:
        print("task %s/%s driver=%s" % (g.get("Name"), t.get("Name"), t.get("Driver")))
        print("  config %s" % json.dumps(t.get("Config"), sort_keys=True, ensure_ascii=False))
        for k, v in sorted((t.get("Env") or {}).items()):
            if re.search(r"SECRET|TOKEN|PASSWORD|_KEY$|ACCESS_KEY|CONNECTION_STRING", k):
                v = "sha256:" + hashlib.sha256(str(v).encode()).hexdigest()[:12]
            print("  env %s=%s" % (k, v))
PYEOF
}

job_version() { "$PY" -c 'import json, sys; print(json.load(open(sys.argv[1]))["Job"].get("Version"))' "$1" 2>/dev/null; }

# job_with_order <inspect.json> <out.json> <order>：order 为空 = 不改 Env（恢复用）。
# 版本号之类的字段去掉，交给 nomad 重新分配，免得被当成过期提交拒绝。
job_with_order() {
	"$PY" - "$1" "$2" "$3" <<'PYEOF'
import json, os, sys
src, dst, order = sys.argv[1:4]
j = json.load(open(src))
if order != "":
    for g in j["Job"].get("TaskGroups") or []:
        for t in g.get("Tasks") or []:
            if t.get("Env") is None:
                t["Env"] = {}
            t["Env"]["FC_HDBSS_ORDER"] = order
for k in ("Version", "SubmitTime", "CreateIndex", "ModifyIndex", "JobModifyIndex", "Status",
          "StatusDescription", "Stable"):
    j["Job"].pop(k, None)
fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)   # 里面有 job 的全部 env（含密钥）
with os.fdopen(fd, "w") as f:
    json.dump(j, f, indent=1)
PYEOF
}

# job_apply <要提交的.json> <期望的 FC_HDBSS_ORDER，空 = 不核对这一项> <日志>
# 提交 → 等旧进程退、新进程起、health 200、进程环境对得上。定义没变就不提交、不重启。
# 返回 0 成功，1 失败。不放进子 shell：JOB_TOUCHED / JOB_SUBMITS 要留在本进程里。
job_apply() {
	local f=$1 want=$2 log=$3 old new="" port h="" i ok=0 cur="$OUT/.job-cur.json"
	echo "## $(date +%T) job_apply $(basename "$f") 期望 FC_HDBSS_ORDER=${want:-（不核对）}" >>"$log"
	if ! nomad job inspect "$JOB" >"$cur" 2>>"$log"; then
		echo "nomad job inspect $JOB 失败" >>"$log"
		return 1
	fi
	chmod 600 "$cur"
	if [ "$(job_view "$cur")" = "$(job_view "$f")" ]; then
		echo "job 定义与目标相同，不提交、不重启" >>"$log"
		return 0
	fi
	old=$(orch_pid "$JOB" || true)
	echo "提交前 pid=${old:-无}，活着的 firecracker $(live_fc) 个" >>"$log"
	for i in 1 2 3 4 5 6; do
		if nomad job run -detach -json "$f" >"$OUT/.job-run.out" 2>&1; then ok=1; break; fi
		# 旧 alloc 还占着静态端口时会报 port collision，等它退干净再试
		grep -qi 'port collision\|exhausted' "$OUT/.job-run.out" || break
		echo "端口还被旧 alloc 占着，15 s 后重试（第 $i 次）" >>"$log"
		sleep 15
	done
	cat "$OUT/.job-run.out" >>"$log"
	if [ "$ok" != 1 ]; then
		echo "nomad job run 失败" >>"$log"
		return 1
	fi
	JOB_TOUCHED=1
	JOB_SUBMITS=$((JOB_SUBMITS + 1))
	for i in $(seq 1 60); do
		# 切 order 的途中被中断就别等了，回去恢复；恢复那一次（want 为空）要等到底
		[ "$INTERRUPTED" = 1 ] && [ -n "$want" ] && return 1
		new=$(orch_pid "$JOB" || true)
		if [ -n "$new" ] && [ "$new" != "$old" ]; then
			port=$(orch_port "$new")
			h=$(orch_health "$port")
			if [ "$h" = 200 ] && { [ -z "$want" ] || [ "$(penv "$new" FC_HDBSS_ORDER)" = "$want" ]; }; then
				echo "$(date +%T) 新进程就绪：pid ${old:-无} → $new，:$port/health=200，FC_HDBSS_ORDER=$(penv "$new" FC_HDBSS_ORDER)" >>"$log"
				return 0
			fi
		fi
		sleep 5
	done
	echo "$(date +%T) 等了 300 s 没就绪：pid=${new:-无}（旧 ${old:-无}）health=${h:-?}" >>"$log"
	return 1
}

# hdbss_restore_job：把 job 恢复成扫描前的定义，并把最后停在什么状态写成一节。幂等；
# 正常收尾、中途失败、Ctrl-C、脚本自己出错退出（EXIT trap）都走这里。
hdbss_restore_job() {
	[ -r "$OUT/job-before.json" ] || return 0
	[ "$JOB_RESTORED" = 1 ] && return 0
	JOB_RESTORED=1
	trap '' INT TERM # 恢复途中不再理会中断：忽略会被子进程继承，nomad 那一次提交不会被 Ctrl-C 打断
	local t0 dt ok=1 p port="" after="$OUT/job-after.json" diff_out cur_order
	t0=$(date +%s)
	say ""
	say "== 恢复 job $JOB（扫描前的定义） =="
	if [ "$JOB_TOUCHED" = 1 ]; then
		if ! job_with_order "$OUT/job-before.json" "$OUT/job-restore.json" "" ||
			! job_apply "$OUT/job-restore.json" "" "$OUT/hdbss-job.log"; then
			ok=0
		fi
	fi
	nomad job inspect "$JOB" >"$after" 2>>"$OUT/hdbss-job.log" || ok=0
	chmod 600 "$after" 2>/dev/null
	job_view "$OUT/job-before.json" >"$OUT/job-before.view.txt" 2>/dev/null
	job_view "$after" >"$OUT/job-after.view.txt" 2>/dev/null
	diff_out=$(diff "$OUT/job-before.view.txt" "$OUT/job-after.view.txt" 2>&1) || ok=0
	p=$(orch_pid "$JOB" || true)
	cur_order=""
	if [ -n "$p" ]; then
		port=$(orch_port "$p")
		cur_order=$(penv "$p" FC_HDBSS_ORDER)
	fi
	dt=$(($(date +%s) - t0))
	HDBSS_STATE="$OUT/job-state.md"
	{
		echo "## order 扫描与 job 恢复"
		echo ""
		echo "| 项 | 值 |"
		echo "|---|---|"
		echo "| nomad job | \`$JOB\` |"
		echo "| 要扫的 order | $ORDERS |"
		echo "| 测完的 order | ${ORDERS_DONE:-无} |"
		echo "| 是否被中断 | $([ "$INTERRUPTED" = 1 ] && echo '是（收到 INT / TERM）' || echo 否) |"
		echo "| 提交 job 的次数 | $JOB_SUBMITS（含恢复那一次；每次换一个 orchestrator 进程） |"
		echo "| 启动命令 + Env 与扫描前对比 | $([ -z "$diff_out" ] && echo '**完全相同**' || echo '**不同，见下**') |"
		echo "| job 版本 | 扫描前 $(job_version "$OUT/job-before.json") → 现在 $(job_version "$after")（每提交一次加 1） |"
		echo "| 现在的 orchestrator | pid ${p:-无} · \`$([ -n "$p" ] && readlink "/proc/$p/exe")\` · health $([ -n "$p" ] && orch_health "$port") |"
		echo "| 现在进程环境里的 FC_HDBSS_ORDER | ${cur_order:-未设（Firecracker 取默认 1）} |"
		echo "| 活着的 firecracker / netns 数 | $(live_fc) / $(ls /run/netns 2>/dev/null | wc -l) |"
		echo ""
		if [ -n "$diff_out" ]; then
			echo "扫描前（<）与现在（>）的差异："
			echo ""
			echo '```'
			echo "$diff_out"
			echo '```'
			echo ""
		fi
		if [ "$ok" != 1 ]; then
			echo "**job 没有恢复成扫描前的样子。**手工恢复（\`job-restore.json\` = 扫描前的 \`job-before.json\` 去掉版本号字段）："
			echo ""
			echo '```bash'
			echo "export NOMAD_TOKEN=\$(cat /data/nomad/acl.token)"
			echo "nomad job run -json $OUT/job-restore.json"
			echo '```'
			echo ""
		fi
		echo "每重启一次 orchestrator，这台机器上的沙箱与 checkpoint 全部作废，网络槽位会多漏一池"
		echo "（手册 29 篇 §9）；要不要清按那一节判断。"
		echo "\`job-before.json\` / \`job-after.json\` / \`job-restore.json\` / \`job-order*.json\` 里有 job 的全部环境变量（含密钥），**不要交回**。"
	} >"$HDBSS_STATE"
	if [ "$ok" = 1 ]; then
		N_PASS=$((N_PASS + 1))
		say "  PASS  job 已恢复成扫描前的定义（${dt}s）"
		row "恢复 job \`$JOB\`" "PASS" "${dt}s" "启动命令与 Env 和扫描前完全相同；进程环境里 FC_HDBSS_ORDER=${cur_order:-未设}" "job-state.md"
	else
		N_FAIL=$((N_FAIL + 1))
		say "  FAIL  job 没有恢复成扫描前的样子（${dt}s）—— 看 $HDBSS_STATE 与 hdbss-job.log"
		row "恢复 job \`$JOB\`" "FAIL" "${dt}s" "没有恢复成扫描前的定义，手工恢复命令见 job-state.md" "hdbss-job.log"
	fi
}

case "$TIER" in

# ================================================================ smoke
smoke)
	PRE=("bash" "$CRTEST/portability/preflight-customer.sh")
	[ -n "$STORE" ] && PRE+=(--store "$STORE")
	item "宿主预检 preflight-customer.sh" "preflight" 180 key_preflight -- "${PRE[@]}"

	if [ -r "$SDK_INSTALL" ]; then
		item "SDK 覆盖层自检 install.py --check" "sdk-check" 120 key_tail -- \
			"$PY" "$SDK_INSTALL" --check
	else
		skip "SDK 覆盖层自检 install.py --check" "找不到 $SDK_INSTALL（--sdk-install 指定）"
	fi

	# checkpoint capabilities：这台机器的脏页跟踪开没开、为什么开，各项超时、上限与
	# 故障注入。hdbss / kvm-wp 这个更细的后端名来自 Firecracker 自己（FC API 的
	# dirty_tracking 字段），由下面的 checkpoint_verify.py 打印。
	#
	# 先读能力文件：orchestrator（deltabox-dev 93ccb02 起）启动时把同样的内容写进
	# DEFAULT_CACHE_DIR/checkpoint-capabilities.json（store 根的父目录），不怕日志轮转。
	# 目录按进程 environ 取：DEFAULT_CACHE_DIR → $ORCHESTRATOR_BASE_PATH/build →
	# /orchestrator/build；给了 --store 就取它的父目录。文件里的 pid 必须是正在运行的
	# orchestrator / template-manager，否则是以前那次启动留下的，不算。
	# 没有有效的文件（此前的版本不写）就退回读启动日志行。
	CAPLOG="$OUT/capabilities.log"
	CAPFILE=""
	{
		echo "# orchestrator / template-manager 的 checkpoint capabilities（能力文件，或启动时打的那一行）"
		found=0
		cur_alloc=""
		stale=""
		for p in /proc/[0-9]*; do
			exe=$(readlink "$p/exe" 2>/dev/null) || continue
			case ${exe##*/} in orchestrator* | template-manager*) ;; *) continue ;; esac
			pid=${p#/proc/}
			penv=$(tr '\0' '\n' <"$p/environ" 2>/dev/null)
			if [ -n "$STORE" ]; then
				capdir=$(dirname "$STORE")
			else
				base=$(sed -n 's/^ORCHESTRATOR_BASE_PATH=//p' <<<"$penv" | head -1)
				base=${base:-/orchestrator}
				capdir=$(sed -n 's/^DEFAULT_CACHE_DIR=//p' <<<"$penv" | head -1)
				capdir=${capdir//\$\{ORCHESTRATOR_BASE_PATH\}/$base}
				capdir=${capdir:-$base/build}
			fi
			cf="$capdir/checkpoint-capabilities.json"
			if [ -r "$cf" ]; then
				fpid=$(grep -aE '^  "pid": [0-9]+,?$' "$cf" | head -1 | tr -dc '0-9')
				if [ "$fpid" = "$pid" ]; then
					echo "# 运行中的进程 $pid：$exe"
					echo "## $cf"
					cat "$cf"
					CAPFILE=$cf
					found=1
					break
				fi
				stale="$stale$cf 的 pid ${fpid:-?} 不是运行中的进程 $pid；"
			fi
			# 日志回退只读**正在运行**的那个进程所在 alloc 的日志：/data/nomad/alloc 下
			# 还留着以前各次部署、切栈留下的旧 alloc，它们的启动行（可能带故障注入、
			# 配额 env）不代表现在这台机器的状态。alloc 目录取自该进程的 NOMAD_ALLOC_DIR。
			if [ -z "$cur_alloc" ]; then
				cur_alloc=$(sed -n 's/^NOMAD_ALLOC_DIR=//p' <<<"$penv" | head -1)
				[ -n "$cur_alloc" ] && echo "# 运行中的进程 $pid：$exe"
			fi
		done
		if [ "$found" = 0 ]; then
			if [ -n "$stale" ]; then
				echo "## 能力文件是以前的启动留下的，不用：$stale"
			else
				echo "## 没有能力文件（此前的版本不写），读启动日志行"
			fi
			if [ -n "$cur_alloc" ] && [ -d "$cur_alloc/logs" ]; then
				# 日志会轮转（start.stdout.0、.1、…），从序号大的（新的）往回找最后一次启动行
				for f in $(ls -1 "$cur_alloc/logs" 2>/dev/null | grep '^start\.stdout\.[0-9]*$' | sort -t. -k3,3nr); do
					f="$cur_alloc/logs/$f"
					if line=$(timeout 60 grep -a 'checkpoint capabilities' "$f" 2>/dev/null | tail -1) &&
						[ -n "$line" ]; then
						echo "## $f"
						echo "$line"
						found=1
						break
					fi
				done
				[ "$found" = 1 ] || echo "## 当前 alloc $cur_alloc 的日志里没有（启动行可能已被轮转掉）"
			else
				echo "## 没找到运行中的 orchestrator / template-manager 进程或它的 alloc 目录"
			fi
		fi
		if [ "$found" = 0 ]; then
			echo "## journald"
			timeout 60 journalctl -u orchestrator --no-pager 2>/dev/null |
				grep -a 'checkpoint capabilities' | tail -1
		fi
	} >"$CAPLOG" 2>&1
	if [ -n "$CAPFILE" ]; then
		N_PASS=$((N_PASS + 1))
		say ""
		say "== checkpoint capabilities（能力文件） =="
		sed 's/^/   /' "$CAPLOG" | tee -a "$RUNLOG" >&3
		row "checkpoint capabilities 能力文件" "PASS" "-" "$(key_capfile "$CAPFILE")" "capabilities.log"
	elif grep -aq 'track_dirty_pages' "$CAPLOG"; then
		N_PASS=$((N_PASS + 1))
		say ""
		say "== checkpoint capabilities =="
		sed 's/^/   /' "$CAPLOG" | tee -a "$RUNLOG" >&3
		row "checkpoint capabilities 日志行" "PASS" "-" "$(key_cap "$CAPLOG")" "capabilities.log"
	else
		N_SKIP=$((N_SKIP + 1))
		say "  SKIP  checkpoint capabilities 日志行 —— 没有有效的能力文件，当前 alloc 日志与 journald 里也没抓到"
		row "checkpoint capabilities 日志行" "SKIP" "-" "能力文件与日志里都没有（换个日志源手工搜）" "capabilities.log"
	fi

	FCLOG="$OUT/fc-sha256.log"
	{
		for fc in "${FC_VERSIONS_DIR:-/fc-versions}"/*/firecracker "$DEPLOY"/bin/firecracker; do
			[ -r "$fc" ] && timeout 120 sha256sum "$fc"
		done
	} >"$FCLOG" 2>&1
	if [ -s "$FCLOG" ]; then
		N_PASS=$((N_PASS + 1))
		say ""
		say "== firecracker sha256 =="
		sed 's/^/   /' "$FCLOG" | tee -a "$RUNLOG" >&3
		row "firecracker 二进制 sha256" "PASS" "-" "$(head -1 "$FCLOG")" "fc-sha256.log"
	else
		N_FAIL=$((N_FAIL + 1))
		say "  FAIL  firecracker 二进制 sha256 —— 一个都没找到"
		row "firecracker 二进制 sha256" "FAIL" "-" "/fc-versions 与 $DEPLOY/bin 下都没有 firecracker" "fc-sha256.log"
	fi

	item "功能正确性 checkpoint_verify.py" "checkpoint_verify" 900 key_verify -- \
		"$PY" "$ACCEPT/checkpoint_verify.py" --template "$TEMPLATE"
	;;

# ================================================================ func
func)
	# 顺序：短的在前，失败早暴露；括号里是 920B 历史中位耗时。
	crtest_case T12 900   # 8.5 s
	crtest_case T15 900   # 7.6 s
	crtest_case T22 900   # 12 s
	crtest_case T24 900   # 12 s
	crtest_case T23 900   # 13 s
	crtest_case T34 900   # 14 s
	crtest_case T32 900   # 18 s
	crtest_case T41 900   # 19 s
	crtest_case T39 900   # 21 s
	crtest_case T26 900   # 26 s
	crtest_case T14 1200  # 50 s
	crtest_case T40 1800  # 146 s
	crtest_case T25 1800  # 153 s
	crtest_case T36 1800  # 178 s
	crtest_case T11 1800  # 188 s
	crtest_case T13 1800  # 197 s

	if [ "$ALLOW_RESTART" = 1 ]; then
		T18_ARGS=(--allow-restart)
		[ -n "$RESTART_CMD" ] && T18_ARGS+=(--restart-cmd "$RESTART_CMD")
		crtest_case T18 1800 "${T18_ARGS[@]}"
	else
		skip "crtest T18（orchestrator 重启后账本不过夜）" \
			"要重启 orchestrator，全机沙箱都会没；要跑加 --allow-restart 并给 --restart-cmd"
	fi
	if [ "$FAULT_CASES" = 1 ]; then
		for fault in envd_timeout torn_assemble seal_move commit_late; do
			crtest_case T21 900 --fault "$fault"
		done
		crtest_case T38 1800 --fault fc_post_commit
	else
		skip "crtest T21（四条故障注入）" \
			"要服务端带 CHECKPOINT_FAULT_INJECT=<名字> 重启；交付态不改线上配置。要跑加 --fault-cases"
		skip "crtest T38（FC 侧 faulted 路径）" \
			"要 FC 带 cargo feature rollback-fault-inject 且 FC_ROLLBACK_FAULT_INJECT=post_commit；交付的 FC 不带。要跑加 --fault-cases"
	fi
	if [ "$QUOTA_CASES" = 1 ]; then
		crtest_case T37 900 --quota disk_full
		crtest_case T37 900 --quota too_many
	else
		skip "crtest T37（两道配额闸）" \
			"要服务端带 CHECKPOINT_MIN_FREE_BYTES / CHECKPOINT_MAX_PER_SANDBOX 重启。要跑加 --quota-cases"
	fi

	# SDK 异常语义：纯本地 pytest，打桩服务端，不建沙箱、不碰栈。
	if "$PY" -c 'import pytest' >/dev/null 2>&1; then
		item "SDK 异常语义 pytest test_checkpoint_errors.py" "sdk-errors" 600 key_pytest -- \
			"$PY" -m pytest -q -p no:cacheprovider "$CRTEST/sdktests/test_checkpoint_errors.py"
	else
		skip "SDK 异常语义 pytest test_checkpoint_errors.py" \
			"这个解释器没装 pytest（pip install pytest 之后重跑；950 能连外网）"
	fi
	;;

# ================================================================ perf
perf)
	if [ "$QUICK" = 1 ]; then
		say ""
		say "!! --quick：这是**短试跑**，只验证三个脚本跑得通，样本数远不够，"
		say "   数字不能当正式结论用。正式采数去掉 --quick。"
		BENCH_ARGS=(--tier-set short --tiers mix8,mix64 --iters 5 --passes 1 --deadline-min 6)
		SERIAL_N=10
		CONC_ARGS=(--stages A --fanout 1,4)
		BENCH_TMO=900; SERIAL_TMO=600; CONC_TMO=900
	else
		BENCH_ARGS=(--tier-set short --passes 2 --deadline-min 25)
		CONC_ARGS=(--stages A,B --fanout 1,2,4,8,16 --same-threads 4 --same-rounds 5)
		BENCH_TMO=2400; SERIAL_TMO=2400; CONC_TMO=2400
	fi

	item "分档基准 bench_tiers.py" "bench-tiers" "$BENCH_TMO" key_crtest -- \
		"$PY" "$CRTEST/bench/bench_tiers.py" "${COMMON[@]}" \
		"${BENCH_ARGS[@]}" --outdir "$OUT" --out "$OUT/bench-tiers.json"

	JSONL=$(ls -t "$OUT"/raw-*.jsonl 2>/dev/null | head -1)
	if [ -n "$JSONL" ]; then
		item "分档达标判定 compliance.py" "compliance" 300 key_compliance -- \
			"$PY" "$CRTEST/bench/compliance.py" "$JSONL"
		"$PY" "$CRTEST/bench/analyze.py" "$JSONL" >"$OUT/analyze.log" 2>&1 || true
	else
		skip "分档达标判定 compliance.py" "bench_tiers.py 没留下 raw-*.jsonl"
	fi

	item "单沙箱串行 restore 长尾（n=$SERIAL_N）" "serial-restore" "$SERIAL_TMO" key_serial -- \
		"$PY" "$CRTEST/bench/serial_restore.py" "${COMMON[@]}" \
		-n "$SERIAL_N" --deadline-min 25 --outdir "$OUT" --out "$OUT/serial-restore.json"

	item "并发 checkpoint_concurrent.py" "concurrent-ab" "$CONC_TMO" key_tail -- \
		"$PY" "$ACCEPT/checkpoint_concurrent.py" --template "$TEMPLATE" \
		"${CONC_ARGS[@]}" --out "$OUT/concurrent-ab.json"
	;;

# ================================================================ hdbss
hdbss)
	# 只量 HDBSS 自己，不做与软件写保护的对照。脏页后端不是 hdbss 的机器上整档 SKIP，
	# 除非带 --force-non-hdbss（仅流程验证）。
	HB="$CRTEST/bench/hdbss_bench.py"
	HB_ARGS=("${COMMON[@]}" --outdir "$OUT")
	[ "$FORCE_NON_HDBSS" = 1 ] && HB_ARGS+=(--force-non-hdbss)
	PLAN=()
	if [ "$QUICK" = 1 ]; then
		say ""
		say "!! --quick：这是**短试跑**，只验证脚本跑得通，样本数远不够，数字不能当结论用。"
		PLAN+=(--quick)
		HB_TMO=600
	else
		HB_TMO=1500
	fi
	[ -n "$HDBSS_ITERS" ] && PLAN+=(--iters "$HDBSS_ITERS")

	# ① 证据：L1 KVM cap 502、L2 FC 自报的 dirty_tracking、L3 冷写 / 热写与 KVM 退出数，
	#    外加 order 核实（进程环境、dmesg、FC 日志）。退出码 3 = 这台机器 / 这个沙箱没在用
	#    HDBSS，后面的项全部 SKIP —— 尤其不会为了扫 order 去重启 orchestrator。
	EV_ARGS=(--evidence)
	[ "$QUICK" = 1 ] && EV_ARGS+=(--quick)
	item "HDBSS 证据（L1 能力 / L2 自报 / L3 数据面 / order 核实）" "hdbss-evidence" 600 key_hdbss -- \
		"$PY" "$HB" "${HB_ARGS[@]}" "${EV_ARGS[@]}" --out "$OUT/hdbss-evidence.json"
	EV_RC=$ITEM_RC
	BACKEND=$(sed -n 's/^脏页后端 *: *\([^ （]*\).*/\1/p' "$OUT/hdbss-evidence.log" | tail -1)
	if [ "$EV_RC" = 0 ] && [ "$BACKEND" != hdbss ]; then
		HDBSS_BANNER="> # ⚠ 非 HDBSS，仅流程验证，数字无意义
>
> 这台机器的脏页后端是 \`${BACKEND:-?}\`，不是 \`hdbss\`；本轮是带 \`--force-non-hdbss\` 跑的，
> 只说明脚本与流程跑得通。下面所有耗时、退出数、差分大小都**不是** HDBSS 的数据，不能引用；
> \`FC_HDBSS_ORDER\` 在这里不生效，order 核实只核对了进程环境变量。"
	fi

	if [ "$EV_RC" = 3 ]; then
		skip "写密集基准" "证据项前置不满足：脏页后端不是 hdbss（${BACKEND:-?}）。本档只量 HDBSS 自己；只想验证流程加 --force-non-hdbss"
		[ -n "$ORDERS" ] && skip "order 扫描（$ORDERS）" "同上；没有改 job、没有重启"
	elif [ "$EV_RC" != 0 ]; then
		skip "写密集基准" "证据项没过（退出码 $EV_RC），先看 hdbss-evidence.log"
		[ -n "$ORDERS" ] && skip "order 扫描（$ORDERS）" "同上；没有改 job、没有重启"
	elif [ -z "$ORDERS" ]; then
		# ② 不扫描：只测当前 order，不动 job、不重启
		item "写密集基准（当前 order）" "hdbss-order-current" "$HB_TMO" key_hdbss -- \
			"$PY" "$HB" "${HB_ARGS[@]}" "${PLAN[@]}" --out "$OUT/hdbss-order-current.json"
	else
		# ③ order 扫描：每个 order 改 job 的 env → 重启 → 等就绪 → 跑基准（带 --expect-order，
		#    基准自己核对进程环境、dmesg、FC 日志里的 order）。任何一步失败就停扫，直接去恢复。
		command -v nomad >/dev/null 2>&1 || die "找不到 nomad 命令，扫不了 order"
		command -v curl >/dev/null 2>&1 || die "找不到 curl（等 orchestrator 就绪要用）"
		if [ -z "${NOMAD_TOKEN:-}" ]; then
			NOMAD_TOKEN=$(sed -n 's/^NOMAD_ACL_TOKEN=//p' "$DEPLOY/.env" 2>/dev/null | head -1 | tr -d "\"'")
			[ -n "$NOMAD_TOKEN" ] || NOMAD_TOKEN=$(cat "${NOMAD_DATA_DIR:-/data/nomad}/acl.token" 2>/dev/null)
		fi
		export NOMAD_TOKEN
		OPID=$(orch_pid) || die "没找到运行中的 orchestrator / template-manager 进程"
		[ -n "$JOB" ] || JOB=$(penv "$OPID" NOMAD_JOB_ID)
		[ -n "$JOB" ] || JOB=template-manager-system
		if ! nomad job inspect "$JOB" >"$OUT/job-before.json" 2>"$OUT/hdbss-job.log"; then
			rm -f "$OUT/job-before.json"
			N_FAIL=$((N_FAIL + 1))
			say "  FAIL  order 扫描 —— nomad job inspect $JOB 失败（NOMAD_TOKEN 对吗？），没有改任何东西"
			row "order 扫描（$ORDERS）" "FAIL" "-" "nomad job inspect $JOB 失败，没有改任何东西" "hdbss-job.log"
		else
			chmod 600 "$OUT/job-before.json"
			trap hdbss_restore_job EXIT
			say ""
			say "!! order 扫描 $ORDERS：要重启 $(echo "$ORDERS" | tr ',' '\n' | wc -l) 次 job $JOB（另加恢复 1 次）。"
			say "   现在的 orchestrator：pid $OPID $(readlink "/proc/$OPID/exe")，活着的 firecracker $(live_fc) 个 —— 都会没。"
			say "   扫描前的定义已留底：$OUT/job-before.json"
			for o in $(echo "$ORDERS" | tr ',' ' '); do
				if [ "$INTERRUPTED" = 1 ]; then
					skip "写密集基准（order $o）" "被中断，没有跑"
					continue
				fi
				say ""
				say "== 切到 FC_HDBSS_ORDER=$o =="
				t0=$(date +%s)
				if job_with_order "$OUT/job-before.json" "$OUT/job-order$o.json" "$o" &&
					job_apply "$OUT/job-order$o.json" "$o" "$OUT/hdbss-job.log"; then
					N_PASS=$((N_PASS + 1))
					say "  PASS  job 已带 FC_HDBSS_ORDER=$o 就绪（$(($(date +%s) - t0))s）"
					row "改 job env：FC_HDBSS_ORDER=$o 并重启" "PASS" "$(($(date +%s) - t0))s" "$(grep -a '新进程就绪\|不提交、不重启' "$OUT/hdbss-job.log" | tail -1 | cut -c1-160)" "hdbss-job.log"
				else
					N_FAIL=$((N_FAIL + 1))
					say "  FAIL  切到 FC_HDBSS_ORDER=$o 没成功，停止扫描，去恢复 job"
					tail -5 "$OUT/hdbss-job.log" | sed 's/^/      /' | tee -a "$RUNLOG" >&3
					row "改 job env：FC_HDBSS_ORDER=$o 并重启" "FAIL" "$(($(date +%s) - t0))s" "没就绪，停止扫描" "hdbss-job.log"
					break
				fi
				if [ "$INTERRUPTED" = 1 ]; then
					skip "写密集基准（order $o）" "被中断，没有跑"
					continue
				fi
				# orchestrator 刚起来时 API 节点要一两分钟才接单：--wait-ready 在这段时间里重试建沙箱
				item "写密集基准（order $o）" "hdbss-order$o" "$((HB_TMO + 300))" key_hdbss -- \
					"$PY" "$HB" "${HB_ARGS[@]}" "${PLAN[@]}" --expect-order "$o" --wait-ready 300 \
					--out "$OUT/hdbss-order$o.json"
				[ "$ITEM_RC" = 0 ] && ORDERS_DONE="$ORDERS_DONE${ORDERS_DONE:+,}$o"
			done
			hdbss_restore_job
			trap on_signal INT TERM # 恢复期间忽略了中断，这里换回来
		fi
	fi

	# ④ 汇总：一张 order × 脏页量 × 写入方式的对比表（Markdown 进 SUMMARY，另存一份 JSON）
	BENCH_JSON=$(ls "$OUT"/hdbss-order*.json 2>/dev/null)
	if [ -n "$BENCH_JSON" ]; then
		# shellcheck disable=SC2086
		item "汇总表 order × 脏页量 × 写入方式" "hdbss-summarize" 120 key_hdbss -- \
			"$PY" "$HB" --summarize $BENCH_JSON "$OUT/hdbss-evidence.json" \
			--md "$OUT/hdbss-table.md" --json "$OUT/hdbss-summary.json"
	else
		skip "汇总表 order × 脏页量 × 写入方式" "没有一份基准结果"
	fi
	;;
esac

# ---------------------------------------------------------------- SUMMARY.md
TOTAL=$((N_PASS + N_FAIL + N_SKIP))
{
	echo "# $TIER 档结果 · $TS"
	echo ""
	if [ -n "${HDBSS_BANNER:-}" ]; then
		echo "$HDBSS_BANNER"
		echo ""
	fi
	echo "| 项 | 值 |"
	echo "|---|---|"
	echo "| 档 | \`$TIER\`$([ "$QUICK" = 1 ] && echo ' · **--quick 短试跑**（样本数远不够，只验证脚本跑得通，数字不能当结论）') |"
	echo "| 开跑 | $TS |
| 收尾 | $(date +%Y%m%d-%H%M%S) |"
	echo "| 主机 | $(hostname) · $(uname -m) · $(uname -r) |"
	echo "| 部署根 | \`$DEPLOY\` |"
	echo "| env 文件 | \`$ENV_FILE\` |"
	echo "| 解释器 | \`$PY\` · $("$PY" -V 2>&1) |"
	echo "| 模板 | \`$TEMPLATE\` |"
	if [ "$TIER" = hdbss ]; then
		echo "| 命令行 | \`run.sh $RUN_ARGS\` |"
		echo "| 脏页后端 | \`${BACKEND:-?}\`（Firecracker 自报；\`未问\` = 宿主 KVM 不认 cap 502，没有建沙箱去问） |"
		echo "| order | $([ -n "$ORDERS" ] && echo "扫描 $ORDERS（测完的：${ORDERS_DONE:-无}）" || echo '只测当前 order，没有改 job、没有重启') |"
	fi
	echo "| 结果 | **PASS $N_PASS · FAIL $N_FAIL · SKIP $N_SKIP**（共 $TOTAL 项） |"
	echo ""
	echo "## 逐项"
	echo ""
	echo "| 项 | 结果 | 耗时 | 关键数字 / 原因 | 产物 |"
	echo "|---|---|---|---|---|"
	printf '%s' "$ROWS"
	echo ""
	if [ "$N_FAIL" -eq 0 ]; then
		echo "**结论：本档没有 FAIL。**SKIP 的项及原因见上表。"
	else
		echo "**结论：有 $N_FAIL 项 FAIL，逐项看对应 .log。**"
	fi
	echo ""
	if [ "$TIER" = hdbss ]; then
		if [ -s "$OUT/hdbss-table.md" ]; then
			echo "## HDBSS 写密集基准"
			echo ""
			cat "$OUT/hdbss-table.md"
			echo ""
			echo "性能数字只记录、不判 PASS / FAIL；PASS 只表示 checkpoint / restore 没有失败、md5 全部一致、order 核实生效。"
			echo "同一张表的机器可读版在 \`hdbss-summary.json\`，每次迭代的原始记录在 \`hdbss-raw-*.jsonl\`。"
			echo ""
		fi
		if [ -n "${HDBSS_STATE:-}" ] && [ -s "$HDBSS_STATE" ]; then
			cat "$HDBSS_STATE"
			echo ""
		fi
	fi
	echo "## 怎么把结果交回"
	echo ""
	echo "把这份 \`SUMMARY.md\` 和需要细看的 \`.json\` 一起拷出来即可；\`.log\` 体积大，"
	echo "只在有 FAIL 时才需要连日志一起带。整个 \`results/\` 不入库。"
} >"$SUMMARY"

say ""
say "================================================================"
say "PASS $N_PASS · FAIL $N_FAIL · SKIP $N_SKIP（共 $TOTAL 项）"
say "汇总：$SUMMARY"
say "================================================================"

exit "$N_FAIL"
