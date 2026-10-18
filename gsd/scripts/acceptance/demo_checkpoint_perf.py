#!/usr/bin/env python3
"""按实际负载大小分档的进程级 checkpoint / restore 性能测试。

每档新建沙箱，只分配本档的负载内存；建立全量基点，改写本档负载，
测增量 create，再测 restore 基点。恢复后校验全部负载的 SHA256 和文件内容。
0 档仍有 Python 运行时与通信开销，不表示没有进程或没有后台改动。

在宿主机上跑（解释器要能 import 到带 checkpoint 接口的 SDK：PYTHONPATH=<代码仓>/py-sdk；凭据由本目录或上一级目录的 e2b_env.py 读取）：
    python demo_checkpoint_perf.py
没有命令行参数，用环境变量调：
    TEMPLATE   模板名，默认 base-gsd
    THP        guest 的透明大页设置，默认 never（gsd 的真实条件）；always 只用于诊断
    OUT_JSON   设了就把结果另存成这个 JSON 文件
输出分档表、实际 RSS、客户端墙钟和服务端分段。每档单样本，建议连跑三次。
JSON schema_version=2，measurement_mode=per-tier-size；与手册 20 章的历史固定
192 MiB、连续建链测试不是同一口径。失败返回非零，部分结果仍写入 OUT_JSON。
"""
import json
import os
import logging
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
logging.getLogger("e2b").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
LOG = logging.getLogger("perf")

NOTE = "/home/user-sandbox/note.txt"
CMD_FILE = "/home/user-sandbox/memhog.cmd"
RSP_FILE = "/home/user-sandbox/memhog.rsp"
PID_FILE = "/home/user-sandbox/memhog.pid"

EXPERIMENTS = [
    {"mem_mb": 0, "file_kb": 0},
    {"mem_mb": 12, "file_kb": 4 * 1024},
    {"mem_mb": 24, "file_kb": 8 * 1024},
    {"mem_mb": 48, "file_kb": 16 * 1024},
    {"mem_mb": 96, "file_kb": 32 * 1024},
    {"mem_mb": 192, "file_kb": 64 * 1024},
]

TEMPLATE = os.environ.get("TEMPLATE", "base-gsd")   # 模板名；手册统一用 base-gsd
THP = os.environ.get("THP", "never")   # 见 main() 里的说明；只在诊断时改成 always

# 常驻 Python 进程：
# - 预分配本档大小的 bytearray，0 档不强制分配 1 MiB
# - 用预先打开的文件接收命令（handler 不调用 open()）
# - signal handler 不重新分配大块内存，不调用 open()
# - dirty N: 覆写前 N MB（内容由 nonce 的 MD5 决定）
RESIDENT_PY = r'''
import hashlib, json, os, sys, signal

CMD, RSP, PID = "/home/user-sandbox/memhog.cmd", "/home/user-sandbox/memhog.rsp", "/home/user-sandbox/memhog.pid"
size = int(sys.argv[1]) << 20  # MB -> bytes
buf = bytearray(size)
for i in range(0, size, 4096):  # 碰实所有页
    buf[i] = 1
marker = "init"
# 1 MB 写块只在启动时分配一次。之前每次 dirty 都用 md5(...) * 65536 现造一个 1 MB 的 bytes，
# 那是一次 mmap/munmap；新建或合并出来的 VMA 会带 VM_SOFTDIRTY，pagemap 把整个 VMA 报成脏，
# 下一次增量 dump 就退化成全量（现场数据 cp1 那一行 143.9 ms 就是这么来的）。
# 之后只原地改内容：临时对象控制在 64 KB 以内，低于 glibc 的 mmap 阈值，不产生新 VMA。
chunk = bytearray(min(size, 1 << 20))

# 预创建文件并保持 fd 打开，handler 里不调 open()
for f in (CMD, RSP):
    try: os.unlink(f)
    except OSError: pass
    open(f, "w").close()
cmd_fd = os.open(CMD, os.O_RDWR)
rsp_fd = os.open(RSP, os.O_RDWR)

def handle_cmd(signum, frame):
    global marker
    try:
        os.lseek(cmd_fd, 0, 0)
        raw = os.read(cmd_fd, 4096)
        cmd = raw.split(b"\x00")[0].decode().strip()
        f = (cmd.split() + ["", ""])[:3]
        nonce, op, arg = f[0], f[1], f[2]
        if op == "dirty":
            n = int(arg)
            if n < 0 or (n << 20) > size:
                raise ValueError("dirty exceeds payload")
            if n:
                blk = hashlib.md5(nonce.encode()).digest() * 4096
                for j in range(16):
                    chunk[j << 16:(j + 1) << 16] = blk
            for i in range(n):
                buf[i << 20:(i + 1) << 20] = chunk
            out = "ok:%d" % n
        elif op == "state":
            out = json.dumps({"payload_bytes": len(buf),
                              "sha256": hashlib.sha256(buf).hexdigest(),
                              "marker": marker})
        elif op == "marker":
            marker = arg
            out = "ok"
        elif op == "get":
            out = marker
        elif op == "ping":
            out = "pong"
        else:
            out = "BADOP"
    except Exception as e:
        out = "ERR:%s" % e
    resp = ("%s %s\n" % (nonce, out)).encode()
    os.lseek(rsp_fd, 0, 0)
    os.write(rsp_fd, resp)
    os.ftruncate(rsp_fd, len(resp))  # 清除残留

signal.signal(signal.SIGUSR1, handle_cmd)
open(PID, "w").write(str(os.getpid()))

while True:
    signal.pause()  # 睡眠等待信号，不产生文件 I/O，不弄脏页
'''

class MemHog:
    def __init__(self, sb):
        self.sb = sb
        self.nonce = 0
        self.pid = None

    def ask(self, op, arg="", tries=600):
        self.nonce += 1
        n = "n%d" % self.nonce
        cmd = "%s %s %s" % (n, op, arg)
        # 写命令到文件（shell 内建，不 spawn python）
        self.sb.commands.run(
            'echo -n "%s" > %s' % (cmd, CMD_FILE),
            user="root", timeout=5)
        # 发信号
        self.sb.commands.run("kill -USR1 %s 2>/dev/null; true" % self.pid, user="root", timeout=5)
        # 读响应
        for _ in range(tries):
            r = self.sb.commands.run('cat %s 2>/dev/null; true' % RSP_FILE, user="root", timeout=5)
            out = r.stdout.strip()
            if out.startswith(n + " "):
                return out[len(n) + 1:]
            time.sleep(0.1)
        return "TIMEOUT"

    def dirty(self, mb):
        return self.ask("dirty", str(mb))

    def marker(self, gen):
        return self.ask("marker", gen)

    def get_marker(self):
        return self.ask("get")

    def ping(self):
        return self.ask("ping")

    def state(self):
        return json.loads(self.ask("state"))

    def start(self, payload_mb):
        self.sb.commands.run(f"rm -f {CMD_FILE} {RSP_FILE} {PID_FILE}", user="root")
        self.sb.files.write("/home/user-sandbox/memhog.py", RESIDENT_PY)
        self.sb.commands.run(f"python3 /home/user-sandbox/memhog.py {payload_mb}",
                             background=True)
        for _ in range(60):
            try:
                r = self.sb.commands.run(f"cat {PID_FILE} 2>/dev/null || true", user="root", timeout=2)
                if r.stdout.strip():
                    self.pid = r.stdout.strip()
                    break
            except Exception:
                pass
            time.sleep(0.2)
        if not self.pid:
            raise RuntimeError("resident process did not start")
        pong = self.ping()
        if pong != "pong":
            raise RuntimeError(f"ping failed: {pong}")
        LOG.info("  常驻进程就绪: pid=%s, 负载=%d MiB", self.pid, payload_mb)


def gsd_timing_for(sb, ckpt_id, operation="create", n=200, tries=20):
    """按 checkpoint 和操作类型取分段；RPC 返回时 journal 可能尚未可读。

    有界重试发生在计时窗口之外。restore 的目标也曾被 create，只有匹配
    操作的完成日志才能提供 total_ms，防止把旧 create 耗时配给 restore。
    """
    stage = "criu_restore_ms" if operation == "restore" else "criu_dump_ms"
    completion = "restore completed" if operation == "restore" else "checkpoint created"
    keys = (("overlay_restore_ms", "criu_restore_ms") if operation == "restore" else
            ("find_pids_ms", "criu_dump_ms", "overlay_sink_ms", "stopped_ms"))
    out = {}
    for attempt in range(tries):
        r = sb.commands.run(
            f"journalctl -u gsd --no-pager -o cat -n {n} 2>/dev/null", user="root", timeout=10)
        out, pending_kill, pending_find, complete = {}, None, None, False
        for line in r.stdout.splitlines():
            match = re.search(r'\{.*\}', line)
            if not match:
                continue
            try:
                entry = json.loads(match.group())
            except json.JSONDecodeError:
                continue
            if operation == "restore" and "kill_ms" in entry:
                pending_kill = entry["kill_ms"]
            if operation == "restore" and "find_pids_ms" in entry and "checkpoint_id" not in entry:
                pending_find = entry["find_pids_ms"]
            if entry.get("checkpoint_id") != ckpt_id:
                continue
            if entry.get("message") == "starting restore" and operation == "restore":
                out, pending_kill, pending_find, complete = {}, None, None, False
            for key in keys:
                if key in entry:
                    out[key] = out.get(key, 0) + entry[key] if key == stage else entry[key]
            if stage in entry and operation == "restore":
                if pending_kill is not None:
                    out["kill_ms"] = pending_kill
                if pending_find is not None:
                    out["find_pids_ms"] = pending_find
            if entry.get("message") == completion and "total_ms" in entry:
                out["total_ms"] = entry["total_ms"]
                complete = True
        if complete and stage in out:
            return out
        if attempt + 1 < tries:
            time.sleep(0.1)
    return out


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def memory_usage(sb, pid):
    """测量在计时窗口之外；RSS 包含解释器、负载与通信等全部驻留内存。"""
    text = sb.commands.run(f"cat /proc/{pid}/status", user="root", timeout=10).stdout
    fields = dict(re.findall(r"^(VmRSS|VmSize):\s+(\d+)\s+kB", text, re.M))
    require(set(fields) == {"VmRSS", "VmSize"}, "无法读取进程 RSS / VmSize")
    return {"rss_bytes": int(fields["VmRSS"]) * 1024,
            "vms_bytes": int(fields["VmSize"]) * 1024}


def create_record(sb, name, kind, exp):
    t0 = time.perf_counter()
    cp = sb.checkpoint.create(name=name)
    elapsed = (time.perf_counter() - t0) * 1000
    row = {"name": name, "kind": kind, **exp, "payload_mem_mb": exp["mem_mb"],
           "checkpoint_id": cp.checkpoint_id, "e2e_ms": elapsed,
           "sdk": dict(getattr(cp, "timings", None) or {}),
           "journal": gsd_timing_for(sb, cp.checkpoint_id),
           "meta": {k: getattr(cp, k, None) for k in
                    ("has_process_image", "parent_checkpoint_id", "image_bytes", "clear_refs_failed")}}
    LOG.info("%s %s: E2E=%.1f ms", name, kind, elapsed)
    return row


def run_tier(factory, index, exp, result):
    """每档只有本档大小的进程与独立的基点，没有跨档增量链或恢复。"""
    name = f"cp{index}"
    sb = factory(template=TEMPLATE)
    try:
        sb.commands.run(f"echo {THP} > /sys/kernel/mm/transparent_hugepage/enabled", user="root")
        hog = MemHog(sb)
        hog.start(exp["mem_mb"])
        require(hog.dirty(exp["mem_mb"]) == f"ok:{exp['mem_mb']}", f"{name}: 基点内存初始化失败")
        require(hog.marker(name + "-base") == "ok", f"{name}: 基点标记失败")
        sb.files.write(NOTE, "x" * (exp["file_kb"] * 1024))
        sb.commands.run("sync", user="root", timeout=60)
        expected = hog.state()
        require(expected["payload_bytes"] == exp["mem_mb"] * (1 << 20), f"{name}: 负载大小不符")
        usage = memory_usage(sb, hog.pid)
        LOG.info("%s: 负载=%d MiB, 文件=%d KiB, 实际 RSS=%.2f MiB",
                 name, exp["mem_mb"], exp["file_kb"], usage["rss_bytes"] / (1 << 20))

        # 首次全量单列；它是本档 restore 的目标，不混入增量 create 统计。
        base = create_record(sb, name + "-base", "full-baseline", exp)
        base.update(usage)
        result["baseline"].append(base)

        require(hog.dirty(exp["mem_mb"]) == f"ok:{exp['mem_mb']}", f"{name}: 改写内存失败")
        require(hog.marker(name + "-changed") == "ok", f"{name}: 改写标记失败")
        sb.files.write(NOTE, "y" * (exp["file_kb"] * 1024))
        sb.commands.run("sync", user="root", timeout=60)
        changed = hog.state()
        require(changed["payload_bytes"] == expected["payload_bytes"], f"{name}: 改写改变了负载大小")
        if exp["mem_mb"]:
            require(changed["sha256"] != expected["sha256"], f"{name}: 未实际改变内存内容")
        current_usage = memory_usage(sb, hog.pid)
        child = create_record(sb, name, "incremental", exp)
        child.update(current_usage)
        child["base_checkpoint_id"] = base["checkpoint_id"]
        result["create"].append(child)
        require(child["meta"]["parent_checkpoint_id"] == base["checkpoint_id"],
                f"{name}: checkpoint 未使用本档基点作增量父节点")

        # 拍子节点后立即回到本档基点：回滚的正是刚才制造的改动。
        t0 = time.perf_counter()
        sb.checkpoint.restore(base["checkpoint_id"])
        elapsed = (time.perf_counter() - t0) * 1000
        sdk = dict(getattr(sb.checkpoint, "last_timings", None) or {})
        row = {"name": name, **exp, "payload_mem_mb": exp["mem_mb"],
               "target_checkpoint_id": base["checkpoint_id"],
               "source_checkpoint_id": child["checkpoint_id"],
               "first_restore": True, "e2e_ms": elapsed, "sdk": sdk,
               "journal": gsd_timing_for(sb, base["checkpoint_id"], operation="restore"),
               "before_memory": changed, "expected_memory": expected,
               "before_usage": current_usage, "ok": False}
        result["restore"].append(row)
        actual = hog.state()
        note = sb.files.read(NOTE)
        row["actual_memory"] = actual
        row["mem_ok"] = actual == expected
        row["file_ok"] = len(note) == exp["file_kb"] * 1024 and all(c == "x" for c in note)
        row["after_usage"] = memory_usage(sb, hog.pid)
        row["ok"] = row["mem_ok"] and row["file_ok"]
        LOG.info("restore->%s-base: E2E=%.1f ms, 全量内存校验=%s, 文件校验=%s",
                 name, elapsed, row["mem_ok"], row["file_ok"])
        require(row["ok"], f"{name}: restore 后内存或文件内容不一致")
    finally:
        sb.kill()


def check_timings(result):
    """缺计时字段也算失败，避免空字典被解释为差值为零。"""
    maxdiff = 0.0
    for kind in ("baseline", "create", "restore"):
        for row in result[kind]:
            sdk, journal = row["sdk"], row["journal"]
            operation_key = "criu_restore_ms" if kind == "restore" else "criu_dump_ms"
            require(all(k in sdk and k in journal for k in ("total_ms", operation_key)),
                    f"{row['name']} {kind}: SDK / journal 缺少必要计时字段")
            for key in sdk.keys() & journal.keys():
                if key.endswith("_ms"):
                    maxdiff = max(maxdiff, abs(sdk[key] - journal[key]))
    result["max_sdk_journal_diff_ms"] = maxdiff
    require(maxdiff < 0.0005, f"SDK / journal 分段不一致: {maxdiff:.6f} ms")
    return maxdiff


def print_summary(result):
    print("\n分档口径: per-tier-size / schema_version=2；每档独立沙箱，恢复本档基点")
    print("0 档无额外负载内存，仍包含 Python 运行时、通信及后台活动。")
    print("单位: 内存 MiB / 文件 KiB / 耗时 ms；RSS 包含解释器等全部进程内存。")
    for kind, label in (("baseline", "首次全量（单列）"), ("create", "增量 checkpoint"), ("restore", "restore 基点")):
        print(f"\n{label}")
        print(f"{'档位':<12} {'负载':>8} {'文件':>8} {'RSS':>9} {'墙钟':>9} {'服务端':>9} {'CRIU':>9} {'kill':>9} {'切层':>9} {'内容校验':>8}")
        for row in result[kind]:
            usage = row.get("before_usage", row)
            server = row["sdk"].get("total_ms")
            server_text = "-" if server is None else f"{server:.1f}"
            criu_key = "criu_restore_ms" if kind == "restore" else "criu_dump_ms"
            overlay_key = "overlay_restore_ms" if kind == "restore" else "overlay_sink_ms"
            stages = ["-" if row["sdk"].get(key) is None else f"{row['sdk'][key]:.1f}"
                      for key in (criu_key, "kill_ms", overlay_key)]
            verdict = ("OK" if row["ok"] else "FAIL") if kind == "restore" else "-"
            print(f"{row['name']:<12} {row['payload_mem_mb']:>8} {row['file_kb']:>8} "
                  f"{usage['rss_bytes'] / (1 << 20):>9.2f} {row['e2e_ms']:>9.1f} {server_text:>9} "
                  f"{stages[0]:>9} {stages[1]:>9} {stages[2]:>9} {verdict:>8}")
    # 与既有验收入口保持一致；只有真正核对过字段才能输出这行。
    if "max_sdk_journal_diff_ms" in result:
        print(f"max |SDK - journal| = {result['max_sdk_journal_diff_ms']:.3f} ms")
    print("判定:", result["verdict"])


def main():
    from e2b_env import load_e2b_env
    os.environ.update(load_e2b_env())
    from e2b import Sandbox

    result = {"schema_version": 2, "measurement_mode": "per-tier-size",
              "template": TEMPLATE, "thp": THP,
              "restore_path": "modified-child-to-full-baseline",
              "samples_per_tier": 1, "baseline": [], "create": [], "restore": [],
              "verdict": "FAIL"}
    try:
        require(THP in ("never", "always", "madvise"), "THP 必须为 never、always 或 madvise")
        for index, exp in enumerate(EXPERIMENTS, 1):
            run_tier(Sandbox.create, index, exp, result)
        check_timings(result)
        result["verdict"] = "PASS"
    except Exception as exc:
        result["error"] = str(exc)
        LOG.exception("FAILED: 分档测试失败")
    finally:
        print_summary(result)
        out = os.environ.get("OUT_JSON")
        if out:
            with open(out, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
    return 0 if result["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
