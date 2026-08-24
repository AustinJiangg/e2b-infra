#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 checkpoint/restore 能力装进已经 pip 装好的 e2b 包里。

做法是**整文件覆盖**，不是字符串替换。payload/ 下的目录结构就是 site-packages
里的结构，装的时候原样铺过去。

为什么不用打补丁的方式：这一版一共动 16 个文件，其中 11 个是全新文件，
只有 5 个是改上游既有文件。对着上游源码做字符串替换，上游一改版就可能替换错地方
而且不报错。整文件覆盖是版本锁死的 —— 只对 e2b==2.20.0 有效，装之前先核版本，
对不上就停，不会悄悄装出一个半吊子。

被覆盖的上游文件都会留一份 .orig 备份（只备份一次），--uninstall 可以还原。

用法:
  python3 install.py              # 安装
  python3 install.py --force      # 跳过版本核对（自己负责）
  python3 install.py --uninstall  # 还原
  python3 install.py --check      # 只看状态，不动文件
"""
import os
import shutil
import sys

# payload 是照着这个版本的 e2b 做的
PINNED = "2.20.0"
HERE = os.path.dirname(os.path.abspath(__file__))
PAYLOAD = os.path.join(HERE, "payload")

# 覆盖的上游既有文件（会备份）。其余是新增文件，卸载时直接删。
UPSTREAM_FILES = {
    "e2b/__init__.py",
    "e2b/connection_config.py",
    "e2b/sandbox/main.py",
    "e2b/sandbox_sync/main.py",
    "e2b/sandbox_async/main.py",
}


def die(msg):
    print("✗ " + msg, file=sys.stderr)
    sys.exit(1)


def site_packages():
    try:
        import e2b
    except ImportError:
        die("没找到 e2b 包。先 pip install e2b==%s" % PINNED)
    return os.path.dirname(os.path.dirname(os.path.abspath(e2b.__file__)))


def installed_version():
    try:
        from importlib.metadata import version
        return version("e2b")
    except Exception:
        return None


def payload_files():
    out = []
    for root, _, files in os.walk(PAYLOAD):
        for f in files:
            p = os.path.join(root, f)
            out.append(os.path.relpath(p, PAYLOAD))
    return sorted(out)


def drop_pycache(base):
    n = 0
    for root, dirs, _ in os.walk(base):
        for d in list(dirs):
            if d == "__pycache__":
                shutil.rmtree(os.path.join(root, d), ignore_errors=True)
                dirs.remove(d)
                n += 1
    return n


# 老名字。phz 那套把同一个东西装在 e2b/gsd/ 下，里面的 checkpoint_pb2 注册的是
# 同一个 proto 文件名 checkpoint/checkpoint.proto。两份同时在场，谁先谁后地被 import
# 到，protobuf 的全局描述符池就会报 "duplicate file name" 然后整个 e2b 都 import 不了。
# 所以装之前必须把老目录挪开，而不是放着不管。
STALE = "e2b/gsd"
STALE_PARKED = "e2b/gsd.replaced-by-checkpointd"

VERIFY_SRC = r"""
import sys
from e2b import Sandbox, CheckpointInfo
from e2b.sandbox_sync.checkpoint import Checkpoint
from e2b.sandbox_async.checkpoint import AsyncCheckpoint
from e2b.checkpointd.checkpoint import checkpoint_pb2
from e2b.connection_config import ConnectionConfig

problems = []
if not isinstance(getattr(Sandbox, "checkpoint", None), property):
    problems.append("Sandbox.checkpoint 属性不在")
if "mem_mode" not in CheckpointInfo("x").__dict__:
    problems.append("CheckpointInfo 没有 mem_mode")
fields = [f.name for f in checkpoint_pb2.CheckpointInfo.DESCRIPTOR.fields]
if "mem_mode" not in fields:
    problems.append("proto 的 CheckpointInfo 没有 mem_mode（字段：%s）" % fields)
for cls, name in ((Checkpoint, "Checkpoint"), (AsyncCheckpoint, "AsyncCheckpoint")):
    for m in ("create", "restore", "list", "delete", "is_running", "is_available"):
        if not hasattr(cls, m):
            problems.append("%s 缺方法 %s" % (name, m))
if ConnectionConfig.checkpointd_port != 49984:
    problems.append("checkpointd_port 不是 49984")
if "Authorization" in ConnectionConfig().checkpointd_headers:
    problems.append("checkpointd_headers 里还有 Authorization")
print("\n".join(problems))
sys.exit(1 if problems else 0)
"""


def park_stale(sp):
    """把老的 e2b/gsd/ 挪开（改名，不删），返回是否挪过。"""
    src, dst = os.path.join(sp, STALE), os.path.join(sp, STALE_PARKED)
    if not os.path.isdir(src):
        return False
    if os.path.isdir(dst):
        shutil.rmtree(src, ignore_errors=True)
    else:
        os.rename(src, dst)
    return True


def verify(sp):
    """装完真的能用吗。

    必须开一个干净的解释器来验：protobuf 的描述符池是进程级的全局状态，
    本进程早就 import 过一次老的 e2b 了，在同一个进程里重新 import 只会撞池子，
    验出来的东西不代表用户新起一个进程会看到什么。
    """
    import subprocess
    r = subprocess.run([sys.executable, "-c", VERIFY_SRC], capture_output=True, text=True,
                       cwd="/", env={**os.environ, "PYTHONPATH": sp})
    if r.returncode != 0:
        die("装完自检不过：\n    " + (r.stdout.strip() or r.stderr.strip()).replace("\n", "\n    "))
    print("  自检通过（干净子进程）：Sandbox.checkpoint / CheckpointInfo.mem_mode / "
          "proto mem_mode / 六个方法 / 端口 49984 / 无死 Authorization 头")
    return



def do_install(sp, force):
    ver = installed_version()
    print("site-packages: %s" % sp)
    print("已装的 e2b   : %s" % (ver or "<读不到版本>"))
    if ver != PINNED:
        msg = ("payload 是照着 e2b==%s 做的，装的却是 %s。整文件覆盖是版本锁死的，"
               "版本对不上会覆盖出一个坏包。" % (PINNED, ver))
        if not force:
            die(msg + "\n  要么 pip install e2b==%s，要么 --force 自己负责。" % PINNED)
        print("⚠ " + msg + "  （--force，继续）")

    if park_stale(sp):
        print("  把老的 %s 挪成 %s（同一个 proto 两份注册会让 e2b 整个 import 不了）"
              % (STALE, STALE_PARKED))

    files = payload_files()
    copied = backed = 0
    for rel in files:
        src = os.path.join(PAYLOAD, rel)
        dst = os.path.join(sp, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if rel in UPSTREAM_FILES and os.path.exists(dst):
            bak = dst + ".orig"
            if not os.path.exists(bak):
                shutil.copy2(dst, bak)
                backed += 1
        shutil.copy2(src, dst)
        copied += 1
    print("  铺了 %d 个文件，其中 %d 个上游文件是首次备份成 .orig" % (copied, backed))
    n = drop_pycache(os.path.join(sp, "e2b"))
    print("  清了 %d 个 __pycache__（不清会加载到旧字节码）" % n)
    verify(sp)
    print("✓ checkpoint/restore SDK 已装好")


def do_uninstall(sp):
    restored = removed = 0
    for rel in payload_files():
        dst = os.path.join(sp, rel)
        if rel in UPSTREAM_FILES:
            bak = dst + ".orig"
            if os.path.exists(bak):
                shutil.copy2(bak, dst)
                os.remove(bak)
                restored += 1
        elif os.path.exists(dst):
            os.remove(dst)
            removed += 1
    for d in ("e2b/checkpointd", "e2b/sandbox/checkpoint"):
        p = os.path.join(sp, d)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
    for rel in ("e2b/sandbox_sync/checkpoint.py", "e2b/sandbox_async/checkpoint.py"):
        p = os.path.join(sp, rel)
        if os.path.exists(p):
            os.remove(p)
    parked = os.path.join(sp, STALE_PARKED)
    if os.path.isdir(parked):
        os.rename(parked, os.path.join(sp, STALE))
        print("  把 %s 挪回 %s" % (STALE_PARKED, STALE))
    drop_pycache(os.path.join(sp, "e2b"))
    print("✓ 还原了 %d 个上游文件，删了 %d 个新增文件" % (restored, removed))


def do_check(sp):
    print("site-packages: %s" % sp)
    print("已装的 e2b   : %s（payload 针对 %s）" % (installed_version(), PINNED))
    missing = [r for r in payload_files() if not os.path.exists(os.path.join(sp, r))]
    if missing:
        print("未安装或不完整，缺 %d 个文件：" % len(missing))
        for r in missing[:10]:
            print("    " + r)
    else:
        print("payload 的 %d 个文件都在位" % len(payload_files()))
        verify(sp)


def main():
    if not os.path.isdir(PAYLOAD):
        die("找不到 payload 目录：%s" % PAYLOAD)
    sp = site_packages()
    if "--uninstall" in sys.argv:
        do_uninstall(sp)
    elif "--check" in sys.argv:
        do_check(sp)
    else:
        do_install(sp, "--force" in sys.argv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
