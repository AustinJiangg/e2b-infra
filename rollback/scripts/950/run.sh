#!/usr/bin/env bash
# rollback/scripts/950/run.sh —— 950（鲲鹏 + HDBSS，RPM 部署）上的一站式测试入口。
#
#   bash run.sh smoke     部署完先跑这个，≤ 3 分钟，回答「这台机器的 checkpoint 能用吗」
#   bash run.sh func      功能与健壮性全量，≤ 40 分钟
#   bash run.sh perf      性能分档 + 长尾 + 并发，≤ 40 分钟
#   bash run.sh long      只打印长测清单与命令，不自动跑
#
# 每次跑出一个 results/<YYYYmmdd-HHMMSS>-<档>/，里面一份 SUMMARY.md、每项一份
# .log、脚本自己的 .json 产物。任一项 FAIL 则退出码非 0（= FAIL 项数）。
#
# 常用可选参数（都有能直接用的默认值，不需要占位符）：
#   --env-file F      客户端凭据的 dotenv；默认 $CRTEST_ENV_FILE，再默认 $DEPLOY/.env
#   --deploy-dir D    RPM 部署根，默认 /opt/e2b-infra
#   --python P        跑脚本的解释器，默认 $CRTEST_PY，再默认 python3
#   --results-dir D   结果根目录，默认 <本目录>/results
#   --template T      模板 id，默认 base
#   --sdk-install P   SDK 覆盖层 install.py 的路径，默认 $DEPLOY/dep/e2b-sdk-checkpoint/install.py
#   --store S         checkpoint 产物目录（预检用），默认从 orchestrator 环境自动取
#   --keep-going      有 FAIL 也把剩下的项跑完（默认也是跑完，这里保留给将来改默认）
#   --allow-restart   func 档把 T18 也跑了（会重启 orchestrator，全机沙箱都会没）
#                     必须同时给 --restart-cmd 或设 CRTEST_RESTART_CMD
#   --restart-cmd C   T18 用的重启命令
#   --fault-cases     func 档把 T21/T38 也跑了（要求服务端带注入 env / 带注入特性的 FC）
#   --quota-cases     func 档把 T37 也跑了（要求服务端带配额 env）
#   -n N              perf 档串行 restore 的次数，默认 100
#   --quick           perf 档只做「短试跑」：验证三个脚本跑得通，不是正式采数
#                     （2 个档各 n=5、串行长尾 n=10、并发只 A 段两个 N），≤ 10 分钟
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

die() { echo "错误：$*" >&2; exit 2; }

while [ $# -gt 0 ]; do
	case "$1" in
	smoke | func | perf | long) TIER=$1; shift ;;
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
	-h | --help) sed -n '2,40p' "$0"; exit 0 ;;
	*) die "未知参数 $1（-h 看用法）" ;;
	esac
done
[ -n "$TIER" ] || die "要给一个档：smoke / func / perf / long"

[ -n "$ENV_FILE" ] || ENV_FILE="$DEPLOY/.env"
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
	say ""
	say "== $name =="
	say "   \$ $*"
	local t0 rc t1
	t0=$(date +%s)
	timeout --signal=INT --kill-after=60 "$tmo" "$@" >"$log" 2>&1
	rc=$?
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
key_serial() { grep -a '客户端墙钟' "$1" | tail -1 | cut -c1-160; }
key_compliance() { grep -a '^\*\*' "$1" | tail -1 | cut -c1-160; }

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

	# checkpoint capabilities：orchestrator 启动时打的那一行，说明这台机器的
	# 脏页跟踪开没开、为什么开。hdbss / kvm-wp 这个更细的后端名来自 Firecracker
	# 自己（FC API 的 dirty_tracking 字段），由下面的 checkpoint_verify.py 打印。
	CAPLOG="$OUT/capabilities.log"
	{
		echo "# orchestrator / template-manager 启动时的 checkpoint capabilities 行"
		found=0
		for d in "${NOMAD_DATA_DIR:-/data/nomad}"/alloc/*/alloc/logs; do
			[ -d "$d" ] || continue
			for f in "$d"/start.stdout.*; do
				[ -r "$f" ] || continue
				if line=$(timeout 60 grep -a 'checkpoint capabilities' "$f" 2>/dev/null | tail -1) &&
					[ -n "$line" ]; then
					echo "## $f"
					echo "$line"
					found=1
				fi
			done
		done
		if [ "$found" = 0 ]; then
			echo "## journald"
			timeout 60 journalctl -u orchestrator --no-pager 2>/dev/null |
				grep -a 'checkpoint capabilities' | tail -1
		fi
	} >"$CAPLOG" 2>&1
	if grep -aq 'track_dirty_pages' "$CAPLOG"; then
		N_PASS=$((N_PASS + 1))
		say ""
		say "== checkpoint capabilities =="
		sed 's/^/   /' "$CAPLOG" | tee -a "$RUNLOG" >&3
		row "checkpoint capabilities 日志行" "PASS" "-" "$(key_cap "$CAPLOG")" "capabilities.log"
	else
		N_SKIP=$((N_SKIP + 1))
		say "  SKIP  checkpoint capabilities 日志行 —— 在 alloc 日志与 journald 里都没抓到"
		row "checkpoint capabilities 日志行" "SKIP" "-" "日志里没抓到（换个日志源手工搜）" "capabilities.log"
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
esac

# ---------------------------------------------------------------- SUMMARY.md
TOTAL=$((N_PASS + N_FAIL + N_SKIP))
{
	echo "# $TIER 档结果 · $TS"
	echo ""
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
