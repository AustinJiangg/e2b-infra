#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Checkpoint/restore 性能测试 —— e2b 原生 snapshot 那一套（pause / create_snapshot）。

这是三套对比里的第三份，与另外两份同一张档位表、同样的负载造法、同样的
两张汇总表，方便逐行并排看：

    demo_checkpoint_perf.py     phz 那套：VM 内 gsd + CRIU，进程级
    checkpoint_bench_v2.py      我们那套：宿主机 Firecracker 差分 + NBD 换盘，沙箱级
    native_snapshot_bench.py    本文件：e2b 自带的沙箱快照（最近修过封层丢失的那条路）

e2b 原生快照在 SDK 上有两个入口，走的是 orchestrator 里同一段代码
（sandbox.Pause → 导出脏页 memfile diff + 导出 rootfs 写层 diff → 上传成一个
新的 template build），差别只在"之后怎么起来"：

    模式 pause      sb.pause() 停机存快照，Sandbox.connect(id) 用同一个沙箱 ID
                    恢复。一次只能存一份、只能回到最近那份 —— 它本来是给
                    "闲置省资源"用的，不是给"多点回退"用的。所以这一模式
                    每档只能做 dirty → pause → resume → 验，没有 Phase 2。

    模式 snapshot   sb.create_snapshot() 存一份**持久**快照后沙箱继续跑（服务端
                    是 pause + snapshot + 同 ID resume 一气做完），返回一个
                    snapshot_id（其实是 template id）。之后任何时候
                    Sandbox.create(template=snapshot_id) 都能起一个处在那一刻
                    的**新**沙箱。多代快照可以并存、可以回到任何一代，这才是
                    跟 checkpoint/restore 语义对得上的那条路，Phase 1 / Phase 2
                    也照那两份脚本的结构走。

    默认两个模式都跑；--mode 只跑一个。

对应关系（分段那一列原生这套**没有**——orchestrator 里 pause 路径只有 otel
span，不写 timings.json，也不打分段日志——所以这里只有端到端和产物两类数）：

    我们那套（checkpoint_bench_v2.py）          原生这套
    ---------------------------------------     -----------------------------------------
    <store>/<sbx>/<ck>/timings.json             （无）
    mem_diff / mem_full 的 st_blocks             <cache>/<buildID>-memfile-<rand>
                                                 —— fc.ExportMemory 只把 FC 报脏的页
                                                 紧凑地拷进这个文件，文件大小 = 脏页量
    layers/ 里新出现的封层文件                    <cache>/<buildID>-rootfs.ext4-<rand>
                                                 —— 写层里所有有数据的块的紧凑导出
    （产物就地可用）                              <storage>/<buildID>/{memfile,rootfs.ext4,
                                                 *.header,snapfile,metadata.json}
                                                 —— pause 模式里上传是 fire-and-forget，
                                                 pause() 返回时可能还没传完，所以单列
                                                 一个 upload 列：从 pause 返回到目录
                                                 里文件齐、大小不再变的时间（上界）。
                                                 snapshot 模式服务端会等上传完再返回，
                                                 这一列并进 e2e 里了。

多出一列 touch：恢复后**第一次**把负载整个读一遍要多久。原生 resume 起来的
沙箱内存是 uffd 按页缺页懒加载的（resume 返回得快，但每碰一页都要去存储里
找），checkpoint 那套是把回滚集一次性搬回来。只看 e2e 会把这笔延后付的账漏掉，
所以 pause 前先量一次同样的读法作基线（touch0），恢复后再量一次，两者的差就
是懒加载那笔。

负载与校验完全照 checkpoint_bench_v2.py：预热把 /dev/shm 上的文件和根文件系统
上的文件各撑到最大档，之后每档 dd conv=notrunc 只覆写前 N MB；代号分别写
/dev/shm（纯内存，只有内存快照能带回来）和根文件系统，恢复后分开验，再用大文件
的 md5 验内容确实整片回来了。理由见 v2 的模块注释，这里不重复。

必须跑在宿主机上：产物几列要 stat 宿主机上的文件、读 /proc。不在宿主机上也能
跑，只是那几列全空。

依赖（与 rollback/scripts 下其它脚本一致）:
    pip install e2b==2.20.0 python-dotenv        # 2.21 也可
    python /opt/e2b-infra/patch_e2b.py
  注意：不需要 e2b-sdk-checkpoint 那个补丁，这里只用 SDK 自带的 pause /
  connect / create_snapshot，装了也不碍事。

环境变量放当前目录的 .env：E2B_API_KEY / E2B_DOMAIN / E2B_API_URL / E2B_HTTP_SSL

用法:
    python native_snapshot_bench.py                 # 两个模式都跑
    python native_snapshot_bench.py --mode pause    # 只跑 pause/resume
    python native_snapshot_bench.py --mode snapshot # 只跑 create_snapshot/create
    python native_snapshot_bench.py --levels 2      # 只跑前两档（冒烟）
    python native_snapshot_bench.py --keep          # 结束后不删 snapshot 模板
"""
import argparse
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

TEMPLATE = "base"
SBX_TIMEOUT = 3600                    # 秒；一轮两个模式跑满要十几分钟，别让沙箱中途到期

FILE_DIR = "/bench-root"              # 文件那份写这里（根文件系统上，进写层）
NOTE = FILE_DIR + "/blob"
FILE_GEN = FILE_DIR + "/gen"          # 文件侧代号：只有 rootfs diff 能带回来
MEM_GEN = "/dev/shm/gen"              # 内存侧代号：tmpfs 是纯内存，只有 memfile 能带回来
SWEEP = "/dev/shm/sweep"              # 内存那份：tmpfs 是纯内存，不落块设备

# 与另外两份脚本同一张表
EXPERIMENTS = [
    {"mem_mb": 0,   "file_mb": 0},
    {"mem_mb": 12,  "file_mb": 4},
    {"mem_mb": 24,  "file_mb": 8},
    {"mem_mb": 48,  "file_mb": 16},
    {"mem_mb": 96,  "file_mb": 32},
    {"mem_mb": 192, "file_mb": 64},
]

# 脏页后端：改判据之后的 orchestrator（infra-arm jll "native snapshot: 4 KiB write-tracked
# diffs"）原生 pause 路径也走它 —— memfile 只装写跟踪位图（KVM 脏页日志 ∪ FC 用户态
# 位图）里的 4KB 页。改判据之前的 orchestrator 不走它，见 DIRTY_NOTE。
BACKEND_NOTE = {
    "hdbss": "硬件标脏（HDBSS）——CPU 自己记脏页，guest 写页不陷出；写跟踪位图由它供数",
    "kvm-wp": "软件写保护（KVM write-protect）——每页第一次被写都要陷出一次；写跟踪位图由它供数",
    "off": "没开脏页跟踪（FC_TRACK_DIRTY_PAGES）。改判据后的 orchestrator 会退回"
           "\"驻留即脏\"，memfile 又变成工作集口径 —— 部署侧必须开这个开关",
}

# memfile 一列的口径取决于 orchestrator 版本，判读之前先确认跑的是哪一版：
#
# **改判据之后**（jll 分支 native snapshot 4 KiB 提交及以后）：memfile = 这一代真正
# 写过的 4KB 页之和 + 本底（guest 自己的内核/守护进程写入，几 MB）。判据是 FC 的写跟踪
# 位图（PUT /snapshot/save-dirty-bitmap），差分 header 块大小 4KB，恢复时 uffd 按 2MB
# 大页缺页、从映射链按 4KB 拼。920B 实测：只读 192MB 不改一字节 memfile 12.7MB，
# 只改 12MB 为 20.3MB，与写跟踪位图逐档相等。
#
# **改判据之前**：memfile = 自上次 resume 以来**被碰过**的页（工作集），读也算。判据在
# FC fork 的 get_dirty_memory：mincore 驻留 + pagemap 第 57 位（uffd 写保护位）未置。
# 前提两层都不成立：FC 只按 MISSING 模式注册 uffd（整棵源码无 MODE_WP，x86 同样），
# 且 arm64 6.6 内核不支持 uffd-wp（features 0x4ffe，MISSING|WP 注册 EINVAL，探测见
# uffdwp_probe.c），第 57 位永不置起，判据退化成"驻留即脏"。所以 ARM 适配注释掉
# UFFDIO_COPY_MODE_WP 是被迫且正确的。后果是名义脏 0MB 和 192MB 的 memfile 差不多大
# （各档 ~400MB），因为 touch0 每档都把两份负载读一遍。它仍是增量，只是判据太宽。
# 拆解见 probe_dirty.py，两份位图并排比见 pb2.py。
DIRTY_NOTE = (
    "memfile 口径取决于 orchestrator 版本。改判据后（jll 4KiB 写跟踪差分）= 写过的 4KB 页\n"
    "  之和 + 几 MB 本底，各档随写入量线性；改判据前 = 工作集（读也算），各档接近 ~400MB。\n"
    "  判别：cp0 那一行减去预热写入量后只剩几 MB 就是新版。详见脚本顶部注释。"
)

UPLOAD_WAIT_S = 120                   # 等上传落齐的上限
UPLOAD_STABLE_S = 0.5                 # 文件大小多久不变算传完


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


def orchestrator_env():
    """orchestrator 进程实际拿到的环境。读进程不读配置文件：配置只说"本该是什么"，
    进程才知道"实际是什么"。"""
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
        return dict(kv.split("=", 1) for kv in raw.split("\0") if "=" in kv)
    return None


def host_dirs():
    """(导出缓存目录, 本地存储目录, 存储后端名, 存储目录文件系统)。

    导出缓存：sandbox.Pause 里 build.GenerateDiffCachePath(DefaultCacheDir, ...)，
    DefaultCacheDir = ORCHESTRATOR_BASE_PATH/build。pause 返回时两个导出文件已经在
    这里，大小就是这一代真正写下的量。

    本地存储：STORAGE_PROVIDER=Local 时是 LOCAL_TEMPLATE_STORAGE_BASE_PATH（默认
    /tmp/templates）；换成 GCS/S3/MinIO 就没有本地目录可看，存储那几列缺省。"""
    env = orchestrator_env()
    if env is None:
        return None, None, "?", "?"
    cache = os.path.join(env.get("ORCHESTRATOR_BASE_PATH", "/orchestrator"), "build")
    provider = env.get("STORAGE_PROVIDER", "?")
    store = None
    if provider.lower() == "local":
        store = env.get("LOCAL_TEMPLATE_STORAGE_BASE_PATH", "/tmp/templates")
    return cache, store, provider, fstype_of(store or cache)


def fstype_of(path):
    best, fstype = "", "?"
    try:
        for line in open("/proc/mounts"):
            f = line.split()
            if len(f) < 3:
                continue
            mnt = f[1]
            if (path == mnt or path.startswith(mnt.rstrip("/") + "/")) and len(mnt) > len(best):
                best, fstype = mnt, f[2]
    except OSError:
        pass
    return fstype


# ---------------------------------------------------------------- 产物

def alloc(path):
    """文件的磁盘实占（字节）。导出缓存是紧凑文件，实占 ≈ 表观；存储里的 memfile
    可能是稀疏的，按 st_blocks 算才是真写下去的量。"""
    try:
        return os.stat(path).st_blocks * 512
    except OSError:
        return None


def listdir(path):
    try:
        return set(os.listdir(path))
    except (OSError, TypeError):
        return set()


def new_build(cache, before):
    """pause 之后导出缓存里新出现的文件 → (buildID, memfile 字节, rootfs 字节)。

    文件名是 <buildID>-memfile-<rand> / <buildID>-rootfs.ext4-<rand>。同一时刻还
    可能出现别的缓存（比如 resume 时拉基底模板），所以按 buildID 归组，取同时有
    memfile 和 rootfs.ext4 两个新文件的那一组；有多组就取 mtime 最新的。"""
    if not cache:
        return None, None, None
    groups = {}
    for name in listdir(cache) - before:
        for kind in ("-memfile-", "-rootfs.ext4-"):
            if kind in name:
                bid = name.split(kind)[0]
                p = os.path.join(cache, name)
                groups.setdefault(bid, {})[kind.strip("-")] = p
    cands = [(b, g) for b, g in groups.items() if "memfile" in g and "rootfs.ext4" in g]
    if not cands:
        # 退一步：只要有 memfile 也认
        cands = [(b, g) for b, g in groups.items() if "memfile" in g]
    if not cands:
        return None, None, None
    cands.sort(key=lambda bg: max(os.stat(p).st_mtime for p in bg[1].values()), reverse=True)
    bid, g = cands[0]
    return bid, alloc(g["memfile"]), (alloc(g["rootfs.ext4"]) if "rootfs.ext4" in g else None)


def wait_upload(store, bid, t_from):
    """等 <store>/<bid>/ 里六个文件齐、大小停止变化。返回 (耗时 ms 或 None, 目录里
    memfile 实占, rootfs.ext4 实占)。耗时从 t_from 起算 —— pause 模式里那是
    pause() 返回的时刻，所以量到的是"用户以为存完了之后服务端还在忙的时间"。"""
    if not store or not bid:
        return None, None, None
    d = os.path.join(store, bid)
    want = ("memfile", "memfile.header", "rootfs.ext4", "rootfs.ext4.header", "snapfile", "metadata.json")
    last, last_t, deadline = None, None, time.time() + UPLOAD_WAIT_S
    while time.time() < deadline:
        sizes = tuple(alloc(os.path.join(d, w)) for w in want)
        if all(s is not None for s in sizes):
            if sizes == last:
                if time.time() - last_t >= UPLOAD_STABLE_S:
                    return (last_t - t_from) * 1000, sizes[0], sizes[2]
            else:
                last, last_t = sizes, time.time()
        time.sleep(0.05)
    LOG.warning("  上传 %ss 内没落齐：%s", UPLOAD_WAIT_S, d)
    return None, None, None


def fmt_mb(n):
    return "-" if n is None else "%.1f" % (n / 1048576.0)


def fmt_ms(x):
    return "-" if x is None else "%.1f" % x


# ------------------------------------------------------------------- guest 侧

def run(sb, cmd, timeout=300):
    """一律以 root 跑：要写 /bench-root。"""
    return sb.commands.run(cmd, user="root", timeout=timeout).stdout


def warm(sb, mem_max_mb, file_max_mb):
    """预热：把两个文件一次撑到各自的最大档。分配 + 首次写满的成本落在这里，不计入
    任何档位。之后每档只覆写前 N MB。"""
    run(sb, "mkdir -p %s" % FILE_DIR)
    run(sb, "rm -f %s; dd if=/dev/urandom of=%s bs=1M count=%d 2>/dev/null"
        % (SWEEP, SWEEP, max(mem_max_mb, 1)), timeout=1800)
    run(sb, "dd if=/dev/urandom of=%s bs=1M count=%d 2>/dev/null; sync"
        % (NOTE, max(file_max_mb, 1)), timeout=1800)


def dirty_mem(sb, mem_mb):
    """内存那份：原地覆写 /dev/shm 上已撑满的文件的前 N MB。conv=notrunc 只覆写不
    动大小：没有分配、没有清零、没有复制，脏 N MB 就是 N MB。"""
    if mem_mb <= 0:
        return
    run(sb, "dd if=/dev/urandom of=%s bs=1M count=%d conv=notrunc 2>/dev/null"
        % (SWEEP, mem_mb), timeout=1800)


def write_file(sb, file_mb):
    """文件那份：原地覆写预热好的文件的前 N MB，与内存那份对称。返回 md5 前 12 位。"""
    if file_mb > 0:
        run(sb, "dd if=/dev/urandom of=%s bs=1M count=%d conv=notrunc 2>/dev/null; sync"
            % (NOTE, file_mb), timeout=1800)
    else:
        run(sb, "sync")
    return run(sb, "md5sum %s | cut -c1-12" % NOTE, timeout=600).strip()


def mark(sb, gen):
    """代号写两处，恢复后分开验：谁回来了、谁没回来。"""
    run(sb, "echo %s > %s" % (gen, MEM_GEN))
    run(sb, "mkdir -p %s && echo %s > %s && sync" % (FILE_DIR, gen, FILE_GEN))


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


def touch(sb):
    """把两份负载整个读一遍，返回 guest 里量到的毫秒数（用 guest 的时钟，不含
    HTTP 往返）。/dev/shm 那份只能走页缓存（tmpfs 本身就是页缓存）；根文件系统
    那份用 O_DIRECT 绕过 guest 页缓存，逼它真的去读块设备 —— 不然一个刚 resume
    的沙箱页缓存是空的、对照基线却是满的，比的不是一回事。"""
    out = run(sb, "t0=$(date +%%s%%N); "
                  "dd if=%s of=/dev/null bs=1M 2>/dev/null; "
                  "dd if=%s of=/dev/null bs=1M iflag=direct 2>/dev/null; "
                  "t1=$(date +%%s%%N); echo $(( (t1 - t0) / 1000000 ))"
              % (SWEEP, NOTE), timeout=1800)
    try:
        return float(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None


def verify(sb, gen, md5_of):
    st = now_state(sb)
    ok_mem = st.get("mem") == gen
    ok_file = st.get("file") == gen
    ok_md5 = st.get("md5") == md5_of.get(gen)
    return ok_mem, ok_file, ok_md5, st


def mark_str(ok_mem, ok_file, ok_md5):
    return ("✓" if ok_mem else "✗"), (("✓" if ok_md5 else "✗md5") if ok_file else "✗")


# ----------------------------------------------------------------- 模式 pause

def bench_pause(levels, cache, store):
    """每档：dirty → pause → connect（同 ID 恢复）→ 验。没有 Phase 2：pause 只留最近
    一份，回不到更早的。"""
    rows = []   # dict(name, mem_mb, file_mb, pause_ms, upload_ms, resume_ms, touch0, touch1, memB, diskB, ok...)
    rc = 0
    sb = Sandbox.create(template=TEMPLATE, timeout=SBX_TIMEOUT)
    sid = sb.sandbox_id
    LOG.info("[pause] sandbox %s", sid)
    md5_of = {}
    backend = "?"
    try:
        info = fc_get(sid) or {}
        backend = info.get("dirty_tracking", "?")
        mem_max = max(e["mem_mb"] for e in levels)
        file_max = max(e["file_mb"] for e in levels)
        LOG.info("[pause] 预热: /dev/shm 撑到 %d MB、%s 撑到 %d MB（不计入档位）", mem_max, NOTE, file_max)
        warm(sb, mem_max, file_max)

        for i, exp in enumerate(levels):
            name = "cp%d" % i
            dirty_mem(sb, exp["mem_mb"])
            md5_of[name] = write_file(sb, exp["file_mb"])
            mark(sb, name)
            time.sleep(0.3)
            touch0 = touch(sb)

            before = listdir(cache)
            t0 = time.time()
            sb.pause()
            t_paused = time.time()
            pause_ms = (t_paused - t0) * 1000
            bid, mem_b, disk_b = new_build(cache, before)
            upload_ms, st_mem, st_disk = wait_upload(store, bid, t_paused)

            t0 = time.time()
            sb = Sandbox.connect(sid, timeout=SBX_TIMEOUT)
            resume_ms = (time.time() - t0) * 1000
            time.sleep(0.3)
            ok_mem, ok_file, ok_md5, st = verify(sb, name, md5_of)
            touch1 = touch(sb)
            if not (ok_mem and ok_file and ok_md5):
                rc = 1
                LOG.warning("  state=%s expect=%s md5=%s", st, name, md5_of.get(name))
            m, f = mark_str(ok_mem, ok_file, ok_md5)
            LOG.info("[pause] %s (%3dMB/%3dMB): pause=%7.1fms upload=%sms resume=%7.1fms "
                     "touch %s→%sms  内存=%s 文件=%s  memfile=%sMB rootfs=%sMB build=%s",
                     name, exp["mem_mb"], exp["file_mb"], pause_ms, fmt_ms(upload_ms), resume_ms,
                     fmt_ms(touch0), fmt_ms(touch1), m, f, fmt_mb(mem_b), fmt_mb(disk_b),
                     (bid or "?")[:8])
            rows.append(dict(name=name, mem_mb=exp["mem_mb"], file_mb=exp["file_mb"],
                             pause_ms=pause_ms, upload_ms=upload_ms, resume_ms=resume_ms,
                             touch0=touch0, touch1=touch1, mem_b=mem_b, disk_b=disk_b,
                             st_mem=st_mem, st_disk=st_disk,
                             ok_mem=ok_mem, ok_file=ok_file, ok_md5=ok_md5, bid=bid))
    except Exception as e:
        LOG.error("[pause] FAILED: %s", e)
        import traceback
        traceback.print_exc()
        rc = 1
    finally:
        try:
            sb.kill()
        except Exception:
            pass
    return rows, rc, backend


# -------------------------------------------------------------- 模式 snapshot

def bench_snapshot(levels, cache, store, keep):
    """Phase 1：每档 dirty → create_snapshot（沙箱继续跑）。
    Phase 2：对每一代 Sandbox.create(template=snapshot_id) 起新沙箱 → 验 → 杀。
    与 checkpoint 两份脚本的两阶段一一对应。"""
    cps = []      # dict(name, mem_mb, file_mb, e2e, mem_b, disk_b, st_mem, st_disk, snap_id, bid, touch0)
    restores = [] # dict(name, mem_mb, file_mb, e2e, touch1, ok...)
    rc = 0
    sb = Sandbox.create(template=TEMPLATE, timeout=SBX_TIMEOUT)
    sid = sb.sandbox_id
    LOG.info("[snapshot] sandbox %s", sid)
    md5_of = {}
    backend = "?"
    try:
        info = fc_get(sid) or {}
        backend = info.get("dirty_tracking", "?")
        mem_max = max(e["mem_mb"] for e in levels)
        file_max = max(e["file_mb"] for e in levels)
        LOG.info("[snapshot] 预热: /dev/shm 撑到 %d MB、%s 撑到 %d MB（不计入档位）", mem_max, NOTE, file_max)
        warm(sb, mem_max, file_max)

        LOG.info("====== [snapshot] Phase 1: create_snapshot ======")
        for i, exp in enumerate(levels):
            name = "cp%d" % i
            dirty_mem(sb, exp["mem_mb"])
            md5_of[name] = write_file(sb, exp["file_mb"])
            mark(sb, name)
            time.sleep(0.3)
            touch0 = touch(sb)

            before = listdir(cache)
            t0 = time.time()
            snap = sb.create_snapshot()
            e2e = (time.time() - t0) * 1000
            bid, mem_b, disk_b = new_build(cache, before)
            # 服务端等上传完才返回，这里只是顺手把存储里的实占也记下来
            _, st_mem, st_disk = wait_upload(store, bid, time.time())
            LOG.info("[snapshot] %s (%3dMB/%3dMB): e2e=%7.1fms  memfile=%sMB rootfs=%sMB  "
                     "snapshot_id=%s build=%s",
                     name, exp["mem_mb"], exp["file_mb"], e2e, fmt_mb(mem_b), fmt_mb(disk_b),
                     snap.snapshot_id, (bid or "?")[:8])
            cps.append(dict(name=name, mem_mb=exp["mem_mb"], file_mb=exp["file_mb"], e2e=e2e,
                            mem_b=mem_b, disk_b=disk_b, st_mem=st_mem, st_disk=st_disk,
                            snap_id=snap.snapshot_id, bid=bid, touch0=touch0))
            # create_snapshot 内部是 pause + resume，原沙箱内存又变成懒加载了；
            # 先摸一遍再进下一档，否则下一档的 dd 会把缺页成本混进去。
            touch(sb)

        LOG.info("====== [snapshot] Phase 2: Sandbox.create(snapshot) ======")
        # 顺序与另两份脚本相同：cp1 → cp2 → …（cp0 只当链根）。每一代都是从存储里
        # 另起一个新沙箱，一跳的代价与上一跳无关，这一点跟差分树那套不同。
        for c in cps[1:]:
            name = c["name"]
            t0 = time.time()
            nsb = Sandbox.create(template=c["snap_id"], timeout=600)
            e2e = (time.time() - t0) * 1000
            try:
                time.sleep(0.3)
                ok_mem, ok_file, ok_md5, st = verify(nsb, name, md5_of)
                touch1 = touch(nsb)
            finally:
                try:
                    nsb.kill()
                except Exception:
                    pass
            if not (ok_mem and ok_file and ok_md5):
                rc = 1
                LOG.warning("  state=%s expect=%s md5=%s", st, name, md5_of.get(name))
            m, f = mark_str(ok_mem, ok_file, ok_md5)
            LOG.info("[snapshot] restore %-4s (%3dMB/%3dMB): e2e=%7.1fms  touch %s→%sms  内存=%s 文件=%s",
                     name, c["mem_mb"], c["file_mb"], e2e, fmt_ms(c["touch0"]), fmt_ms(touch1), m, f)
            restores.append(dict(name=name, mem_mb=c["mem_mb"], file_mb=c["file_mb"], e2e=e2e,
                                 touch0=c["touch0"], touch1=touch1,
                                 ok_mem=ok_mem, ok_file=ok_file, ok_md5=ok_md5))
    except Exception as e:
        LOG.error("[snapshot] FAILED: %s", e)
        import traceback
        traceback.print_exc()
        rc = 1
    finally:
        try:
            sb.kill()
        except Exception:
            pass
        if not keep:
            for c in cps:
                try:
                    Sandbox.delete_snapshot(c["snap_id"])
                except Exception as e:
                    LOG.warning("  delete_snapshot %s: %s", c["snap_id"], str(e)[:80])
    return cps, restores, rc, backend


# ----------------------------------------------------------------------- 汇总

def print_pause(rows):
    print("\n" + "=" * 118)
    print("Native pause / resume Summary（同一沙箱，dirty → pause → connect）")
    print("=" * 118)
    print("%-6s %8s %9s %10s %10s %10s %9s %9s %11s %12s %7s %8s"
          % ("name", "mem(MB)", "file(MB)", "pause(ms)", "upload(ms)", "resume(ms)",
             "touch0", "touch1", "memfile(MB)", "rootfs(MB)", "mem_ok", "file_ok"))
    print("-" * 118)
    for r in rows:
        m, f = mark_str(r["ok_mem"], r["ok_file"], r["ok_md5"])
        print("%-6s %8d %9d %10.1f %10s %10.1f %9s %9s %11s %12s %7s %8s"
              % (r["name"], r["mem_mb"], r["file_mb"], r["pause_ms"], fmt_ms(r["upload_ms"]),
                 r["resume_ms"], fmt_ms(r["touch0"]), fmt_ms(r["touch1"]),
                 fmt_mb(r["mem_b"]), fmt_mb(r["disk_b"]), m, f))
    print("-" * 118)
    print("  pause = sb.pause() 端到端（停机 + 导出脏页 memfile diff + 导出 rootfs 写层 + 写缓存，")
    print("  **不含上传**）；upload = pause 返回后到存储目录里六个文件落齐且大小不再变（上界）。")
    print("  resume = Sandbox.connect() 端到端。touch0/touch1 = pause 前 / resume 后把两份负载整个")
    print("  读一遍的 guest 侧毫秒数，差值就是 uffd 懒加载延后付的账 —— 比 resume 一列更接近")
    print("  用户真正等到的时间。memfile/rootfs 是导出缓存的实占：memfile 只装 FC 报脏的页，")
    print("  rootfs 只装写层里有数据的块。cp0 那一行是**本底**（从模板起来到第一次 pause 之间")
    print("  系统自己弄脏的页 + 预热写下的文件），各档共有，比对时先减掉。")
    print("  这一模式没有 Phase 2：pause 只保留最近一份，回不到更早的。")


def print_snapshot(cps, restores):
    print("\n" + "=" * 118)
    print("Native create_snapshot Summary（沙箱继续跑，每代一个持久 snapshot 模板）")
    print("=" * 118)
    print("%-6s %8s %9s %11s %11s %12s %11s %12s  %s"
          % ("name", "mem(MB)", "file(MB)", "e2e(ms)", "memfile(MB)", "rootfs(MB)",
             "storeMem", "storeRootfs", "snapshot_id"))
    print("-" * 118)
    for c in cps:
        print("%-6s %8d %9d %11.1f %11s %12s %11s %12s  %s"
              % (c["name"], c["mem_mb"], c["file_mb"], c["e2e"], fmt_mb(c["mem_b"]),
                 fmt_mb(c["disk_b"]), fmt_mb(c["st_mem"]), fmt_mb(c["st_disk"]), c["snap_id"]))
    print("-" * 118)
    print("  e2e = sb.create_snapshot() 端到端 —— 服务端是 pause + 导出 + 同 ID resume + **等上传")
    print("  完成**一气做完才返回，所以这一列天然比 pause 模式的 pause 列大一截。")
    print("  memfile/rootfs 是导出缓存实占（这一代真正写下的量），storeMem/storeRootfs 是本地")
    print("  存储里同一份的实占；两者应相等，不等说明上传路径改了布局。cp0 是本底。")

    print("\n" + "=" * 118)
    print("Native restore Summary（Sandbox.create(template=snapshot_id) 另起新沙箱）")
    print("=" * 118)
    print("%-6s %8s %9s %11s %9s %9s %7s %8s"
          % ("name", "mem(MB)", "file(MB)", "e2e(ms)", "touch0", "touch1", "mem_ok", "file_ok"))
    print("-" * 118)
    for r in restores:
        m, f = mark_str(r["ok_mem"], r["ok_file"], r["ok_md5"])
        print("%-6s %8d %9d %11.1f %9s %9s %7s %8s"
              % (r["name"], r["mem_mb"], r["file_mb"], r["e2e"],
                 fmt_ms(r["touch0"]), fmt_ms(r["touch1"]), m, f))
    print("-" * 118)
    print("  e2e = Sandbox.create() 到能执行命令。每一代都从存储另起新沙箱，一跳与上一跳无关，")
    print("  没有差分树那套的 from → to 问题，可以直接照档位读。touch1 是新沙箱第一次把负载整个")
    print("  读一遍的毫秒数（内存走 uffd 缺页、文件走 O_DIRECT 读块设备），touch0 是建快照前")
    print("  同一读法的基线；差值是 resume 快在哪儿——把搬内存的活推到了每次缺页上。")
    print("  内存列验 /dev/shm 里的代号（只有 memfile 能带回来），文件列验根文件系统里的代号 +")
    print("  大文件 md5 —— 哪一半没回来一眼能看出。")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--mode", choices=["pause", "snapshot", "both"], default="both")
    ap.add_argument("--levels", type=int, default=len(EXPERIMENTS),
                    help="只跑档位表前 N 行（含 0 档），冒烟用")
    ap.add_argument("--keep", action="store_true", help="结束后不删 snapshot 模式建的快照模板")
    args = ap.parse_args()
    levels = EXPERIMENTS[:max(1, args.levels)]

    cache, store, provider, fstype = host_dirs()
    if cache:
        LOG.info("导出缓存 : %s", cache)
        LOG.info("存储后端 : %s   本地目录 = %s   文件系统 = %s", provider, store or "（非本地，存储列缺省）", fstype)
    else:
        LOG.info("读不到 orchestrator 进程 —— 没跑在宿主机上？产物几列全空，其余照跑。")

    rc = 0
    backends = set()
    p_rows = c_rows = r_rows = None
    if args.mode in ("pause", "both"):
        p_rows, prc, b = bench_pause(levels, cache, store)
        rc |= prc
        backends.add(b)
    if args.mode in ("snapshot", "both"):
        c_rows, r_rows, src, b = bench_snapshot(levels, cache, store, args.keep)
        rc |= src
        backends.add(b)

    backend = next((b for b in backends if b != "?"), "?")
    print("\n脏页后端 : %s" % BACKEND_NOTE.get(
        backend, "未知（拿不到 Firecracker 套接字——没跑在宿主机上？）"))
    if p_rows is not None:
        print_pause(p_rows)
    if c_rows is not None:
        print_snapshot(c_rows, r_rows)

    print()
    print("脏页判据 : %s" % DIRTY_NOTE)
    if not cache:
        print("注意：没跑在宿主机上，产物几列全空。")
    all_ok = all(r["ok_mem"] and r["ok_file"] and r["ok_md5"]
                 for r in (p_rows or []) + (r_rows or []))
    if rc == 0 and all_ok:
        print("每一次恢复的内存代号、文件代号和大文件 md5 都回到了目标代。")
    return rc


if __name__ == "__main__":
    sys.exit(main())
