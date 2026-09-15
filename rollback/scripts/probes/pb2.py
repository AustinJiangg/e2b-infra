#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""原生快照能不能做到精确增量：把两份位图按**各自的页粒度**换算后并排比。

前一版把两份位图都按 4KB 换算，而 /memory/dirty 在开了大页时是 2MB 粒度，
差 512 倍，导致"对不上"。这一版从 /machine-config 读大页配置、从 FCDB 头读
页大小，各按各的算，并且每档真做一次 create_snapshot 取实际导出量作锚点。

    GET  /memory/dirty               现在原生 pause 用的判据
    PUT  /snapshot/save-dirty-bitmap KVM/HDBSS 写跟踪 ∪ FC 宿主侧写入（我们那套用的）
    create_snapshot 导出的 memfile    真值锚点
"""
import glob, json, os, socket, struct, sys, tempfile, time
from e2b import Sandbox
from dotenv import load_dotenv
load_dotenv()

MB = 1048576
SWEEP = "/dev/shm/sweep"


def fc_sock(sid):
    h = glob.glob(os.path.join(tempfile.gettempdir(), "fc-%s-*.sock" % sid))
    return max(h, key=os.path.getmtime) if h else None


def fc(sock, method, path, body=None):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(120)
    try:
        s.connect(sock)
        payload = b"" if body is None else json.dumps(body).encode()
        req = "%s %s HTTP/1.1\r\nHost: localhost\r\nAccept: application/json\r\n" % (method, path)
        if body is not None:
            req += "Content-Type: application/json\r\nContent-Length: %d\r\n" % len(payload)
        req += "\r\n"
        s.sendall(req.encode() + payload)
        buf = b""
        while b"\r\n\r\n" not in buf:
            c = s.recv(65536)
            if not c:
                return 0, None
            buf += c
        head, rest = buf.split(b"\r\n\r\n", 1)
        code = int(head.split(b" ")[1])
        n = 0
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                n = int(line.split(b":", 1)[1])
        while len(rest) < n:
            c = s.recv(65536)
            if not c:
                break
            rest += c
        try:
            return code, json.loads(rest[:n]) if n else (code, None)[1]
        except Exception:
            return code, rest[:n].decode("utf8", "replace")
    finally:
        s.close()


def popcnt(words):
    return sum(bin(w & (2**64 - 1)).count("1") for w in words)


def fcdb(path):
    d = open(path, "rb").read()
    if len(d) < 24 or d[:4] != b"FCDB":
        return None, None
    ps = struct.unpack_from("<Q", d, 8)[0]
    npg = struct.unpack_from("<Q", d, 16)[0]
    w = struct.unpack_from("<%dQ" % ((npg + 63) // 64), d, 24)
    return popcnt(w), ps


def orch_cache():
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            raw = open("/proc/%s/environ" % pid, "rb").read().decode("utf8", "replace")
            b = os.path.basename(os.path.realpath("/proc/%s/exe" % pid))
        except OSError:
            continue
        if b.startswith("orchestrator") or "ORCHESTRATOR_SERVICES=" in raw:
            e = dict(kv.split("=", 1) for kv in raw.split("\0") if "=" in kv)
            return os.path.join(e.get("ORCHESTRATOR_BASE_PATH", "/orchestrator"), "build")
    return None


CACHE = orch_cache()


def export_size(before):
    best = None
    for n in (set(os.listdir(CACHE)) - before) if CACHE else []:
        if "-memfile-" in n:
            try:
                v = os.stat(os.path.join(CACHE, n)).st_blocks * 512
            except OSError:
                continue
            best = v if best is None or v > best else best
    return best


def main():
    sb = Sandbox.create(template="base", timeout=1800)
    sid = sb.sandbox_id
    print("沙箱 %s   导出缓存 %s" % (sid, CACHE))
    sock = fc_sock(sid)
    code, mc = fc(sock, "GET", "/machine-config")
    hp = (mc or {}).get("huge_pages", "?")
    dirty_ps = 2 * MB if str(hp).lower() in ("2m", "hugetlbfs2m", "nr2m") else 4096
    code, info = fc(sock, "GET", "/")
    print("FC %s   脏页后端 %s   huge_pages=%s → /memory/dirty 位图粒度 %d KB"
          % ((info or {}).get("vmm_version", "?"), (info or {}).get("dirty_tracking", "?"),
             hp, dirty_ps // 1024))
    print()
    hdr = ("%-30s %14s %14s %14s" % ("步骤", "/memory/dirty", "KVM+宿主写", "实际导出"))
    print(hdr); print("-" * len(hdr))
    snaps = []

    def step(label, cmd=None):
        nonlocal sock
        if cmd:
            sb.commands.run(cmd, user="root", timeout=900)
        sock = fc_sock(sid)
        c, _ = fc(sock, "PATCH", "/vm", {"state": "Paused"})
        dm = kv = kps = None
        if c < 300:
            c2, r = fc(sock, "GET", "/memory/dirty")
            if c2 == 200 and isinstance(r, dict):
                dm = popcnt(r.get("bitmap", []))
            p = "/tmp/pb2-%s.fcdb" % sid
            c3, _ = fc(sock, "PUT", "/snapshot/save-dirty-bitmap", {"path": p})
            if c3 < 300:
                kv, kps = fcdb(p)
                try:
                    os.unlink(p)
                except OSError:
                    pass
            fc(sock, "PATCH", "/vm", {"state": "Resumed"})
        before = set(os.listdir(CACHE)) if CACHE else set()
        s = sb.create_snapshot()
        snaps.append(s.snapshot_id)
        ex = export_size(before)
        f = lambda v, ps: "-" if v is None else "%9.1f MB" % (v * ps / MB)
        print("%-30s %14s %14s %14s"
              % (label, f(dm, dirty_ps), f(kv, kps or 4096),
                 "-" if ex is None else "%9.1f MB" % (ex / MB)))

    step("A 刚建好，什么都没做")
    step("B 写 192MB 到 /dev/shm", "dd if=/dev/urandom of=%s bs=1M count=192 2>/dev/null" % SWEEP)
    step("C 什么都不做")
    step("D 只读那 192MB，一字节没改", "dd if=%s of=/dev/null bs=1M 2>/dev/null" % SWEEP)
    step("E 什么都不做")
    step("F 再写其中 12MB", "dd if=/dev/urandom of=%s bs=1M count=12 conv=notrunc 2>/dev/null" % SWEEP)

    print("-" * len(hdr))
    print("每一档都真做了一次 create_snapshot，所以每档之间沙箱都经历了 pause+resume，")
    print("驻留集和位图都从零重建 —— 与 native_snapshot_bench.py 的节奏一致。")
    print()
    print("判读：")
    print("  D 行：只读不写。/memory/dirty 与实际导出若跟着涨 → 现判据把'读'算成脏。")
    print("        KVM+宿主写 若不涨 → 换成这份位图就能做到精确增量。")
    print("  F 行：只改 12MB。KVM+宿主写 应约等于 12MB + 本底。")
    for s in snaps:
        try:
            Sandbox.delete_snapshot(s)
        except Exception:
            pass
    sb.kill()


if __name__ == "__main__":
    sys.exit(main())
