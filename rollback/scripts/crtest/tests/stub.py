# -*- coding: utf-8 -*-
"""
打桩的沙箱 + 打桩的 checkpoint 服务端：让 T11 / T21 能在**没有栈**的机器上整段跑一遍。

这不是"模拟服务端"，只是把用例真正读的那几样东西按服务端的规矩摆出来，用来抓
低级错误（拼错的字段名、越界、把 guest 命令发给一个已经 paused 的沙箱、
判定分支写反）：

  · `commands.run` 按命令里的 `echo k=v` 回一份 kv —— `Box.scene()` / `alive()`
    要的就是这个；`dirty()` 会把现场改掉，create 记下现场、restore 放回去，
    于是"现场逐项一致"这条判定在打桩里也是真在比；
  · `checkpoint.create/restore/list/delete` 按 `CHECKPOINT_FAULT_INJECT` 的四条
    注入回错，错误文案抄服务端原文（service.go / faults.go），异常类名叫
    `SandboxException`、文案前缀 `Code.<code>:` —— 和 SDK 今天的形状一样
    （`envd/rpc.py` 里没映射的 code 都长这样，reason 字段没有）；
  · manifest 按 store.go 的 Entry 字段写进 `<store>/<沙箱>/ckpt_*/manifest.json`，
    `CommitHidden` 的语义（hidden=true、base 跟着挪）也照着来 —— T21 的判定一半
    读的是这个。
"""

import json
import os
import re
import threading
import time


class SandboxException(Exception):
    """SDK 今天的样子：没映射的 code 只剩一句 `Code.<x>: <message>`。"""


def _err(code, message):
    return SandboxException("Code.%s: %s" % (code, message))


class _Res:
    def __init__(self, stdout):
        self.stdout = stdout


class _Info:
    def __init__(self, cid, name, mem_mode):
        self.checkpoint_id = cid
        self.name = name
        self.mem_mode = mem_mode
        self.created_at = int(time.time())


class FakeServer:
    """一个 orchestrator 的替身。

    `fault` = 启动时 env 里那一条（None = 没装），写法和服务端一样：`seal_move`
    是常开，`seal_move:once` 只炸一次 —— once 炸过之后注入就用光了，于是
    `seal_move` 的第二次 create 撞上的是 poison 本身（reason=rootfs_poisoned），
    `commit_late` 的第二次 create 则会成功并接着原来的链长。
    """

    def __init__(self, store, fault=None, op_seconds=0.2):
        self.store = store
        self.fault_label = fault or None       # 原样写进错误文案，带 `:once`
        name, _, mode = (fault or "").partition(":")
        self.fault = name or None
        self.fault_once = (mode == "once")
        self.poisoned = set()
        self.op_seconds = op_seconds
        self.entries = {}          # sandbox -> [manifest dict]（顺序 = 时间序）
        self.base = {}             # sandbox -> 下一次 create 的 parent（None = 还没有）
        self.torn = set()
        self.killed = set()
        self.paused = set()
        self.locks = {}

    # ---- 内部

    def lock(self, sid):
        return self.locks.setdefault(sid, threading.Lock())

    def _dir(self, sid, cid):
        d = os.path.join(self.store, sid, cid)
        os.makedirs(d, exist_ok=True)
        return d

    def _write(self, sid, m):
        with open(os.path.join(self._dir(sid, m["id"]), "manifest.json"), "w") as f:
            json.dump(m, f)
        self.entries.setdefault(sid, []).append(m)
        self.base[sid] = m["id"]           # publishLocked：提交（含 hidden）就挪 base

    def _fire(self):
        """注入放了一次电。`:once` 的放完就没了（服务端那边是个 sync.Once）。"""
        if self.fault_once:
            self.fault = None

    def _new(self, sid, name, hidden):
        parent = self.base.get(sid)
        return {"id": "ckpt_%d" % time.time_ns(), "name": name, "sandbox_id": sid,
                "state": "committed", "hidden": hidden,
                "parent_id": parent or "",
                "mem_mode": "incremental" if parent else "full"}

    # ---- API

    def create(self, sid, name, scene):
        if sid in self.killed:
            raise _err("not_found", "sandbox %s not found" % sid)
        with self.lock(sid):
            time.sleep(self.op_seconds)
            if sid in self.torn:
                raise _err("data_loss", "sandbox %s was left torn between two moments by a "
                                        "failed restore; it cannot be checkpointed or restored "
                                        "and must be destroyed and recreated" % sid)
            if self.fault == "seal_move":
                self._write(sid, self._new(sid, name, hidden=True))
                self.poisoned.add(sid)     # failCreate：epoch 推进了、层没记上 → PoisonRootfs
                label = self.fault_label
                self._fire()
                raise _err("internal", "failed to snapshot sandbox: fault injected: the sealed "
                                       "layer never reached the store "
                                       "(CHECKPOINT_FAULT_INJECT=%s)" % label)
            if self.fault == "commit_late":
                self._write(sid, self._new(sid, name, hidden=True))
                label = self.fault_label
                self._fire()
                raise _err("internal", "failed to record checkpoint: fault injected: commit "
                                       "failed after the artifacts were renamed into place "
                                       "(CHECKPOINT_FAULT_INJECT=%s)" % label)
            # poison 的检查在注入点之后（AppendLayer 里），所以常开时它永远轮不到，
            # 只有 `:once` 用光了注入才看得见 —— 这正是 T21 once 模式要验的那一条。
            if sid in self.poisoned:
                raise _err("internal", "cannot checkpoint sandbox %s: the rootfs bookkeeping is "
                                       "poisoned; checkpoints are refused until a restore "
                                       "reseeds the rootfs bookkeeping" % sid)
            m = self._new(sid, name, hidden=False)
            m["scene"] = dict(scene)
            self._write(sid, m)
            return _Info(m["id"], name, m["mem_mode"])

    def get(self, sid, cid):
        for m in self.entries.get(sid, []):
            if m["id"] == cid and not m["hidden"]:
                return m
        return None

    def restore(self, sid, cid):
        if sid in self.killed:
            raise _err("not_found", "sandbox %s not found" % sid)
        with self.lock(sid):
            time.sleep(self.op_seconds)
            if sid in self.torn:
                raise _err("data_loss", "sandbox %s was left torn between two moments by a "
                                        "failed restore" % sid)
            m = self.get(sid, cid)
            if m is None:
                raise _err("not_found", "checkpoint %s not found" % cid)
            if self.fault == "torn_assemble":
                self.torn.add(sid)
                self._fire()
                raise _err("data_loss", "restore failed past the commit point; the sandbox must "
                                        "be recreated: open %s/%s/fault-injected/torn_assemble: "
                                        "no such file or directory" % (self.store, sid))
            self.base[sid] = cid               # A1：账本跟着虚拟机走
            self.poisoned.discard(sid)         # 一次成功的 restore 重新播种 rootfs 账本
            if self.fault == "envd_timeout":
                label = self.fault_label
                self._fire()
                return ("rolled_back", m, _err("internal",
                        "the guest never answered after the rollback\nsandbox restored but envd "
                        "did not come back after 45s: fault injected: the guest never answered "
                        "after the rollback (CHECKPOINT_FAULT_INJECT=%s)" % label))
            return ("rolled_back", m, None)

    def list(self, sid):
        return [m for m in self.entries.get(sid, []) if not m["hidden"]]

    def delete(self, sid, cid):
        m = self.get(sid, cid)
        if m is None:
            raise _err("not_found", "checkpoint %s not found" % cid)
        self.entries[sid].remove(m)
        return True

    def kill(self, sid):
        with self.lock(sid):                   # R3：kill 等在途操作做完
            self.killed.add(sid)
            d = os.path.join(self.store, sid)
            for root, dirs, files in os.walk(d, topdown=False):
                for f in files:
                    os.remove(os.path.join(root, f))
                for x in dirs:
                    os.rmdir(os.path.join(root, x))
            if os.path.isdir(d):
                os.rmdir(d)


# T40 的长流命令：`for i in $(seq N); do echo $i; sleep 0.1; done`
_SEQ_RE = re.compile(r"for i in \$\(seq (\d+)\)")


class FakeCommands:
    def __init__(self, sbx):
        self.sbx = sbx

    def run(self, cmd, user=None, timeout=None, on_stdout=None, on_stderr=None):
        """T40 要的流式签名：认得 `seq N` 那条长流，把 N 行喂给 on_stdout。

        `server.stream_cut`（0 = 不截）让打桩造出「流跑到第 k 行就被掐断」——
        用例必须把它记成截断，而不是当成成功。"""
        m = _SEQ_RE.search(cmd)
        if m is not None and on_stdout is not None:
            n = int(m.group(1))
            cut = getattr(self.sbx.server, "stream_cut", 0)
            emit = n if not cut else min(n, cut)
            for i in range(emit):
                on_stdout(str(i + 1))
            if cut and emit < n:
                raise SandboxException(
                    "RemoteProtocolError: peer closed connection without sending "
                    "complete message body (incomplete chunked read)")
            return _Res("")
        return _Res(self.sbx._run(cmd))


class FakeCheckpoint:
    def __init__(self, sbx):
        self.sbx = sbx

    def create(self, name=None, request_timeout=None):
        return self.sbx.server.create(self.sbx.sandbox_id, name, self.sbx.scene)

    def restore(self, checkpoint_id, request_timeout=None):
        _, m, err = self.sbx.server.restore(self.sbx.sandbox_id, checkpoint_id)
        self.sbx.scene = dict(m.get("scene") or {})
        if err is not None:
            raise err
        return True

    def list(self, request_timeout=None):
        return [_Info(m["id"], m.get("name"), m.get("mem_mode"))
                for m in self.sbx.server.list(self.sbx.sandbox_id)]

    def delete(self, checkpoint_id, request_timeout=None):
        return self.sbx.server.delete(self.sbx.sandbox_id, checkpoint_id)


class FakeSandbox:
    _n = 0

    def __init__(self, server, sandbox_id=None):
        FakeSandbox._n += 1
        self.server = server
        self.sandbox_id = sandbox_id or "fake%03d" % FakeSandbox._n
        self.commands = FakeCommands(self)
        self.checkpoint = FakeCheckpoint(self)
        self.scene = {"mem_gen": "init", "file_gen": "init", "mem_md5": "m0",
                      "file_md5": "f0", "hb_pid": "101"}
        self._hb = 10
        self.files = {}              # T34：假的 guest 文件大小表（dd 写、stat 读）
        self.cold_mbps = 400.0       # T34：链深 0 的冷读吞吐
        self.cold_decay = 0.02       # T34：每加一层掉多少（judge_read_slowdown 的输入）

    # guest 侧：只认用例真正用到的那几条
    def _run(self, cmd):
        sid = self.sandbox_id
        if sid in self.server.killed:
            raise SandboxException("sandbox %s is not running" % sid)
        if sid in self.server.torn:
            raise SandboxException("Code.unavailable: sandbox is paused (torn)")
        import re
        m = re.search(r"echo (\S+) > /dev/shm/gen", cmd)
        if m:
            gen = m.group(1)
            self.scene.update({"mem_gen": gen, "file_gen": gen,
                               "mem_md5": "m-" + gen, "file_md5": "f-" + gen})
            return ""
        # T34：`dd ... of=<路径> bs=1M count=N` 记一个假的文件大小，`stat -c %s` 读回来。
        m = re.search(r"\bdd .*?of=(\S+).*?\bcount=(\d+)", cmd)
        if m:
            path, count = m.group(1), int(m.group(2))
            seek = re.search(r"\bseek=(\d+)", cmd)
            end = ((int(seek.group(1)) if seek else 0) + count) * 1048576
            if "notrunc" in cmd:
                self.files[path] = max(self.files.get(path, 0), end)
            else:
                self.files[path] = count * 1048576
            return ""
        m = re.search(r"stat -c %s (\S+)", cmd)
        if m:
            return "%d\n" % self.files.get(m.group(1), 0)
        # T34：冷读器。吞吐按链深（这个沙箱盘上有几层）线性掉一点，好让
        # judge_read_slowdown 的分支在离线单测里也真的被走到。
        m = re.search(r"python3 \S*t34_read\S* (\S+) (\d+) (\d+) (\d+) (\d+)", cmd)
        if m:
            path, mb, nrand = m.group(1), int(m.group(2)), int(m.group(3))
            depth = len(self.server.entries.get(sid, []))
            size = self.files.get(path, mb * 1048576)
            mbps = self.cold_mbps / (1.0 + self.cold_decay * depth)
            got_mb = min(mb, size / 1048576.0)
            return ("size_mb=%.1f\nread_mb=%.1f\nseq_s=%.4f\nmbps=%.2f\nrand_n=%d\n"
                    "p50_ms=%.3f\np99_ms=%.3f\nmax_ms=%.3f\ndropped=2\n"
                    % (size / 1048576.0, got_mb, got_mb / mbps if mbps else 0.0, mbps,
                       nrand, 0.2 * (1 + depth), 0.6 * (1 + depth), 1.0 * (1 + depth)))
        out = []
        for key in re.findall(r"echo (\w+)=", cmd):
            if key in self.scene:
                out.append("%s=%s" % (key, self.scene[key]))
            elif key == "cmd":
                out.append("cmd=alive")
            elif key == "rw":
                out.append("rw=n%s" % self._hb)
            elif key == "hb0":
                out.append("hb0=%d" % self._hb)
            elif key == "hb1":
                self._hb += 2
                out.append("hb1=%d" % self._hb)
            else:
                out.append("%s=?" % key)
        if "__rc=" in cmd:
            out.append("__rc=0")
        return "\n".join(out) + ("\n" if out else "")

    def kill(self):
        self.server.kill(self.sandbox_id)

    def beta_pause(self):
        with self.server.lock(self.sandbox_id):
            self.server.paused.add(self.sandbox_id)
        return True


class FakeLog:
    """`common.OrchLog` 的替身：日志目录当作读得到，`since()` 永远给一行（于是
    `common.wait_for_log()` 的轮询第一次就命中，打桩冒烟不会白等）。"""

    dir = "/fake/logs"

    def mark(self):
        pass

    def since(self, pattern=None):
        return ["fake log line matching %s" % pattern]


def install(common, server, monkey):
    """把 common 里那几个"要真机器才有"的入口换成打桩版。`monkey` 收回滚动作。"""
    orig = {name: getattr(common, name) for name in
            ("sandbox_create", "connect_box", "checkpoint_store", "netns_count",
             "netns_count_steady", "orchestrator_uptime_s", "fc_processes", "OrchLog")}

    def sandbox_create(template, private=True, timeout=3600):
        return FakeSandbox(server)

    def connect_box(ctx, sandbox_id, label):
        if sandbox_id in server.killed:
            raise SandboxException("sandbox %s not found" % sandbox_id)
        return common.Box(FakeSandbox(server, sandbox_id), ctx.store, label)

    common.sandbox_create = sandbox_create
    common.connect_box = connect_box
    common.checkpoint_store = lambda: (server.store, "ext4", None)
    common.netns_count = lambda: 42
    # 打桩里不等稳定：netns_count_steady 真会连采 20 s，整套单测会白等
    common.netns_count_steady = lambda **kw: 42
    # 打桩机器上没有真的 orchestrator：当成「早就起来了」，netns 判定不降级
    common.orchestrator_uptime_s = lambda *a, **kw: 99999.0
    common.fc_processes = lambda sid: []
    common.OrchLog = FakeLog
    monkey.append(lambda: [setattr(common, k, v) for k, v in orig.items()])
