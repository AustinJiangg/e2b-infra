#!/usr/bin/env python3
"""验证 dump 改用 --leave-stopped 之后 clear_refs 竞态不再出现，并可用修复前的 gsd 二进制做反向对照。

竞态（修复前）：CRIU --leave-running dump 完立刻放行进程 → gsd 做 overlay sink → 再写 clear_refs=4。
这段窗口里进程写的页被抹掉 soft-dirty，下一次增量 dump 漏掉它们，restore 后这些页是旧内容。

在宿主机上跑（解释器要能 import 到带 checkpoint 接口的 SDK：PYTHONPATH=<代码仓>/py-sdk；凭据由本目录或上一级目录的 e2b_env.py 读取）：
    python verify_clear_refs_race.py [--template base-gsd] [--rounds 20] [--pages 4096] [--rate 100000]
                                     [--gsd-binary /path/to/gsd] [--jsonl out.jsonl]

测试设计
  写入进程 writer（uid 1000，经 envd 后台启动。gsd 把每棵用户进程树各 dump 一次，树根是"自己是
  uid 1000、父进程不是用户进程"的进程；guest 里开机时没有其它 uid 1000 进程，writer 又是 exec 出来、
  直接由 envd 拉起的，所以它是唯一一棵树的根，脚本会用日志里的 dump_root_pids 核对）：私有匿名映射 N 页，按全局递增序号 s 写 page[s % N] 的前 8 字节 = s，
  按 --rate 页/秒匀速写（busy loop，只用 vDSO 的 perf_counter，无 sleep）。
  为什么限速：若全速写（~数百万页/秒），窗口里写过的页在下一次 dump 之前会被环形写入再覆盖很多遍，
  重新变脏，漏页被掩盖；限速保证"窗口里写的页"到下一次 dump 前不会被再写一遍（需 N > rate×(窗口+间隔)）。

  每轮：
    cpA（writer 在跑，竞态窗口就在这次 dump 之后）
    → 同一条 guest shell 里紧接着 kill -STOP writer（间隔只有几毫秒）
    → 从 /proc/<pid>/mem 读出整块缓冲区，记下每页的值（期望内容 M_stop，存 guest /dev/shm）
    → cpB（增量，父镜像是 cpA；writer 停着，所以 cpB 应当精确等于 M_stop）
    → restore cpB。CRIU 把 dump 时处于 stopped 的任务恢复成 stopped，因此恢复后 writer
      一条指令都没执行，/proc/<pid>/mem 读到的就是"恢复那一刻"的内容（脚本核对 /proc/<pid>/stat 为 T）
    → 逐页比对：restored != M_stop 的页 = 漏页（再细分为"比期望旧"的页）；另外按环形规律
      （S=max，页 j 期望 ≤S 且 ≡j mod N 的最大值）独立再算一遍。
    → kill -CONT，进入下一轮。
  漏页只可能来自 cpB 的增量没带上本该带的页（从父镜像 cpA 取了旧值）。

  每轮还从 gsd 日志（journalctl -u gsd -o cat）取 cpA/cpB 的 criu_dump_ms、overlay_sink_ms、
  stopped_ms、stopped_pids（修复后才有），以及 guest 里 /var/lib/gsd/snapshots/<id>_mem/pages-*.img 大小。
  默认不删旧 checkpoint：实测删掉两轮以前的镜像后 criu restore 会失败（增量链仍引用父目录），
  所以 --keep-rounds 默认 0；轮数多时留意 /var/lib/gsd/snapshots 的空间（脚本结尾打印 df/free）。
"""
import argparse, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from e2b_env import load_e2b_env
os.environ.update(load_e2b_env())
from e2b import Sandbox

ap = argparse.ArgumentParser()
ap.add_argument("--template", default="base-gsd")
ap.add_argument("--rounds", type=int, default=20)
ap.add_argument("--pages", type=int, default=4096, help="缓冲区页数 N（4 KiB/页）")
ap.add_argument("--rate", type=float, default=100000, help="写入速率，页/秒")
ap.add_argument("--gsd-binary", default="", help="把 guest 的 /usr/bin/gsd 换成这个宿主机上的二进制")
ap.add_argument("--keep-rounds", type=int, default=0, help=">0 时每轮结束删掉更早的 checkpoint（省根盘）；0 不删")
ap.add_argument("--jsonl", default="", help="每轮结果追加写入的 JSONL 文件")
args = ap.parse_args()

WRITER = r'''
import sys, mmap, ctypes, time
N = int(sys.argv[1]); RATE = float(sys.argv[2]); CAP = max(64, N // 8)
m = mmap.mmap(-1, N * 4096, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS)
addr = ctypes.addressof(ctypes.c_char.from_buffer(m))
mv = memoryview(m).cast("Q")
s = 0
for _ in range(2 * N):
    s += 1
    mv[(s % N) << 9] = s
open("/home/user/writer.ack", "w").write("%d %d %d" % (__import__("os").getpid(), addr, N))
pc = time.perf_counter
t0 = pc(); base = s
while True:
    target = base + int((pc() - t0) * RATE)
    if target - s > CAP:          # 被 SIGSTOP / checkpoint 停过：重新对齐，不做追赶式突发写
        t0 = pc(); base = s
        continue
    while s < target:
        s += 1
        mv[(s % N) << 9] = s
'''

# guest 里 root 跑的探针：snap 记下期望内容；check 比对恢复后的内容。python3.10，f-string 里不放反斜杠。
PROBE = r'''
import sys, json, hashlib
mode, pid, addr, N = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
path = sys.argv[5]
state = open("/proc/%d/stat" % pid).read().rsplit(")", 1)[1].split()[0]
with open("/proc/%d/mem" % pid, "rb", buffering=0) as f:
    f.seek(addr)
    data = f.read(N * 4096)
vals = [int.from_bytes(data[j * 4096:j * 4096 + 8], "little") for j in range(N)]
tail_nonzero = sum(1 for j in range(N) if any(data[j * 4096 + 8:(j + 1) * 4096]))
S = max(vals)
def expect(j):
    return S - ((S - j) % N)
ring_miss = [j for j in range(N) if vals[j] < expect(j)]
bad_mod = sum(1 for j in range(N) if vals[j] % N != j)
out = {"state": state, "S": S, "sha": hashlib.sha256(data).hexdigest()[:16],
       "ring_miss": len(ring_miss), "bad_mod": bad_mod, "tail_nonzero": tail_nonzero}
if mode == "snap":
    open(path, "wb").write(b"".join(v.to_bytes(8, "little") for v in vals))
else:
    raw = open(path, "rb").read()
    exp = [int.from_bytes(raw[j * 8:j * 8 + 8], "little") for j in range(N)]
    diff = [j for j in range(N) if vals[j] != exp[j]]
    older = [j for j in diff if vals[j] < exp[j]]
    out.update({"diff": len(diff), "older": len(older), "exp_S": max(exp),
                "lag_max": max([exp[j] - vals[j] for j in older], default=0),
                "diff_seq_min": min([exp[j] for j in diff], default=0),
                "diff_seq_max": max([exp[j] for j in diff], default=0)})
print(json.dumps(out))
'''

RPC = "curl -s -m 30 -u root: -H 'Content-Type: application/json' -d '%s' http://127.0.0.1:49984/checkpoint.Checkpoint/%s"


def log(msg):
    print(msg, flush=True)


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))] if xs else float("nan")


sb = Sandbox.create(template=args.template, timeout=3600)
# gsd 校验 X-Access-Token：secure 沙箱（SDK 默认）要带 envd 的 access token；反向对照用的修复前二进制忽略这个头。
_tok = getattr(sb, "_envd_access_token", None)
if _tok:
    RPC = RPC.replace("curl -s", "curl -s -H 'X-Access-Token: %s'" % _tok.replace("%", "%%"), 1)
jf = open(args.jsonl, "a") if args.jsonl else None


def run(cmd, timeout=90):
    return sb.commands.run(cmd, user="root", timeout=timeout).stdout


def gsd_events(ckpt):
    out = {}
    for line in run("journalctl -u gsd --no-pager -o cat | grep -F '%s' || true" % ckpt).splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            j = json.loads(line)
        except ValueError:
            continue
        for k in ("criu_dump_ms", "overlay_sink_ms", "stopped_ms", "stopped_pids", "total_ms",
                  "dump_root_pid", "restored_pid", "criu_restore_ms"):
            if k in j:
                out[k] = j[k]
        # 支持多棵进程树之后日志字段是 dump_root_pids（数组）；只有一棵树时折算成旧字段 dump_root_pid
        if isinstance(j.get("dump_root_pids"), list) and j.get("message") == "starting checkpoint":
            out["dump_root_pids"] = j["dump_root_pids"]
            if len(j["dump_root_pids"]) == 1:
                out["dump_root_pid"] = j["dump_root_pids"][0]
    return out


def pages_bytes(ckpt):
    # 新布局：每棵树的镜像在 <id>_mem/t<序号>/ 下；旧布局直接在 <id>_mem/ 下
    o = run("cat /var/lib/gsd/snapshots/%s_mem/pages-*.img /var/lib/gsd/snapshots/%s_mem/t*/pages-*.img 2>/dev/null | wc -c" % (ckpt, ckpt)).strip()
    return int(o or 0)


def create(name):
    r = run(RPC % (json.dumps({"name": name}), "CreateCheckpoint"))
    return json.loads(r)["checkpointId"]


try:
    log(f"[{args.template}] sandbox {sb.sandbox_id}  rounds={args.rounds} N={args.pages} rate={args.rate:.0f}/s")
    # criu -v4 的输出也进 gsd 的 journal，默认限流（10000 条/30 s）会在第 ~10 轮后把 gsd 的 JSON 日志一起丢掉。
    # 给 gsd.service 加 drop-in 关掉按单元限流，并重启 gsd（正向、反向都重启一次，条件对称）。
    # 另外 journal 默认写在 /var/log/journal（从 ext4 底层 bind 进来，不随 restore 回滚），criu 的详细输出会让它
    # 一直涨。改成 Storage=volatile（写 /run，tmpfs），长跑不占根盘。
    run("mkdir -p /etc/systemd/system/gsd.service.d /etc/systemd/journald.conf.d"
        " && printf '[Service]\\nLogRateLimitIntervalSec=0\\n' > /etc/systemd/system/gsd.service.d/norl.conf"
        " && printf '[Journal]\\nStorage=volatile\\nRateLimitIntervalSec=0\\nRuntimeMaxUse=512M\\n'"
        " > /etc/systemd/journald.conf.d/volatile.conf"
        " && systemctl daemon-reload && systemctl restart systemd-journald")
    if args.gsd_binary:
        data = open(args.gsd_binary, "rb").read()
        sb.files.write("/home/user/gsd.upload", data)
        run("chmod 755 /home/user/gsd.upload && systemctl stop gsd && cp /home/user/gsd.upload /usr/bin/gsd"
            " && rm -f /home/user/gsd.upload && systemctl start gsd")
        log(f"已替换 /usr/bin/gsd（{len(data)} B）")
    else:
        run("systemctl restart gsd")
    for _ in range(100):
        if run(RPC % ("{}", "ListCheckpoints") + " || true").strip().startswith("{"):
            break
        time.sleep(0.1)
    log(f"gsd LogRateLimitIntervalSec={run('systemctl show gsd -p LogRateLimitIntervalUSec').strip()}")
    commit = run("/usr/bin/gsd -commit").strip()
    log(f"gsd -commit : {commit}   sha256={run('sha256sum /usr/bin/gsd').split()[0][:16]}")
    log(f"kernel      : {run('uname -r').strip()}   criu: {run('criu --version | head -1').strip()}")
    run("echo never > /sys/kernel/mm/transparent_hugepage/enabled")
    uid1000 = run("ps -eo pid,uid,comm --no-headers | awk '$2==1000'").strip()
    log(f"启动 writer 前的 uid-1000 进程: {uid1000 or '(无)'}")

    sb.files.write("/home/user/writer.py", WRITER)
    run("mkdir -p /dev/shm/race && : > /dev/shm/race/writer.err && chown 1000:1000 /dev/shm/race/writer.err"
        " && cat > /dev/shm/race/probe.py <<'EOF'\n" + PROBE + "\nEOF")
    # exec：让 writer 自己就是 envd 拉起的那个进程（dump root）。stderr 进 /dev/shm（tmpfs，不随 overlay 回滚，
    # 文件在 writer 崩溃前一直是 0 字节，criu 的文件大小校验不受影响），用来抓 writer 崩溃时的 traceback。
    sb.commands.run(f"exec python3 /home/user/writer.py {args.pages} {args.rate} 2>>/dev/shm/race/writer.err",
                    background=True)
    ack = ""
    for _ in range(200):
        ack = run("cat /home/user/writer.ack 2>/dev/null || true").strip()
        if ack:
            break
        time.sleep(0.1)
    if not ack:
        raise SystemExit("writer 没起来: " + run("cat /dev/shm/race/writer.err; ls -la /dev/shm/race"))
    pid, addr, N = map(int, ack.split())
    log(f"writer pid={pid} buf=0x{addr:x} N={N} ({N * 4 // 1024} MiB)")

    cp0 = create("cp0")
    ev0 = gsd_events(cp0)
    log(f"cp0 {cp0}: dump_root_pid={ev0.get('dump_root_pid')} pages={pages_bytes(cp0) >> 20} MiB {ev0}")
    if ev0.get("dump_root_pid") != pid:
        raise SystemExit(f"dump_root_pid {ev0.get('dump_root_pid')} != writer pid {pid}，writer 不在 dump 的进程树里")

    rounds_ids = {0: [cp0]}
    results = []
    for k in range(1, args.rounds + 1):
        # cpA 与 STOP 在同一条 shell 里，尽量缩短 cpA 返回到 writer 停住的间隔
        r = run(RPC % (json.dumps({"name": f"A{k}"}), "CreateCheckpoint") +
                f" > /dev/shm/race/cpA.json; kill -STOP {pid}; cat /dev/shm/race/cpA.json")
        cpA = json.loads(r)["checkpointId"]
        snap = json.loads(run(f"python3 /dev/shm/race/probe.py snap {pid} {addr} {N} /dev/shm/race/exp.bin"))
        cpB = create(f"B{k}")
        rr = run(RPC % (json.dumps({"checkpoint_id": cpB}), "RestoreCheckpoint"))
        if "true" not in rr:
            log(run("journalctl -u gsd --no-pager -o cat | grep -v '^{' | grep -i -E 'error|fail|warn' | tail -20"))
            raise SystemExit(f"round {k}: restore 失败: {rr}")
        chk = json.loads(run(f"python3 /dev/shm/race/probe.py check {pid} {addr} {N} /dev/shm/race/exp.bin"))
        run(f"kill -CONT {pid}")
        eA, eB = gsd_events(cpA), gsd_events(cpB)
        rec = {"round": k, "commit": commit, "cpA": cpA, "cpB": cpB,
               "snap_state": snap["state"], "restored_state": chk["state"],
               "S": snap["S"], "diff": chk["diff"], "older": chk["older"], "ring_miss": chk["ring_miss"],
               "snap_ring_miss": snap["ring_miss"], "bad_mod": chk["bad_mod"], "lag_max": chk["lag_max"],
               "diff_seq": [chk["diff_seq_min"], chk["diff_seq_max"]],
               "sha_eq": snap["sha"] == chk["sha"],
               "A_dump_ms": eA.get("criu_dump_ms"), "A_sink_ms": eA.get("overlay_sink_ms"),
               "A_stopped_ms": eA.get("stopped_ms"), "A_stopped_pids": eA.get("stopped_pids"),
               "A_dump_root": eA.get("dump_root_pid"),
               "B_stopped_ms": eB.get("stopped_ms"),
               "restored_pid": eB.get("restored_pid"),
               "A_pages": pages_bytes(cpA), "B_pages": pages_bytes(cpB)}
        # 反向对照里：漏掉的页也可能是 writer 自己的 Python 堆（计数器、时间基准），恢复后 writer 可能卡死不再写，
        # 之后每轮内容都不变、漏页恒为 0，没有信息量。S 不再增长就判为"writer 已被破坏"，记下并结束本沙箱。
        # 正常一轮 S 增长约 3 万；增长不到 N 就视为卡死（实测反向对照里出现过只 +2 的情况）。
        rec["wedged"] = bool(results) and rec["S"] - results[-1]["S"] < N
        results.append(rec)
        if jf:
            jf.write(json.dumps(rec) + "\n"); jf.flush()
        log(f"round {k:2d}: 漏页 diff={rec['diff']:5d} (更旧 {rec['older']:5d}, 环形校验 {rec['ring_miss']:5d})"
            f"  S={rec['S']}  restored_state={rec['restored_state']} sha_eq={rec['sha_eq']}"
            f"  A: dump={rec['A_dump_ms']} sink={rec['A_sink_ms']} stopped={rec['A_stopped_ms']} pids={rec['A_stopped_pids']}"
            f"  pagesA={rec['A_pages'] >> 10}KiB pagesB={rec['B_pages'] >> 10}KiB")
        if rec["wedged"]:
            log(f"  !! S 增长不足 N（{results[-2]['S']} -> {rec['S']}）：writer 恢复后已不再写入"
                "（其 Python 状态页被漏掉），本轮起无信息量，停止")
            break
        if rec["restored_pid"] != pid or rec["A_dump_root"] != pid:
            log(f"  !! restored_pid={rec['restored_pid']} dump_root={rec['A_dump_root']} 与 writer pid {pid} 不一致")
        rounds_ids[k] = [cpA, cpB]
        for old in [i for i in rounds_ids if args.keep_rounds and i <= k - args.keep_rounds]:
            for c in rounds_ids.pop(old):
                run(RPC % (json.dumps({"checkpoint_id": c}), "DeleteCheckpoint"))
        time.sleep(0.2)
        alive = run(f"cat /proc/{pid}/stat 2>/dev/null | cut -d' ' -f3").strip()
        if alive not in ("R", "S", "T"):
            log(f"  !! writer 状态异常: '{alive}'（CONT 之后进程已退出），停止。dmesg 尾部:")
            log(run("dmesg | grep -v -E 'pagemap_read|clear_refs|ovl_ioctl' | tail -5"))
            log("writer stderr:" + chr(10) + run("tail -c 3000 /dev/shm/race/writer.err 2>/dev/null || true"))
            log(run("journalctl -u gsd --no-pager -o cat | grep -v '^{' | tail -8"))
            break

    wedged = [r for r in results if r.get("wedged")]
    results = [r for r in results if not r.get("wedged")]
    if wedged:
        log(f"（排除 writer 卡死后的 {len(wedged)} 轮）")
    diffs = [r["diff"] for r in results]
    log("")
    log(f"== 汇总 gsd {commit}  {len(results)} 轮  N={N} rate={args.rate:.0f}/s ==")
    log(f"每轮漏页: {diffs}")
    log(f"有漏页的轮数: {sum(1 for d in diffs if d)} / {len(diffs)}   漏页合计 {sum(diffs)}")
    for key in ("A_stopped_ms", "A_sink_ms", "A_dump_ms", "B_stopped_ms"):
        xs = [r[key] for r in results if isinstance(r.get(key), (int, float))]
        if xs:
            log(f"{key:13s}: p50={pct(xs, .5):.2f} min={min(xs):.2f} max={max(xs):.2f} (n={len(xs)})")
    bs = [r["B_pages"] for r in results]
    as_ = [r["A_pages"] for r in results]
    log(f"cpB 增量 pages: p50={pct(bs, .5) >> 10} KiB min={min(bs) >> 10} max={max(bs) >> 10}")
    log(f"cpA pages     : p50={pct(as_, .5) >> 10} KiB min={min(as_) >> 10} max={max(as_) >> 10}")
    log(f"df: {run('df -m / /var/lib/gsd/snapshots | tail -2').strip()}")
    log(f"free: {run('free -m | sed -n 2p').strip()}")
    log("journald Suppressed 行数: " + run("journalctl --no-pager -o cat -u systemd-journald | grep -c Suppressed || true").strip())
    log("writer stderr 字节数: " + run("stat -c %s /dev/shm/race/writer.err 2>/dev/null || echo NA").strip())
finally:
    if jf:
        jf.close()
    sb.kill()
    log(f"sandbox {sb.sandbox_id} killed")
