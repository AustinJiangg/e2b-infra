# -*- coding: utf-8 -*-
"""
T38 FC 侧 faulted 路径（D2）。

Firecracker 侧新增了一个**测试专用** cargo feature `rollback-fault-inject`
（KASandbox jll `fddd65d`）。装了这个 feature 的 FC 读环境变量
`FC_ROLLBACK_FAULT_INJECT=post_commit`，在 rollback 的 Phase 5 **之后**人为失败，
把 VM 置成 `Faulted` 并回 **HTTP 500 + `{"fault":true}`**，日志里留一行
`injected on purpose by FC_ROLLBACK_FAULT_INJECT`。

这条路平时够不着：它是「过了 commit point 才炸」的那一类 —— 内存/盘已经改了一半，
谁也回不去。本用例把服务端对它的**既有**处理实机验一遍。

期望全部从代码读出，不是猜的（infra-arm jll `bdcf17adb` 的 worktree
`tmp/wt-jll/`，SDK 是 e2b-arm jll）：

  · `internal/sandbox/fc/rollback.go:202-209` —— 响应体里 `fault` 字段是**契约**，
    状态码只需不矛盾；`*fault == true` → `RollbackFaultedError`，文案
    `rollback failed past the commit point, VM is faulted: <FC 的 fault_message>`。
  · `internal/sandbox/checkpoint.go:447-449` —— 外面包一层
    `RollbackTornError`，文案 `rollback left the sandbox torn between two moments: ...`。
  · `internal/checkpoint/service.go:1018-1029` —— restore 捕到 torn 后调
    `markTorn()` 并 `writeError(500, "data_loss", reasonTorn, "restore failed past
    the commit point; the sandbox must be recreated: ...")`。
  · SDK `e2b/sandbox/checkpoint/errors.py` 的 `_CHECKPOINT_REASON_MAP` 把
    `torn` 映到 **`CheckpointTornException`**（`e2b/exceptions.py:186-197`，
    `_default_reason = "torn"`）。

**注意三条与直觉相反、但代码写死了的后置状态**（service.go:640-656 的 `markTorn`
与 1021-1025 的注释）：`markTorn` 只做三件事 —— `InvalidateBase` / `PoisonRootfs` /
把沙箱记进 `s.torn`，全在内存里。它**不 kill FC、不把沙箱移出 sandbox.Map、不释放
netns、不删 `store/<沙箱>/` 目录**；注释明写 VM 是**故意**保持 paused 的、盘视图也
故意不解绑。`store/<沙箱>/` 只有沙箱真正被 `OnRemove` 回收时才删
（store.go:1276-1295）。所以本用例把这三条按「仍在」判，判「已清」会永远失败。

`is_running` 同理：SDK 探的是 checkpoint 服务自己的 `/health`（service.go:290-291、
310-314），由 orchestrator 直接回 200，根本不碰 guest —— torn 之后它**仍然为真**。
真正「用不了」体现在 guest 侧命令上（VM 永远 paused），那条按记录项走并带超时上限。

判定的秤：

  · **SDK 异常对象**：类名必须是 `CheckpointTornException`、`.reason == "torn"`、
    `reason_src == "field"`（退化到 guess 说明 SDK 没把 reason 带出来）；
  · **错误文案**：三层文案必须一路带上来（`faulted` / `torn between two moments` /
    `must be recreated`），这是「确实是 FC 判 faulted 打上来的」而不是别的 torn；
  · **后续调用**：再 create / 再 restore 都被 `refuseIfTorn`（service.go:773-784）
    打回同一个 `CheckpointTornException`，文案 `was left torn between two moments`；
  · **服务端日志**：`markTorn` 的 Error 行 `the sandbox is torn and must be
    recreated`（service.go:654）+ restore defer 的 Warn 行 `checkpoint restore
    failed`（service.go:918-933）；
  · **FC 日志**（FC 的 stdout 被 orchestrator 接进同一份 nomad 日志）：
    `injected on purpose by FC_ROLLBACK_FAULT_INJECT`。

`fc_vcpu_*` 四个字段在这条路上**预期不出现**：`metrics.go:97-108` 的
`vcpuCounterFields` 遇 nil 返回 nil，而 `checkpoint.go:449` 判 faulted 时返回的
counters 就是 nil（FC 压根没把 `vcpu_counters` 发回来）。所以这一条按**记录项**走，
把实际看到的记下来 —— 「有」才是要报给主会话的意外。

**用例只检测、不装注入**：注入是 FC 启动时读一次 env 的事，而 FC 继承 orchestrator
的 task env。前置不满足以**退出码 3** 退出（沿用 T21/T37 的口径）。
"""

NAME = "T38"

import os
import time

from .. import common
from ..common import Unmet, expect, log, note

ENV_NAME = "FC_ROLLBACK_FAULT_INJECT"
FAULTS = ["fc_post_commit"]

HOWTO = """注入版 FC（带 cargo feature `rollback-fault-inject`）要同时满足两件事：
      1) tmp/fc-versions/v1.13.1/firecracker 换成注入版
         （tmp/fc-build-out/fc-ext4-1c6a428-faultinject，sha 前 16 位 d722eee5b0a8d068）；
      2) orchestrator 的 task env 带上 FC_ROLLBACK_FAULT_INJECT=post_commit
         —— FC 是 orchestrator 的子进程，整份 env 都继承过去：
      EXTRA_ENV="FC_ROLLBACK_FAULT_INJECT=post_commit" tmp/switch-stack.sh jll
    切栈要求活 FC 数为 0，切完 API 侧约 2 分钟才 ready。跑完务必把生产版 FC
    （fc-ext4-1c6a428）拷回去，再用不带 EXTRA_ENV 的 tmp/switch-stack.sh jll 换回来
    —— armed 的节点每一次 restore 都会把沙箱打成 torn。"""

BASIS = ("KASandbox jll fddd65d（cargo feature rollback-fault-inject）+ 1c6a428；"
         "infra-arm jll bdcf17adb：fc/rollback.go:202-209、sandbox/checkpoint.go:447-449、"
         "checkpoint/service.go:1018-1029 与 640-656、metrics.go:97-117；"
         "SDK e2b-arm jll errors.py 的 _CHECKPOINT_REASON_MAP")

FC_INJECT_MARK = "injected on purpose by FC_ROLLBACK_FAULT_INJECT"
VCPU_FIELDS = ("fc_vcpu_mmio_drained", "fc_vcpu_mmio_drain_failed",
               "fc_vcpu_readback_mismatch", "fc_vcpu_mmio_drained_pause_total")


def add_args(ap):
    ap.add_argument("--fault", default="fc_post_commit", choices=FAULTS,
                    help="测哪一条 FC 侧注入（目前只有 post_commit 一条）")
    ap.add_argument("--cmd-timeout", type=float, default=30.0,
                    help="torn 之后那次 guest 命令等多久算「用不了」（秒，只做记录项）")
    return ap


def _precheck(ctx):
    """orchestrator（也就是 FC 的父进程）的 environ 里必须有注入开关。"""
    pid, env = common.orchestrator_pid_env()
    ctx.results["meta"]["orchestrator_pid"] = pid
    val = env.get(ENV_NAME)
    if not pid:
        raise Unmet("读不到 orchestrator 进程 —— T38 的前置检查和一半判据都在宿主机上",
                    "在宿主机（920B / 950）上跑")
    if not val:
        raise Unmet("orchestrator 的 environ 里没有 %s —— 这个节点没装 FC 侧注入" % ENV_NAME,
                    HOWTO)
    if val != "post_commit":
        raise Unmet("装的是 %s=%s，本用例要的是 post_commit" % (ENV_NAME, val), HOWTO)
    log("  前置：orchestrator pid=%s 带 %s=%s（FC 继承整份 env）" % (pid, ENV_NAME, val))
    ctx.results["summary"]["T38"]["env"] = val
    return val


def _log_note(ctx, orch, pattern, name, want, basis, hard=True, timeout=None):
    if not orch.dir:
        note(ctx, name, want, "读不到 orchestrator 日志（不在宿主机上？）", basis)
        return []
    kw = {"timeout": timeout} if timeout is not None else {}
    lines, waited, reads = common.wait_for_log(orch, pattern, **kw)
    got = ("%s（等了 %.1f s、读了 %d 次）" % (lines[0][-220:], waited, reads) if lines
           else "日志里没有这一行（轮询等了 %.1f s、读了 %d 次）" % (waited, reads))
    if hard:
        expect(ctx, name, bool(lines), want, got, basis)
    else:
        note(ctx, name, want, got, basis)
    return lines


def run(ctx):
    ctx.results["summary"].setdefault("T38", {})
    ctx.stage("T38", fault=ctx.args.fault)
    _precheck(ctx)

    orch = common.OrchLog()
    box = common.spawn(ctx, 1, "t38-")[0]
    log("  沙箱 %s" % box.id)
    box.setup(warm_mem=32, warm_file=8)
    box.dirty("g0", mem_mb=16, file_mb=4)

    netns_before = common.netns_count()
    fc_before = common.fc_processes(box.id)
    log("  torn 之前：FC 进程 %s，netns %s" % (fc_before, netns_before))

    crec = ctx.op(box.create("g0"), stage="T38", step="create")
    if not crec.get("ok"):
        raise Unmet("注入装上了，但 create 就先失败了：%s" % crec.get("err"),
                    "FC 侧注入只在 rollback 路径上生效，create 不该被打回；"
                    "先确认切栈时没顺手带上 CHECKPOINT_FAULT_INJECT")
    ck = crec["id"]
    log("  checkpoint %s（mem_mode=%s）" % (ck, crec.get("mem_mode")))
    box.dirty("g1", mem_mb=16, file_mb=4)

    orch.mark()
    rrec = ctx.op(box.restore(ck, verify=False), stage="T38", step="restore-faulted")
    info = common.rec_error_info(rrec)
    ctx.results["summary"]["T38"]["restore_error"] = info

    if rrec.get("ok"):
        raise Unmet("restore 居然成功了 —— 这个节点上的 FC 不是注入版，或 env 没传到 FC",
                    HOWTO)

    msg = info.get("message") or ""

    expect(ctx, "restore 被 FC 的 faulted 打回，SDK 抛 CheckpointTornException",
           info.get("type") == "CheckpointTornException",
           "异常类 = CheckpointTornException",
           "异常类 = %s：%s" % (info.get("type"), msg[-200:]),
           "SDK errors.py `_CHECKPOINT_REASON_MAP[\"torn\"] = CheckpointTornException`")

    ok_reason, detail = common.reason_ok(info, "torn")
    expect(ctx, "reason 是 torn，且来自异常对象的字段（不是猜的）",
           ok_reason and info.get("reason_src") == "field",
           "reason=torn，出处 field",
           detail,
           "service.go:1018-1029 writeError(500, data_loss, reasonTorn)；"
           "exceptions.py:186-197 `_default_reason = \"torn\"`")

    expect(ctx, "Connect code 是 data_loss",
           str(info.get("code")) in ("data_loss", "Code.data_loss"),
           "code = data_loss", "code = %s" % info.get("code"),
           "service.go:1021 writeError 的第二个参数")

    for frag, why in (("faulted", "fc/rollback.go:78-80 `VM is faulted`"),
                      ("torn between two moments", "sandbox/checkpoint.go:302 的包装文案"),
                      ("must be recreated", "service.go:1022 writeError 的 message")):
        expect(ctx, "错误文案里带 `%s`" % frag, frag in msg,
               "message 含 %s" % frag, "message=%s" % msg[-220:], why)

    # ---- 服务端 / FC 日志
    _log_note(ctx, orch, FC_INJECT_MARK.replace("(", r"\("),
              "FC 日志里有那行「故意注入」",
              "FC 打出 %s" % FC_INJECT_MARK,
              "KASandbox jll fddd65d；FC 的 stdout 被 orchestrator 接进同一份 nomad 日志")
    _log_note(ctx, orch, r"the sandbox is torn and must be recreated",
              "orchestrator 打了 markTorn 那行 Error",
              "Error `restore failed past the commit point; the sandbox is torn and must be recreated`",
              "service.go:654")
    fail_lines = _log_note(ctx, orch, r"checkpoint restore failed",
                           "orchestrator 打了 restore 收尾那行 Warn",
                           "Warn `checkpoint restore failed`，带 sandbox.id / checkpoint_id / timings_ms",
                           "service.go:918-933 的 defer")

    line = fail_lines[0] if fail_lines else ""
    present = [f for f in VCPU_FIELDS if ('"%s"' % f) in line]
    note(ctx, "失败那行带不带 fc_vcpu_* 四件套",
         "预期**一个都不带**：FC 判 faulted 时 RollbackInPlace 返回的 counters 是 nil，"
         "metrics.go 的 vcpuCounterFields 遇 nil 返回 nil（四个字段一起有或一起没有）",
         "出现的字段 = %s" % (present or "无"),
         "sandbox/checkpoint.go:449 返回 nil counters；metrics.go:97-108")
    ctx.results["summary"]["T38"]["vcpu_fields_on_failure"] = present

    # ---- 后续调用：refuseIfTorn
    crec2 = ctx.op(box.create("g2", record_scene=False), stage="T38", step="create-after-torn")
    info2 = common.rec_error_info(crec2)
    expect(ctx, "torn 之后再 create 被 refuseIfTorn 打回",
           (not crec2.get("ok")) and info2.get("type") == "CheckpointTornException"
           and "was left torn between two moments" in (info2.get("message") or ""),
           "CheckpointTornException，文案带 `was left torn between two moments`",
           "居然成功了" if crec2.get("ok") else "%s：%s" % (info2.get("type"),
                                                          (info2.get("message") or "")[-200:]),
           "service.go:773-784 refuseIfTorn，create 调用点 service.go:412")

    rrec2 = ctx.op(box.restore(ck, verify=False), stage="T38", step="restore-after-torn")
    info3 = common.rec_error_info(rrec2)
    expect(ctx, "torn 之后再 restore 也被 refuseIfTorn 打回",
           (not rrec2.get("ok")) and info3.get("type") == "CheckpointTornException",
           "CheckpointTornException",
           "居然成功了" if rrec2.get("ok") else "%s：%s" % (info3.get("type"),
                                                          (info3.get("message") or "")[-200:]),
           "service.go:773-784 refuseIfTorn，restore 调用点 service.go:901")

    lrec = ctx.op(box.list_rec(), stage="T38", step="list-after-torn")
    note(ctx, "torn 之后 list 还能用",
         "refuseIfTorn 只挡 create / restore，list / delete 不挡（service.go:770-772 的注释）",
         "list ok=%s ids=%s" % (lrec.get("ok"), lrec.get("ids") or lrec.get("err")),
         "service.go:770-772")

    # ---- 后置资源：按代码判，不是按直觉判
    store_dir = box.store_dir()
    store_there = os.path.isdir(store_dir) if ctx.store else None
    fc_after = common.fc_processes(box.id)
    netns_after = common.netns_count()

    expect(ctx, "torn 之后 FC 进程**仍在**（markTorn 不 kill 它）",
           bool(fc_after),
           "FC 进程还活着，VM 保持 paused",
           "FC 进程 = %s（torn 前 %s）" % (fc_after, fc_before),
           "service.go:640-645 markTorn 的注释「The VM stays paused on purpose」；"
           "全仓没有 markTorn → kill/Remove 的调用")

    if ctx.store:
        expect(ctx, "torn 之后 store 目录**仍在**（只有沙箱被回收才删）",
               store_there, "%s 还在" % store_dir,
               "存在 = %s" % store_there,
               "store.go:1276-1295 RemoveSandbox 的 os.RemoveAll 只在 OnRemove 上")

    note(ctx, "netns 计数前后",
           "markTorn 不释放 netns，所以计数不该减少（沙箱还在）",
           "torn 前 %s → 后 %s" % (netns_before, netns_after),
           "全仓没有 markTorn → netns 释放的调用")

    try:
        running = box.sbx.is_running()
    except Exception as e:              # noqa: BLE001
        running = "抛了 %s: %s" % (type(e).__name__, e)
    note(ctx, "torn 之后 is_running",
         "预期**仍为真**：SDK 探的是 checkpoint 服务自己的 /health，orchestrator 直接回 200，不碰 guest",
         "is_running = %r" % (running,),
         "service.go:290-291 路由、310-314 处理")

    t0 = time.monotonic()
    try:
        out = box.run("echo still-alive", timeout=ctx.args.cmd_timeout)
        cmd_state = "命令返回了：%r（%.1f s）" % (out[:60], time.monotonic() - t0)
    except Exception as e:              # noqa: BLE001
        cmd_state = "%s: %s（%.1f s）" % (type(e).__name__, str(e)[:160], time.monotonic() - t0)
    note(ctx, "torn 之后 guest 侧命令",
         "VM 永远停在 paused，所以命令要么超时要么报连接错；代码没有为这条路写明确返回，"
         "这一条只做记录，不判定",
         cmd_state,
         "service.go:1021-1025：盘视图故意不解绑、VM 故意保持 paused（推断部分：超时形态）")
    ctx.results["summary"]["T38"].update(
        {"sandbox": box.id, "checkpoint": ck,
         "fc_before": fc_before, "fc_after": fc_after,
         "netns_before": netns_before, "netns_after": netns_after,
         "store_dir_exists": store_there, "is_running": running,
         "cmd_after_torn": cmd_state})
