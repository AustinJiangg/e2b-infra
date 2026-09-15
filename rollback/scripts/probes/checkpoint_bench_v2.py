#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Checkpoint/restore 性能测试 —— 宿主机 Firecracker + NBD 那一套（我们的实现）。

结构照搬那套的 demo_checkpoint_perf.py（920B 上 /home/phz/e2b-phz/e2b-script/）：
同一张档位表、Phase 1 建代 / Phase 2 恢复 / Phase 3 分段与产物、同样两张汇总表。
区别在"分段和产物从哪读"、内存负载怎么造，以及多出几项那套没有的判据。

对应关系：

    别人那套（VM 内 gsd）                我们这套（宿主机 Firecracker + NBD）
    ------------------------------      -------------------------------------
    journalctl -u gsd 里的 JSON          <store>/<sbx>/<ck>/timings.json
                                         <store>/<sbx>/last-restore-timings.json
    criu_dump_ms                         snapshot        （内存那半）
    overlay_sink_ms                      seal            （文件那半：封层）
    kill_ms + criu_restore_ms            save_live_bitmap + materialize + fc_rollback
    overlay_restore_ms                   assemble_view + reset_view
    total_ms                             frozen          （虚机真正停住的窗口）
    du -sh /var/lib/gsd/snapshots/*_mem  mem_diff / mem_full 的 st_blocks
    （无）                               layers/ 里新出现的封层文件的 st_blocks

我们这套多出来的三项判据，那套没有对应物：

  · 脏页后端。HDBSS（硬件标脏）/ kvm-wp（软件写保护）/ off，直接问沙箱的
    Firecracker API 套接字。同样的代码在这三种后端上能差好几倍，数字不带
    条件就没法跟另一次跑比。
  · 产物文件系统。从 orchestrator 进程实际拿到的 environ 读，不读配置文件
    —— 配置只说"本该是什么"，进程才知道"实际是什么"。
  · mem_mode。增量档被服务端报成 full，就是这套东西的静默失效模式：脏页
    跟踪没开，每次 checkpoint 悄悄整份拷内存，测试全过但测的根本不是增量。

相对那套改掉的两处（都是会让数字失真或让校验失效的，不是风格问题）：

  一、内存负载改成 dd 原地覆写 /dev/shm，不再用 shell 变量。

      那套用 `eval "mem_X=\"$(head -c NMB /dev/zero | tr \\0 x)\""` 造内存负载。
      这一句实际弄脏的远不止 N MB：命令替换先缓冲一份、eval 赋值再复制一份、
      缓冲区增长过程中 realloc 的中间态、内核给每块新映射清零、上一代释放后
      重新缺页 —— 950 实测放大约 5 倍（档位 192+64=256 MB，mem_diff 1087 MB）。
      档位标签就此名不副实：它标的是"请求改多少"，量到的是"实际脏多少"。

      我们这套 Firecracker 快照整个 guest RAM，tmpfs 页就是 guest RAM，所以
      可以照 checkpoint_bench.py 的办法来：预热代一次把 /dev/shm 上的文件撑到
      最大档（分配 + 首次写满的成本落在预热，不落在任何档位），之后每档
      `dd conv=notrunc` 只覆写前 N MB —— 不申请、不清零、不复制，脏 N MB 就是
      N MB。CRIU 那套换不了这个办法（它搬的是进程地址空间，dd 出来的 tmpfs
      文件不在任何被 dump 进程的地址空间里），所以那边只能改成"进程持有一块
      预分配 buffer、原地覆写前 N MB"。

  二、恢复后内存和文件分别校验。那套只读回 note.txt 比内容 —— 内存快照就算
      完全没生效、只有磁盘那半回滚了，检查照样打勾。这里内存代号写 /dev/shm
      （tmpfs 是纯内存，不落块设备，只有内存快照能把它带回来），文件代号写
      根文件系统，两个分开验，哪一半没回来一眼能看出。

必须跑在宿主机上：分段和产物那几列要 stat 宿主机上的文件、读 /proc。不在
宿主机上也能跑，只是那几列全空。

依赖（与 rollback/scripts 下其它脚本一致）:
    pip install e2b==2.20.0 python-dotenv
    python /opt/e2b-infra/patch_e2b.py
    python /opt/e2b-infra/dep/e2b-sdk-checkpoint/install.py

环境变量放当前目录的 .env：E2B_API_KEY / E2B_DOMAIN / E2B_API_URL / E2B_HTTP_SSL
"""
import glob
import json
import logging
import os
import socket
import sys
import tempfile
import time

from e2b import Sandbox
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
logging.getLogger("e2b").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
LOG = logging.getLogger("perf")

FILE_DIR = "/bench-root"              # 文件那份写这里（根文件系统上，进 NBD 写层）
NOTE = FILE_DIR + "/blob"
FILE_GEN = FILE_DIR + "/gen"          # 文件侧代号：只有换盘视图能带回来
MEM_GEN = "/dev/shm/gen"              # 内存侧代号：tmpfs 是纯内存，只有内存快照能带回来
SWEEP = "/dev/shm/sweep"              # 内存那份：tmpfs 是纯内存，不落块设备

# 诊断开关，默认关。只用来定位「全量之后紧接的第一次增量为什么慢」：
#
#   SETTLE_AFTER_FULL=1 python checkpoint_bench_v2.py
#
# 开了之后，cp0 与 cp1 之间把宿主的脏页冲干净再走。cp0 是 2 GB 全量，它的回写
# 要刷好一阵；checkpoint 路径上有三次 fsync（snapfile、位图 sidecar、mem_diff），
# 在 ext4 上 fsync 要等同一事务里别人的 ordered 数据落盘，所以 cp1 会排在 cp0
# 后面。开与不开各跑一次，cp1 的差值就是这笔排队。
#
# **不要默认打开**：开了量的是静默宿主上的成本，而用户在全量之后紧接着做增量时
# 真的要付那一下。默认关着，cp1 那一行才是诚实的。
SETTLE_AFTER_FULL = os.environ.get("SETTLE_AFTER_FULL", "") not in ("", "0", "false", "no")

EXPERIMENTS = [
    {"mem_mb": 0,   "file_mb": 0},
    {"mem_mb": 12,  "file_mb": 4},
    {"mem_mb": 24,  "file_mb": 8},
    {"mem_mb": 48,  "file_mb": 16},
    {"mem_mb": 96,  "file_mb": 32},
    {"mem_mb": 192, "file_mb": 64},
]

# 分段列的映射（key 名取自 checkpoint/service.go 与 sandbox 侧的 Mark/Timed）
CK_MEM = ["snapshot"]
CK_DISK = ["seal"]
# restore 的内存那半不能只算 fc_rollback：差分树方案必须先把回滚集从各代差分里
# 物化出来，这一步常比搬运本身还贵（实测 materialize 22.4ms vs fc_rollback
# 15.9ms）。只算 fc_rollback 会让三分之一的冻结窗口对不上账。
RS_MEM = ["save_live_bitmap", "materialize", "fc_rollback"]
RS_DISK = ["assemble_view", "reset_view"]

BACKEND_NOTE = {
    "hdbss": "✓ 硬件标脏（HDBSS）——CPU 自己记脏页，不用为每个干净页的第一次写陷出虚机",
    "kvm-wp": "软件写保护（KVM write-protect）——每页第一次被写都要陷出一次，下面的数字含这笔开销",
    "off": "✗ 没开脏页跟踪——每次 checkpoint 都会退化成整份内存拷贝，查 FC_TRACK_DIRTY_PAGES",
}


# ------------------------------------------------ 宿主机侧探测（本次跑在什么上）

def fc_get(sandbox_id, path="/"):
    """向这个沙箱的 Firecracker API 套接字发一个 GET。套接字是本地文件，所以只有
    在宿主机上跑才拿得到；远程跑返回 None，后端就标"未知"。"""
    hits = glob.glob(os.path.join(tempfile.gettempdir(), "fc-%s-*.sock" % sandbox_id))
    if not hits:
        return None
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(5)
        s.connect(hits[0])
        s.sendall(("GET %s HTTP/1.1\r\nHost: localhost\r\n"
                   "Accept: application/json\r\n\r\n" % path).encode())
        # Firecracker 走 keep-alive 不主动关连接，按 Content-Length 读够就停，
        # 等 EOF 会一直挂着。
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                return None
            buf += chunk
        head, body = buf.split(b"\r\n\r\n", 1)
        length = 0
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":", 1)[1])
        while len(body) < length:
            chunk = s.recv(65536)
            if not chunk:
                break
            body += chunk
        return json.loads(body)
    except (OSError, ValueError):
        return None
    finally:
        s.close()


def checkpoint_store():
    """checkpoint 产物落在哪个目录、那个目录是什么文件系统。

    路径读 orchestrator 进程实际拿到的 ORCHESTRATOR_BASE_PATH —— 读配置文件会
    告诉你"本该是什么"，读进程才知道"实际是什么"，这两者分岔过不止一次。"""
    env = {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            base = os.path.basename(os.path.realpath("/proc/%s/exe" % pid))
            raw = open("/proc/%s/environ" % pid, "rb").read().decode("utf8", "replace")
        except OSError:
            continue
        if not (base.startswith("orchestrator") or base.startswith("template-manager")
                or "ORCHESTRATOR_SERVICES=" in raw):
            continue
        env = dict(kv.split("=", 1) for kv in raw.split("\0") if "=" in kv)
        break
    if not env:
        return None, "?"
    store = os.path.join(env.get("ORCHESTRATOR_BASE_PATH", "/orchestrator"),
                         "build", "checkpoints")
    best, fstype = "", "?"
    try:
        for line in open("/proc/mounts"):
            f = line.split()
            if len(f) < 3:
                continue
            mnt = f[1]
            if (store == mnt or store.startswith(mnt.rstrip("/") + "/")) and len(mnt) > len(best):
                best, fstype = mnt, f[2]
    except OSError:
        pass
    return store, fstype


# ---------------------------------------------------------------- 产物与分段

def alloc(path):
    """文件的磁盘实占（字节）。稀疏文件按 st_blocks 算 —— 表观大小是整个地址
    空间，实占才是真写下去的量。"""
    try:
        return os.stat(path).st_blocks * 512
    except OSError:
        return None


def read_json(path):
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return {}


def layers_of(store, sbx_id):
    try:
        return set(os.listdir(os.path.join(store, sbx_id, "layers")))
    except (OSError, TypeError):
        return set()


def measure(store, sbx_id, ck_id, before_layers):
    """这一代实际写下了多少。

    内存：ext4 差分树轨每代一个 mem_diff（只装脏页）；XFS 轨每代存完整镜像，
    文件叫 mem_full。两个都试，报出用的是哪个 —— 拿 mem_full 当"增量"读会
    得出完全错误的结论，所以名字必须打出来。

    文件：建代前后 layers/ 目录的差集，就是这一代封的层。"""
    if not store:
        return None, "-", None
    d = os.path.join(store, sbx_id, ck_id)
    mem, kind = None, "-"
    for name in ("mem_diff", "mem_full"):
        v = alloc(os.path.join(d, name))
        if v is not None:
            mem, kind = v, name
            break
    disk = 0
    for name in layers_of(store, sbx_id) - before_layers:
        disk += alloc(os.path.join(store, sbx_id, "layers", name)) or 0
    return mem, kind, disk


def total(tm, keys):
    """几个分段求和；一个都没有就返回 None（缺省，不是 0）。"""
    if not any(k in tm for k in keys):
        return None
    return sum(tm[k] for k in keys if k in tm)


def fmt_mb(n):
    return "-" if n is None else "%.1f" % (n / 1048576.0)


def fmt_ms(x):
    return "-" if x is None else "%.1f" % x


# ------------------------------------------------------------------- guest 侧

def run(sb, cmd, timeout=300):
    """一律以 root 跑：要写 /bench-root。"""
    return sb.commands.run(cmd, user="root", timeout=timeout).stdout


def warm(sb, mem_max_mb, file_max_mb):
    """预热：把两个文件一次撑到各自的最大档。

    分配 + 首次写满的成本落在这里，不计入任何档位。之后每档只覆写前 N MB，
    量到的才是"改了多少"而不是"分配了多少"。"""
    run(sb, "mkdir -p %s" % FILE_DIR)
    run(sb, "rm -f %s; dd if=/dev/urandom of=%s bs=1M count=%d 2>/dev/null"
        % (SWEEP, SWEEP, max(mem_max_mb, 1)), timeout=1800)
    run(sb, "dd if=/dev/urandom of=%s bs=1M count=%d 2>/dev/null; sync"
        % (NOTE, max(file_max_mb, 1)), timeout=1800)


def dirty_mem(sb, mem_mb):
    """内存那份：原地覆写 /dev/shm 上那个已经撑满的文件的前 N MB。

    conv=notrunc 是要害 —— 只覆写，不动文件大小。tmpfs 页已经驻留，所以没有
    分配、没有内核清零、没有复制：脏 N MB 就是 N MB。"""
    if mem_mb <= 0:
        return
    run(sb, "dd if=/dev/urandom of=%s bs=1M count=%d conv=notrunc 2>/dev/null"
        % (SWEEP, mem_mb), timeout=1800)


def write_file(sb, file_mb):
    """文件那份：原地覆写预热好的那个文件的前 N MB，与内存那份对称。

    在 guest 里生成，不走 SDK 上传 —— 上传要把几十兆推过 HTTP，慢，而落地路径
    一样是 envd 写 → guest 页缓存 → 虚拟盘，对测量没有区别。普通缓冲写 + sync，
    不用 O_DIRECT：真实用户不会绕过页缓存。"""
    if file_mb > 0:
        run(sb, "dd if=/dev/urandom of=%s bs=1M count=%d conv=notrunc 2>/dev/null; sync"
            % (NOTE, file_mb), timeout=1800)
    else:
        run(sb, "sync")
    return run(sb, "md5sum %s | cut -c1-12" % NOTE, timeout=600).strip()


def mark(sb, gen):
    """代号写两处，恢复后分开验：谁回来了、谁没回来。"""
    run(sb, "echo %s > %s" % (gen, MEM_GEN))                     # 内存侧（tmpfs）
    run(sb, "mkdir -p %s && echo %s > %s && sync" % (FILE_DIR, gen, FILE_GEN))  # 文件侧


def now_state(sb):
    out = run(sb, "echo mem=$(cat %s 2>/dev/null || echo MISSING)\n"
                  "echo file=$(cat %s 2>/dev/null || echo MISSING)\n"
                  "echo md5=$(md5sum %s 2>/dev/null | cut -c1-12)"
                  % (MEM_GEN, FILE_GEN, NOTE), timeout=600)
    d = {}
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            d[k.strip()] = v.strip()
    return d


# ----------------------------------------------------------------------- 主体

def main():
    sb = Sandbox.create(template="base")
    LOG.info("sandbox %s", sb.sandbox_id)

    info = fc_get(sb.sandbox_id) or {}
    backend = info.get("dirty_tracking", "?")
    store, fstype = checkpoint_store()
    LOG.info("脏页后端 : %s", BACKEND_NOTE.get(backend, "未知（拿不到 Firecracker 套接字——没跑在宿主机上？）"))
    if store:
        LOG.info("产物落盘 : %s   文件系统 = %s", store, fstype)
    else:
        LOG.info("产物落盘 : 未知（读不到 orchestrator 进程——没跑在宿主机上？）")
        LOG.info("           不在宿主机上也能跑，只是分段和产物那几列缺省。")

    cps = []              # (name, cp, mem_mb, file_mb, e2e, mem_mode, timings, memB, memkind, diskB)
    restores = []         # (frm, to, mem_mb, file_mb, e2e, timings, ok_mem, ok_file, ok_md5)
    memkinds = set()
    md5_of = {}           # 代名 -> 建那一代时大文件的 md5
    rc = 0

    try:
        mem_max = max(e["mem_mb"] for e in EXPERIMENTS)
        file_max = max(e["file_mb"] for e in EXPERIMENTS)
        free = run(sb, "echo shm=$(df -m --output=avail /dev/shm 2>/dev/null | tail -1)\n"
                       "echo root=$(df -m --output=avail / 2>/dev/null | tail -1)")
        LOG.info("余量检查 : %s（/dev/shm 需 %d MB，/ 需 %d MB）",
                 " ".join(free.split()), mem_max + 16, file_max + 16)
        LOG.info("预热     : /dev/shm 撑到 %d MB、%s 撑到 %d MB（这一步的成本不计入任何档位）",
                 mem_max, NOTE, file_max)
        warm(sb, mem_max, file_max)

        # ── Phase 1: Checkpoints ──
        LOG.info("====== Phase 1: Checkpoints ======")
        md5_of["cp0"] = write_file(sb, 0)
        mark(sb, "cp0")
        before = layers_of(store, sb.sandbox_id)
        t0 = time.time()
        cp0 = sb.checkpoint.create(name="cp0")
        e2e = (time.time() - t0) * 1000
        mm, kind, dd = measure(store, sb.sandbox_id, cp0.checkpoint_id, before)
        memkinds.add(kind)
        tm = read_json(os.path.join(store, sb.sandbox_id, cp0.checkpoint_id, "timings.json")) if store else {}
        mode = getattr(cp0, "mem_mode", None) or "?"
        LOG.info("cp0 (baseline):          E2E=%7.1fms  mem_mode=%s", e2e, mode)
        cps.append(("cp0", cp0, 0, 0, e2e, mode, tm, mm, kind, dd))

        if SETTLE_AFTER_FULL:
            LOG.info("settle   : sync + 歇 1 秒（SETTLE_AFTER_FULL=1，**诊断用**）"
                     " —— 之后各档量到的是静默宿主上的成本，不是用户实际要付的")
            os.system("sync")
            time.sleep(1.0)

        for i, exp in enumerate(EXPERIMENTS, 1):
            name = "cp%d" % i
            dirty_mem(sb, exp["mem_mb"])
            md5_of[name] = write_file(sb, exp["file_mb"])
            mark(sb, name)
            time.sleep(0.3)

            before = layers_of(store, sb.sandbox_id)
            t0 = time.time()
            cp = sb.checkpoint.create(name=name)
            e2e = (time.time() - t0) * 1000
            mm, kind, dd = measure(store, sb.sandbox_id, cp.checkpoint_id, before)
            memkinds.add(kind)
            tm = read_json(os.path.join(store, sb.sandbox_id, cp.checkpoint_id, "timings.json")) if store else {}
            mode = getattr(cp, "mem_mode", None) or "?"
            LOG.info("%s (%3dMB/%3dMB):    E2E=%7.1fms  mem_mode=%-11s snapshot=%sms seal=%sms",
                     name, exp["mem_mb"], exp["file_mb"], e2e, mode,
                     fmt_ms(total(tm, CK_MEM)), fmt_ms(total(tm, CK_DISK)))
            cps.append((name, cp, exp["mem_mb"], exp["file_mb"], e2e, mode, tm, mm, kind, dd))

        # ── Phase 2: Restores ──
        # 升序遍历，与 demo_checkpoint_perf.py 相同 —— 那套每一代都是独立全量
        # 镜像，恢复代价与顺序无关，所以它自然写成升序；这里照搬是为了两张表
        # 能逐行并排看。
        #
        # 但对差分树来说顺序有意义。cps 是 cp1..cpN，起点 cur=cpN（链尾），
        # 遍历 cps[1:] 即 cp2..cpN，于是实际跳法是：
        #
        #     cpN → cp2   第一跳，回滚，要撤销 cp3..cpN 全部代的并集 —— 整轮最贵
        #     cp2 → cp3   往后每一跳都是前滚，只重放目标那一代自己的脏页
        #     ...
        #     cpN-1 → cpN
        #
        # 注意 cp1 全程不作为目标（它是 0MB/0MB 那一档，只当链根用）。
        #
        # 一跳的代价取决于这一跳要撤销/重放的量，不是目标那一代自己有多大 ——
        # 第一跳尤其如此，它挂在 cp2 这一行上却撤销了整条链。所以下面把
        # from → to 打出来，别照着档位那两列读。
        #
        # 还有一点：前滚命中的是目标那一代自己刚写完的差分文件（解析一跳到底、
        # 大概率还在 page cache 里），回滚要穿到链根的全量镜像上散读。同样的
        # 页数，两个方向的读放大不一样，别拿这里的前滚数字当回滚的代价。
        LOG.info("====== Phase 2: Restores ======")
        cur = cps[-1][0]
        for name, cp, mem_mb, file_mb, _e, _m, _t, _mm, _k, _d in cps[1:]:
            run(sb, "echo MUTATED > %s; echo MUTATED > %s; : > %s; sync"
                % (MEM_GEN, FILE_GEN, NOTE))
            t0 = time.time()
            sb.checkpoint.restore(cp.checkpoint_id)
            e2e = (time.time() - t0) * 1000
            # last-restore-timings.json 每次 restore 覆写一遍，必须紧接着读
            tm = read_json(os.path.join(store, sb.sandbox_id, "last-restore-timings.json")) if store else {}
            time.sleep(0.3)

            st = now_state(sb)
            ok_mem = st.get("mem") == name
            ok_file = st.get("file") == name
            # 代号只有一页，证明"层换对了"；大文件的 md5 证明"内容真的整片回来了"。
            ok_md5 = st.get("md5") == md5_of.get(name)
            if not (ok_mem and ok_file and ok_md5):
                rc = 1
            LOG.info("restore %-4s → %-4s (%3dMB/%3dMB): E2E=%7.1fms  内存=%s 文件=%s  "
                     "mem=%sms disk=%sms frozen=%sms",
                     cur, name, mem_mb, file_mb, e2e,
                     "✓" if ok_mem else "✗",
                     ("✓" if ok_md5 else "✗md5") if ok_file else "✗",
                     fmt_ms(total(tm, RS_MEM)), fmt_ms(total(tm, RS_DISK)),
                     fmt_ms(tm.get("frozen")))
            restores.append((cur, name, mem_mb, file_mb, e2e, tm, ok_mem, ok_file, ok_md5))
            cur = name

    except Exception as e:
        LOG.error("FAILED: %s", e)
        import traceback
        traceback.print_exc()
        rc = 1
    finally:
        try:
            sb.kill()
        except Exception:
            pass

    # ── 汇总 ──
    print("\n" + "=" * 108)
    print("Checkpoint Performance Summary")
    print("=" * 108)
    print("%-6s %8s %9s %11s %11s %11s %10s %11s %12s  %s"
          % ("name", "mem(MB)", "file(MB)", "e2e(ms)", "snapshot", "seal", "frozen",
             "memArt(MB)", "diskArt(MB)", "mem_mode"))
    print("-" * 108)
    for name, _cp, mem, fmb, e2e, mode, tm, mm, kind, dd in cps:
        print("%-6s %8d %9d %11.1f %11s %11s %10s %11s %12s  %s"
              % (name, mem, fmb, e2e, fmt_ms(total(tm, CK_MEM)), fmt_ms(total(tm, CK_DISK)),
                 fmt_ms(tm.get("frozen")), fmt_mb(mm), fmt_mb(dd), mode))
    print("-" * 108)
    print("  snapshot + seal ≈ frozen（虚机真正停住的窗口）；e2e − frozen = 网络往返 +")
    print("  服务端没停虚机时干的活。实测两列是产物的磁盘实占（稀疏文件按 st_blocks 算）。")
    kinds = ", ".join(sorted(k for k in memkinds if k != "-")) or "无（没跑在宿主机上）"
    print("  内存产物取自：%s"
          "（mem_diff = ext4 差分树轨的按代增量；mem_full = XFS 轨的每代完整镜像）" % kinds)
    print("  名义两列是**请求改多少**，memArt/diskArt 是**服务端实际写下多少**。负载是")
    print("  dd conv=notrunc 原地覆写预热好的文件，不申请、不清零、不复制，所以两者应接近。")
    print("  文件写走的是普通缓冲写，先进 guest 页缓存再落盘，所以一份文件写在两列各记一次：")
    print("  预期 memArt ≈ 名义内存 + 名义文件 + 本底，diskArt ≈ 名义文件 + 本底。")
    print("  cp1（0 档）那一行两列都是**本底噪声** —— 两次 checkpoint 之间 envd/systemd 等")
    print("  自己弄脏的页，各档共有，比对时先减掉它。")
    print("  注意 memArt 记的是「这一页被写过」，与现在还归不归谁用无关：脏过又释放掉的内存")
    print("  照样要写进快照。所以进程 RSS 通常小于 memArt。")

    print("\n" + "=" * 108)
    print("Restore Performance Summary")
    print("=" * 108)
    print("%-14s %8s %9s %11s %11s %11s %10s %7s %8s"
          % ("hop", "mem(MB)", "file(MB)", "e2e(ms)", "memUndo", "viewSwap", "frozen", "mem_ok", "file_ok"))
    print("-" * 108)
    for frm, to, mem, fmb, e2e, tm, ok_mem, ok_file, ok_md5 in restores:
        print("%-14s %8d %9d %11.1f %11s %11s %10s %7s %8s"
              % ("%s → %s" % (frm, to), mem, fmb, e2e,
                 fmt_ms(total(tm, RS_MEM)), fmt_ms(total(tm, RS_DISK)),
                 fmt_ms(tm.get("frozen")),
                 "✓" if ok_mem else "✗",
                 ("✓" if ok_md5 else "✗md5") if ok_file else "✗"))
    print("-" * 108)
    print("  档位那两列是**目标那一代**的负载，不是这一跳要撤销的量 —— 一跳的代价取决于")
    print("  from → to 之间差了多少。第一跳是从链尾大幅回退，之后都是前滚。")
    print("  mem回滚 = save_live_bitmap + materialize + fc_rollback（物化常比搬运还贵）；")
    print("  换盘视图 = assemble_view + reset_view。两段都在 frozen 内。")
    print("  内存列验的是 /dev/shm 里的代号（tmpfs 是纯内存，只有内存快照能带回来），")
    print("  文件列验的是根文件系统里的代号 —— 哪一半没回来一眼能看出。")

    print()
    if backend == "kvm-wp":
        print("注意：本次跑在软件写保护上，不是硬件标脏，checkpoint 的数字含 VM exit 开销。")
    elif backend == "off":
        print("注意：脏页跟踪没开，checkpoint 会全部退化成全量。")
    bad_mode = [c[0] for c in cps[1:] if c[5] == "full"]
    if bad_mode:
        print("⚠ %s 被服务端报成 mem_mode=full —— 脏页跟踪没开，这组数字不是增量。"
              "查 template-manager 的 FC_TRACK_DIRTY_PAGES。" % ", ".join(bad_mode))
        rc = 1
    if not store:
        print("注意：没跑在宿主机上，分段和实测产物几列全空。")
    if rc == 0 and restores:
        print("每一跳的内存代号和文件代号都回到了目标代。")
    return rc


if __name__ == "__main__":
    sys.exit(main())
