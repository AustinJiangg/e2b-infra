#!/usr/bin/env python3
"""复现：restore 之后删除下层文件触发 guest 内核 BUG（fs/namei.c may_delete，经 ovl_cleanup_and_whiteout）。

现象最早出现在 verify_fixes 的 UMASK 项（criu restore 时 unlink 下层的 unix socket 文件）。这里不用 CRIU，只用 rm：
  bug   ：f1、f2 进下层（create c1）→ rm f1（建 whiteout，内核把它缓存在当前 workdir）→ restore c1（upper/work 被丢弃并删除）
          → 等 --wait 秒 → rm f2（再要一个 whiteout）
  ctl   ：对照。rm f1 之后先 create c2（whiteout 沉进保留的层，共享 inode 仍有链接）→ restore c2 → rm f2
  nodel ：对照。restore 之前没有删过下层文件（没有缓存的 whiteout）→ restore c1 → rm f2
  masked：判别用。最开始先删一个 base 镜像里的文件（/etc/legal），让第一个 whiteout 落在开机时的 upper 里（它会沉成 L1、
          被所有 checkpoint 的层栈保留），之后同 bug。预期不崩：共享 whiteout 的 inode 一直有链接。
  trap  ：另一个缺陷（work 目录的 trap inode 没有随切换释放）的确定性复现，不涉及 whiteout：两次 create（开机时的 workdir 被换下、它的 dentry 引用在
          第二次切换时放掉）→ drop_caches（该目录的 inode 被回收）→ 在 overlay 上新建 --trap-n 个目录和文件并逐个 stat/listdir/open。
          修复前：开机时给 workdir 建的 trap inode（以目录 inode 的地址为键）没有随切换释放，新建的 inode 复用到那个地址时，
          目录 lookup 得 ELOOP，普通文件 open 得 ENXIO，把它当路径分量用得 ENOTDIR。预期：错误计数为空。
每个沙箱开头还统计既存的 whiteout（upper/层目录里的 0:0 字符设备）和 dmesg 里的 whiteout 相关行。
每种情形起一个新沙箱；rm 用 8 秒超时，之后读 dmesg（读不到就算 guest 已不可用）。
在宿主机上跑（解释器要能 import 到带 checkpoint 接口的 SDK：PYTHONPATH=<代码仓>/py-sdk；凭据由本目录或上一级目录的 e2b_env.py 读取）：
    python probe_whiteout_bug.py <模板>[,<模板>...] [--cases bug,ctl,nodel] [--wait 1.0] [--dir /home/user]
--cases 默认 bug,ctl,nodel，可选的还有 masked、trap（同一个用例可以重复写，各起一个沙箱）。
判据：每个用例一行，crashed=False；trap 用例 errs={}。
"""
import argparse, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from e2b_env import load_e2b_env
os.environ.update(load_e2b_env())
from e2b import Sandbox

ap = argparse.ArgumentParser()
ap.add_argument("templates")
ap.add_argument("--wait", type=float, default=1.0)
ap.add_argument("--dir", default="/home/user")
ap.add_argument("--cases", default="bug,ctl,nodel")
ap.add_argument("--trap-n", type=int, default=6000)
args = ap.parse_args()
D = args.dir

TRAP_PY = r"""
import errno, os, sys
d, n = sys.argv[1], int(sys.argv[2])
errs = {}
def note(op, e):
    k = op + ":" + errno.errorcode.get(e.errno, str(e.errno)); errs[k] = errs.get(k, 0) + 1
def tryop(op, fn):
    try:
        fn()
    except OSError as e:
        note(op, e)
os.makedirs(d, exist_ok=True)
for i in range(n):
    sub = "%s/d%d" % (d, i); f = "%s/f%d" % (d, i)
    tryop("mkdir", lambda: os.mkdir(sub))
    tryop("create_in_dir", lambda: open(sub + "/x", "w").close())
    tryop("create", lambda: open(f, "w").close())
# 丢掉 overlay 的 dentry（下层 ext4 的 inode 被 overlay inode 的引用钉着，要丢两遍），让下面每一步都重新 lookup
for _ in range(2):
    os.sync()
    open("/proc/sys/vm/drop_caches", "w").write("2")
for i in range(n):
    sub = "%s/d%d" % (d, i); f = "%s/f%d" % (d, i)
    tryop("lstat_dir", lambda: os.lstat(sub))
    tryop("listdir", lambda: os.listdir(sub))
    tryop("lstat_in_dir", lambda: os.lstat(sub + "/x"))
    tryop("lstat_file", lambda: os.lstat(f))
    tryop("open", lambda: open(f).close())
print("n=%d errs=%s" % (n, dict(sorted(errs.items()))))
"""


def run(sb, cmd, timeout=8):
    try:
        r = sb.commands.run(cmd, user="root", timeout=timeout)
        return "ok", r.stdout.strip()
    except Exception as e:  # noqa: BLE001
        return type(e).__name__, str(e)[:200]


res = []
for tpl in args.templates.split(","):
    for case in args.cases.split(","):
        sb = Sandbox.create(template=tpl, timeout=120)
        row = {"template": tpl, "case": case, "sandbox": sb.sandbox_id, "wait": args.wait, "dir": D}
        try:
            row["env"] = run(sb, "/usr/bin/gsd -commit; uname -v; df %s | tail -1" % D)[1]
            row["whiteouts_at_start"] = run(sb, "echo upper=$(find /overlay/workspace -xdev -type c 2>/dev/null | wc -l) layers=$(find /overlay/layers -xdev -type c 2>/dev/null | wc -l); "
                                                "find /overlay/workspace /overlay/layers -xdev -type c 2>/dev/null | head -5 | tr '\\n' ' '; dmesg | grep -i whiteout | head -3")[1]
            if case == "masked":
                row["rm0"] = run(sb, "ls -la /etc/legal && rm /etc/legal && echo removed; find /overlay/workspace -xdev -type c | head -3")
            if case == "trap":
                sb.files.write("/dev/shm/trap.py", TRAP_PY)
                run(sb, f"mkdir -p {D}/wb && echo 1 > {D}/wb/f1 && sync")
                sb.checkpoint.create(name="t1")
                run(sb, f"echo 2 > {D}/wb/f2 && sync")
                sb.checkpoint.create(name="t2")
                row["drop"] = run(sb, "sync; echo 2 > /proc/sys/vm/drop_caches; echo dropped")
                row["trap"] = run(sb, f"python3 /dev/shm/trap.py {D}/wb/tr {args.trap_n}", timeout=120)
                st, dm = run(sb, "dmesg | grep -E 'kernel BUG|Oops|may_delete|whiteout|vfs_rename|ovl_|overlayfs' | head -12")
                row["dmesg"] = (st, dm)
                row["crashed"] = row["trap"][0] != "ok" or "errs={}" not in row["trap"][1] or "kernel BUG" in dm or st != "ok"
                print("%-12s %-6s crashed=%s trap=%s dmesg=%s" % (tpl, case, row["crashed"], str(row["trap"])[:300], str(dm)[:200]), file=sys.stderr, flush=True)
                res.append(row)
                continue
            run(sb, f"mkdir -p {D}/wb && echo 1 > {D}/wb/f1 && echo 2 > {D}/wb/f2 && sync")
            c1 = sb.checkpoint.create(name="c1")
            target = c1.checkpoint_id
            if case in ("bug", "ctl", "masked"):
                row["rm1"] = run(sb, f"rm {D}/wb/f1 && ls -la {D}/wb | tr '\\n' ';'")
            if case == "ctl":
                target = sb.checkpoint.create(name="c2").checkpoint_id
            row["layers_before"] = run(sb, "ls /overlay/layers | tr '\\n' ' '")[1]
            row["restore"] = sb.checkpoint.restore(target)
            time.sleep(args.wait)
            row["layers_after"] = run(sb, "ls -a /overlay/layers /overlay/layers/.trash 2>/dev/null | tr '\\n' ' '")[1]
            row["rm2"] = run(sb, f"rm {D}/wb/f2 && ls -la {D}/wb | tr '\\n' ';'")
            row["after"] = run(sb, f"ls {D}/wb | tr '\\n' ' '; echo ok > {D}/wb/new && cat {D}/wb/new")
            st, dm = run(sb, "dmesg | grep -E 'kernel BUG|Oops|may_delete|whiteout|vfs_rename|ovl_' | head -12")
            row["dmesg"] = (st, dm)
            row["crashed"] = (row["rm2"][0] != "ok") or ("kernel BUG" in dm) or st != "ok"
        except Exception as e:  # noqa: BLE001
            row["error"] = "%s: %s" % (type(e).__name__, str(e)[:300])
            row["crashed"] = True
        finally:
            try:
                sb.kill()
            except Exception:  # noqa: BLE001
                pass
        res.append(row)
        print("%-12s %-6s crashed=%s start=[%s] rm0=%s rm2=%s dmesg=%s" % (tpl, case, row.get("crashed"), row.get("whiteouts_at_start"), str(row.get("rm0"))[:160], str(row.get("rm2"))[:60], str(row.get("dmesg"))[:200]), file=sys.stderr, flush=True)
json.dump(res, sys.stdout, ensure_ascii=False, indent=1)
