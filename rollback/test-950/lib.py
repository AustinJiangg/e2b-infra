"""Shared helpers for the 950 test scripts.

Nothing here is scheme-specific except SCHEME_MEMFILE: the two schemes are
meant to behave identically through the API, and these scripts are how that
claim gets checked.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time

os.environ.setdefault("E2B_DOMAIN", "e2b.app")
os.environ.setdefault("E2B_HTTP_SSL", "false")
if not os.environ.get("E2B_API_URL"):
    sys.exit("请先 export E2B_API_URL=http://<950-ip>:3000")
if not os.environ.get("E2B_API_KEY"):
    sys.exit("请先 export E2B_API_KEY=<key>")

from e2b import Sandbox  # noqa: E402  (after the env is set)

# The per-generation memory artifact is named differently by the two schemes:
# XFS keeps a logically complete image per generation, ext4 a sparse diff.
SCHEME_MEMFILE = {"xfs": "mem_full", "ext4": "mem_diff"}


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout.strip()


def orchestrator_env():
    """The environment the running orchestrator actually got, so the tests
    measure the deployment rather than what someone meant to deploy.

    The binary is /usr/bin/orchestrator in a normal deployment but often a
    hash-suffixed build artifact while testing, so identify it by its own
    environment (ORCHESTRATOR_SERVICES) as well as by name."""
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            base = os.path.basename(os.path.realpath("/proc/%s/exe" % pid))
            raw = open("/proc/%s/environ" % pid, "rb").read().decode("utf8", "replace")
        except Exception:
            continue
        named = base.startswith("orchestrator") or base.startswith("template-manager")
        if not (named or "ORCHESTRATOR_SERVICES=" in raw):
            continue
        return dict(kv.split("=", 1) for kv in raw.split("\0") if "=" in kv)
    return {}


ENV = orchestrator_env()
BASE_PATH = ENV.get("ORCHESTRATOR_BASE_PATH", "/orchestrator")
STORE = os.path.join(BASE_PATH, "build", "checkpoints")
FULL_ROOT = ENV.get("CHECKPOINT_FULL_ROOT", "true").lower() in ("true", "1", "")


def fc_socket(sandbox_id):
    """Firecracker's API socket for a sandbox (orchestrator puts it in TMPDIR
    as fc-<sandbox>-<random>.sock)."""
    import glob
    hits = glob.glob(os.path.join(tempfile.gettempdir(), "fc-%s-*.sock" % sandbox_id))
    return hits[0] if hits else None


def fc_get(sandbox_id, path="/"):
    """GET on Firecracker's unix socket, without needing curl."""
    sock_path = fc_socket(sandbox_id)
    if not sock_path:
        return None
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(5)
        s.connect(sock_path)
        s.sendall(("GET %s HTTP/1.1\r\nHost: localhost\r\nAccept: application/json\r\n\r\n"
                   % path).encode())
        # Firecracker answers keep-alive and does not close, so read exactly
        # what Content-Length promises instead of waiting for EOF.
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
    except (OSError, ValueError):
        return None
    finally:
        s.close()
    try:
        return json.loads(body)
    except ValueError:
        return None


def dirty_tracking(sandbox_id):
    """Which dirty-tracking backend Firecracker actually chose for this
    sandbox: "hdbss" (hardware), "kvm-wp" (software write-protect), "off",
    or "?" when the running Firecracker predates the field.

    This is the answer to "did HDBSS really get used", and it is per sandbox
    — so every measurement can state which backend produced it instead of
    assuming the one the host is capable of."""
    info = fc_get(sandbox_id) or {}
    return info.get("dirty_tracking", "?")


def fc_pid(sandbox_id):
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            cmd = open("/proc/%s/cmdline" % pid, "rb").read().decode("utf8", "replace")
        except Exception:
            continue
        if "firecracker" in cmd and sandbox_id in cmd:
            return int(pid)
    return None


def store_fstype():
    return sh("findmnt -no FSTYPE --target %s" % STORE) or "?"


def df_used_mb(mount):
    out = sh("df -m --output=used %s | tail -1" % mount)
    return int(out) if out.isdigit() else -1


class Checker:
    def __init__(self):
        self.failures = []

    def __call__(self, cond, what):
        print("  [%s] %s" % ("PASS" if cond else "FAIL", what), flush=True)
        if not cond:
            self.failures.append(what)
        return cond

    def finish(self, label="ALL PASS"):
        if self.failures:
            print("FAILED (%d):" % len(self.failures), flush=True)
            for f in self.failures:
                print("  - " + f, flush=True)
            return 1
        print(label, flush=True)
        return 0


class Box:
    """One sandbox plus the markers that prove a restore really landed on the
    target moment: a tmpfs file (guest RAM), a disk file, and the checksum of
    a multi-megabyte blob (bulk memory, not just one page)."""

    def __init__(self, template="base", timeout=1800):
        self.sbx = Sandbox.create(template=template, timeout=timeout)
        self.id = self.sbx.sandbox_id
        self.cks = {}
        self.sums = {}
        self.backend = dirty_tracking(self.id)
        print("sandbox: %s   store: %s (%s)   dirty tracking: %s"
              % (self.id, STORE, store_fstype(), self.backend), flush=True)
        if self.backend == "kvm-wp":
            print("  注意：本次跑在 KVM 写保护上，不是 HDBSS——数字里不含硬件标脏的收益。", flush=True)
        elif self.backend == "off":
            print("  警告：脏页跟踪没开，checkpoint 会全部退化成全量。", flush=True)

    def run(self, cmd, timeout=120):
        return self.sbx.commands.run(cmd, timeout=timeout).stdout.strip()

    def dir_of(self, name):
        return os.path.join(STORE, self.id, self.cks[name])

    def manifest(self, name):
        try:
            return json.load(open(os.path.join(self.dir_of(name), "manifest.json")))
        except (FileNotFoundError, ValueError):
            return None

    def dirty(self, mb):
        if mb:
            self.sbx.commands.run(
                "dd if=/dev/urandom of=/dev/shm/blob bs=1M count=%d 2>/dev/null" % mb, timeout=300)

    def stamp(self, name):
        self.sbx.commands.run(
            "echo %s > /dev/shm/marker && echo %s > /home/user/marker && sync" % (name, name),
            timeout=60)
        self.sums[name] = self.blob_sum()

    def blob_sum(self):
        return self.run("md5sum /dev/shm/blob 2>/dev/null | cut -c1-8")

    def markers(self):
        m = self.run("cat /dev/shm/marker /home/user/marker").split()
        return (m + ["?", "?"])[:2]

    def checkpoint(self, name, dirty_mb=0, mount=None):
        self.dirty(dirty_mb)
        self.stamp(name)
        u0 = df_used_mb(mount) if mount else 0
        t0 = time.monotonic()
        ck = self.sbx.checkpoint.create(name=name)
        dt = time.monotonic() - t0
        u1 = df_used_mb(mount) if mount else 0
        self.cks[name] = ck.checkpoint_id
        mode = (self.manifest(name) or {}).get("mem_mode")
        print("CREATE %-5s dirty=%4dMB  time=%6.3fs  df_delta=%5dMB  mem_mode=%s"
              % (name, dirty_mb, dt, u1 - u0, mode), flush=True)
        return dt, u1 - u0, mode

    def restore(self, name, note=""):
        t0 = time.monotonic()
        ok = self.sbx.checkpoint.restore(self.cks[name])
        dt = time.monotonic() - t0
        mem, disk = self.markers()
        blob = self.blob_sum()
        good = bool(ok) and mem == name and disk == name and blob == self.sums[name]
        print("RESTORE %-5s time=%6.3fs  ok=%s mem=%s disk=%s blob=%s  %s"
              % (name, dt, ok, mem, disk, blob or "-", note), flush=True)
        return dt, good

    def kill(self):
        try:
            self.sbx.kill()
        except Exception:
            pass


def wait_for_api(tries=30, delay=4):
    """A freshly restarted node refuses placement for a few seconds. Poll
    rather than let the first test fail for an unrelated reason."""
    from e2b import Sandbox as S
    for i in range(tries):
        try:
            s = S.create(template="base", timeout=60)
            s.kill()
            if i:
                print("API 就绪（第 %d 次尝试）" % (i + 1), flush=True)
            return True
        except Exception as e:
            last = e
            time.sleep(delay)
    print("API 一直起不来: %s" % last, flush=True)
    return False
