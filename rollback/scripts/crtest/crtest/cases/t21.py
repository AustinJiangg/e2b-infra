# -*- coding: utf-8 -*-
"""
T21 故障注入（方案 §4.2）。

服务端第 2 轮给了测试钩子 `CHECKPOINT_FAULT_INJECT`（infra-arm jll ed03fe57c，
`checkpoint/faults.go`），把四条平时够不着的失败路径挪到手边。四个子用例各自独立
跑，一次一个（`--fault envd_timeout|torn_assemble|seal_move|commit_late`），因为
注入是 orchestrator 启动时读一次 env 的事，一次只可能装一个。

**用例不负责装注入，只负责检测**：跑一次对应操作，看错误里有没有
`fault injected:`（`torn_assemble` 是拿一个进不去的路径
`<store>/<沙箱>/fault-injected/torn_assemble` 触发的，文案里带 `fault-injected`）。
检测不到就以**退出码 3（前置不满足）**退出并打印怎么设 env —— 判 1 会把"没装钩子"
和"钩子坏了"混成一件事。

判定的"秤"有两把：

  · **服务端日志**：只在实现里**真有**那一行时才硬判（`envd_timeout` 的
    `rollback succeeded but the guest's envd never answered`、`torn_assemble` 的
    `restore failed past the commit point`、`seal_move` 的 `sealing the write layer
    failed`）。`commit_late` 的 Commit 失败分支只写响应体、不打日志，那条就改判
    客户端拿到的 message，日志侧退成 `note`（`_log_expect(..., hard=False)`）。
  · **客户端**：HTTP 状态码 + 错误体的 `code` / `reason`（43b1c2456 的 reason 表）。
    SDK 侧的异常层次正在同步实现，本用例**不依赖**它：`common.error_info()` 先看
    异常对象上有没有 `reason` 字段，没有就退回 `Code.<x>` + message 子串，并把
    出处（`reason_src`）记进 JSON —— "SDK 有无 reason 字段"本身就是记录项。
  · **盘上的 manifest**：`<store>/<沙箱>/ckpt_*/manifest.json`（store.go 的 Entry）。
    注入是靠 env 常开的，**每一次**对应操作都会被打回，客户端因此看不到后置状态；
    而 `parent_id` / `hidden` / `state` / `mem_mode` 全在 manifest 里，A1 的
    "下一次 create 挂在 restore 目标下"、A3 的"链没被判 invalid"都靠它判。

因此有两条后置状态在 env 常开时**原理上看不到**，用例只把观察到的记下来并说明：

  · `seal_move` 的 `reason=rootfs_poisoned`：poison 是 AppendLayer 才检查的，而注入点
    在它**之前**（service.go:483 一带），armed 时每次 create 都停在注入点；poison 又
    只活在内存里，重启 orchestrator 摘 env 的同时它也没了。
  · `seal_move` / `commit_late` 的"之后 create 恢复可用"：同一个原因。

要把这两条也测上，服务端需要一个"只炸一次"的注入（`CHECKPOINT_FAULT_INJECT=<名字>:once`）
或运行期可切的开关 —— 服务端已经给了 `:once`，**用例两种模式都支持**：

  · 模式是**测出来的**，不是参数：`_inject_mode()` 从第一次被打回的错误文案里读
    `(CHECKPOINT_FAULT_INJECT=<名字>[:once])`。文案里没写模式就按常开办（保守的一侧：
    真是 once 的话第二次操作会成功，用例当场报出来，不会悄悄放过）。
  · **常开**（`=commit_late`）：每一次对应操作都被打回，验的是"失败之后账本没坏"——
    隐藏条目、链没断、list 语义、隐藏条目不能当 restore 目标。
  · **只炸一次**（`=commit_late:once`）：第一次打回，之后注入就用光了，验的是**自愈路径**：
    `commit_late` 的第二次 create **成功**、`mem_mode=incremental`、`parent_id` = 刚才那个
    隐藏条目（链真的能接着长）；`seal_move` 的第二次 create **仍被拒，但 reason 变成
    `rootfs_poisoned`** —— 挡住它的不再是注入而是 poison 本身，这正是常开时看不到的那条。
    （`seal_move` 的 poison 要一次成功的 restore 才能重新播种，而 armed 过的沙箱一个可见
    checkpoint 都没有，所以"之后 create 恢复可用"仍然测不了，还是记成 note。）
"""

NAME = "T21"

import os
import re

from .. import common
from ..common import Unmet, expect, log, note

FAULTS = ["envd_timeout", "torn_assemble", "seal_move", "commit_late"]

HOWTO = """在 orchestrator 的 nomad job 里给 task 加 env，然后重跑 job：
      jq '.Job.TaskGroups[0].Tasks[0].Env.CHECKPOINT_FAULT_INJECT="%s"' \\
         e2b-repo/tmp/stack/job-tm-<栈>.json > /tmp/job-fault.json
      source /opt/e2b-infra/.env; NOMAD_TOKEN=$NOMAD_ACL_TOKEN nomad job run -detach -json /tmp/job-fault.json
    起来以后 orchestrator 会打一行 "checkpoint fault injection is armed"；API 节点
    ready 大约要 2 分钟（920B 网络槽位那条经验）。跑完用 tmp/switch-stack.sh <栈>
    把 job 换回去 —— armed 的节点会故意把 checkpoint 打回失败。"""

BASIS = "方案 §4.2 T21；infra-arm jll ed03fe57c faults.go；错误 reason 表见 43b1c2456"


def add_args(ap):
    ap.add_argument("--fault", required=True, choices=FAULTS,
                    help="测哪一条注入（服务端一次只可能装一条）")
    return ap


# ---------------------------------------------------------------- 小工具

def _boot(ctx, label="t21-"):
    box = common.spawn(ctx, 1, label)[0]
    log("  沙箱 %s" % box.id)
    box.setup(warm_mem=32, warm_file=8)
    box.dirty("g0", mem_mb=16, file_mb=4)
    return box


def _manifests(ctx, box):
    """盘上的 manifest 列表（按时间序）。不在宿主机上跑不了这个用例。"""
    ms = common.read_manifests(ctx.store, box.id)
    ctx.op({"op": "scan", "box": box.label, "sandbox": box.id, "stage": "T21",
            "manifests": [{k: m.get(k) for k in
                           ("id", "state", "hidden", "parent_id", "mem_mode")} for m in ms]})
    return ms


def _need_fault(ctx, rec, fault, step):
    """检测这一步是不是真被注入打回的。不是就 3 号退出。返回 error_info。"""
    if rec.get("ok"):
        raise Unmet("服务端没有装 %s 注入：%s 居然成功了" % (fault, step),
                    HOWTO % fault)
    info = common.rec_error_info(rec)
    if not info["fault_injected"]:
        raise Unmet("%s 失败了，但错误里没有 `fault injected:` —— 不是注入，是真出事了：%s"
                    % (step, info["message"]), HOWTO % fault)
    # 注入的文案里带着自己的名字（faults.go 的 `injectedError` 把
    # `CHECKPOINT_FAULT_INJECT=<名字>` 写进去了；torn_assemble 是那条
    # `fault-injected/torn_assemble` 的路径）。seal_move 与 commit_late 都让
    # create 回同一个 500，不比对名字就会把"装错了注入"当成测过了。
    if fault not in info["message"]:
        raise Unmet("服务端装的是别的注入，不是 %s：%s" % (fault, info["message"]),
                    HOWTO % fault)
    return info


def _err_info(rec):
    """这一步的错误分型。记录时（`Box` 的 except / `ctx.op`）已经从异常对象上把
    reason / status 抠好了，这里直接取，别再从字符串重新解析一遍。"""
    return common.rec_error_info(rec)


def _record_reason_source(ctx, infos):
    """把"SDK 到底能给出什么"记下来（需求书的记录项）。"""
    srcs = sorted({i.get("reason_src") or "无" for i in infos if i})
    has_field = "field" in srcs
    note(ctx, "SDK 异常里有没有 reason 字段",
         "有就直接用，没有就按 code + message 子串退化",
         "reason 出处 = %s（SDK %s reason 字段）" % ("、".join(srcs),
                                                    "已有" if has_field else "还没有"),
         "e2b_connect/client.py make_error 目前只取 code 与 message")
    ctx.results["summary"].setdefault("T21", {})["reason_src"] = srcs
    ctx.results["summary"]["T21"]["sdk_has_reason_field"] = has_field


def _scene_same(box, ck):
    want = box.scenes.get(ck, {})
    now = box.scene()
    bad = {k: (want.get(k), now.get(k)) for k in want if now.get(k) != want.get(k)}
    return (not bad), (bad or "五项全同")


def _log_expect(ctx, orch, pattern, name, want, basis, timeout=None, interval=None,
                hard=True):
    """orchestrator 日志里应当出现的那一行。读不到日志就退化成记录项。

    `hard=False`：只把 grep 的结果记下来，不判定 —— 给"服务端在这条路上本来就没有
    对应日志行"的情况用（硬断言会永远失败，见 commit_late）。

    nomad 的 logmon 批量刷盘（第 4 轮验收实测滞后 0.05–1.3 s、批间隔约 1.6 s），
    操作一返回就读多半是空的 —— 直接断言就是竞态误判。所以这里**轮询重读**：
    默认最长等 `common.LOG_WAIT_TIMEOUT`（5 s）、每 `common.LOG_WAIT_INTERVAL`
    （0.5 s）重读一次，命中即返回；等满了才判失败，并把等了多久、读了几次写进
    「实际」里。
    """
    if not orch.dir:
        note(ctx, name, want, "读不到 orchestrator 日志（不在宿主机上？）", basis)
        return []
    kw = {}
    if timeout is not None:
        kw["timeout"] = timeout
    if interval is not None:
        kw["interval"] = interval
    lines, waited, reads = common.wait_for_log(orch, pattern, **kw)
    if lines:
        got = "%s（等了 %.1f s、读了 %d 次）" % (lines[0][-160:], waited, reads)
    else:
        got = "日志里没有这一行（轮询等了 %.1f s、读了 %d 次）" % (waited, reads)
    if hard:
        expect(ctx, name, bool(lines), want, got, basis)
    else:
        note(ctx, name, want, got, basis)
    return lines


# faults.go 的 `injectedError` 把 `CHECKPOINT_FAULT_INJECT=<名字>[:once]` 原样写进
# 错误文案，所以第一次被打回时就知道服务端装的是哪种模式。
_MODE_RE = re.compile(r"CHECKPOINT_FAULT_INJECT=([a-z_]+)(?::([a-z]+))?")


def _inject_mode(info, fault):
    """这条注入是"常开"还是"只炸一次"：返回 `"always"` / `"once"`。

    只认属于 `fault` 自己的那一段（`seal_move` 与 `commit_late` 回同一个 500，
    文案里认错名字就会按错的期望表判）。文案里没写模式 —— 老服务端，或
    `torn_assemble` 那条走路径触发的 —— 一律按 `"always"`：那是保守的一侧，
    真是 once 的话第二次操作会成功，用例会当场把它报成失败，而不是悄悄放过。
    """
    for m in _MODE_RE.finditer(info.get("message") or ""):
        if m.group(1) == fault:
            return "once" if (m.group(2) or "").lower() == "once" else "always"
    return "always"


def _note_mode(ctx, fault, mode):
    note(ctx, "服务端装的注入模式",
         "once = 只炸一次（验自愈路径）；always = 常开（验失败之后账本没坏）",
         "%s（文案里的 CHECKPOINT_FAULT_INJECT=%s%s）"
         % (mode, fault, ":once" if mode == "once" else ""),
         "faults.go injectedError；两种模式的期望表见模块头")
    ctx.results["summary"]["T21"]["inject_mode"] = mode
    return mode


# ---------------------------------------------------------------- envd_timeout

def envd_timeout(ctx):
    """回滚成功、guest 再也不答话（A1/F4）。

    判据：restore 回 500 `reason=guest_unresponsive`；**账本必须已经跟着虚拟机
    走到目标**——所以随后再 create 一次，它的 `parent_id` 应当等于刚才那次失败
    restore 的目标（这就是 A1：账本跟 hypervisor 走，不跟 RPC 的成败走）。
    """
    box = _boot(ctx)
    orch = common.OrchLog()
    infos = []

    rec = ctx.op(box.create("cp0"), stage="T21", step="cp0")
    expect(ctx, "建 cp0", rec.get("ok"), "成功", rec.get("err") or "ok",
           "T21 前置：create 路径不在这条注入上")
    cp0 = rec["id"]

    box.dirty("g1", mem_mb=16, file_mb=4)
    rec = ctx.op(box.create("cp1"), stage="T21", step="cp1")
    expect(ctx, "建 cp1", rec.get("ok"), "成功", rec.get("err") or "ok", "T21 前置")
    cp1 = rec["id"]

    rr = ctx.op(box.restore(cp0, verify=False), stage="T21", step="restore-cp0")
    info = _need_fault(ctx, rr, "envd_timeout", "restore")
    infos.append(info)
    ok, detail = common.reason_ok(info, "guest_unresponsive")
    expect(ctx, "restore 回 guest_unresponsive", ok,
           "500 internal，reason=guest_unresponsive", detail, BASIS)

    ok, detail = _scene_same(box, cp0)
    expect(ctx, "回滚其实已经落地（现场 = cp0）", ok, "五项全同", detail,
           "A1：RPC 失败但 hypervisor 那半已经成功")

    rec = ctx.op(box.create("cp2"), stage="T21", step="cp2")
    expect(ctx, "失败 restore 之后 create 仍然成功", rec.get("ok"), "成功",
           rec.get("err") or "ok", "A1：账本没被这次失败搞坏")
    cp2 = rec["id"]

    ms = {m.get("id"): m for m in _manifests(ctx, box)}
    parent = (ms.get(cp2) or {}).get("parent_id")
    expect(ctx, "新 checkpoint 的 parent = 刚才 restore 的目标", parent == cp0,
           "parent_id = %s（cp0）" % cp0,
           "parent_id = %s%s" % (parent, "（= cp1，账本没跟着虚拟机走）" if parent == cp1 else ""),
           "A1 判据；manifest = store.go Entry.ParentID")

    _log_expect(ctx, orch, r"envd never answered|rollback succeeded but",
                "服务端日志留下了 guest 没答话的痕迹",
                "一行 rollback succeeded but the guest's envd never answered",
                "service.go finishRestore 的 Error 日志")

    # restore 到新 checkpoint：注入还在，所以这次照样回 guest_unresponsive，
    # 但内容必须是 cp2 那一刻的 —— 这就是"restore 到新 checkpoint 内容正确"。
    box.dirty("g3", mem_mb=16, file_mb=4)
    rr2 = ctx.op(box.restore(cp2, verify=False), stage="T21", step="restore-cp2")
    info2 = _err_info(rr2)
    infos.append(info2)
    ok, detail = common.reason_ok(info2, "guest_unresponsive")
    expect(ctx, "restore 到新 checkpoint 也只坏在 envd 这一步", ok,
           "500 internal，reason=guest_unresponsive", detail, BASIS)
    ok, detail = _scene_same(box, cp2)
    expect(ctx, "restore 到新 checkpoint 内容正确", ok, "现场 = 拍 cp2 那一刻", detail,
           "方案 §4.2 T21")

    ms = _manifests(ctx, box)
    note(ctx, "盘上的 checkpoint 条目", "3 个（cp0/cp1/cp2），没有多余目录",
         "%d 个：%s" % (len(ms), [m.get("id") for m in ms]), "无目录泄漏（F7）")
    _record_reason_source(ctx, infos)
    ctx.results["summary"]["T21"].update(
        {"fault": "envd_timeout", "checkpoints": [m.get("id") for m in ms],
         "parent_of_new": parent, "target_of_failed_restore": cp0})


# ---------------------------------------------------------------- torn_assemble

def torn_assemble(ctx):
    """回滚提交点之后磁盘视图拼不起来 —— 沙箱被撕成两个时刻（F2）。

    撕了之后**虚拟机是故意停在 paused 的**（markTorn 不 resume），所以从这里起
    一律不碰 guest：`record_scene=False`，也不调 `alive()` / `scene()`，
    否则测试自己会挂在那条命令上。
    """
    box = _boot(ctx)
    orch = common.OrchLog()
    infos = []

    rec = ctx.op(box.create("cp0"), stage="T21", step="cp0")
    expect(ctx, "建 cp0", rec.get("ok"), "成功", rec.get("err") or "ok", "T21 前置")
    cp0 = rec["id"]

    rr = ctx.op(box.restore(cp0, verify=False), stage="T21", step="restore-cp0")
    info = _need_fault(ctx, rr, "torn_assemble", "restore")
    infos.append(info)
    expect(ctx, "restore 回 data_loss/torn",
           info["code"] == "data_loss" and info["reason"] == "torn",
           "500 data_loss，reason=torn",
           "code=%s reason=%s（出处 %s）" % (info["code"], info["reason"], info["reason_src"]),
           BASIS)

    rec = ctx.op(box.create("after-torn", record_scene=False), stage="T21", step="create-after")
    info = _err_info(rec)
    infos.append(info)
    expect(ctx, "撕了之后 create 被拒",
           (not rec.get("ok")) and info["code"] == "data_loss" and info["reason"] == "torn",
           "500 data_loss，reason=torn",
           "居然成功了" if rec.get("ok") else "code=%s reason=%s" % (info["code"], info["reason"]),
           "F2：撕了的沙箱只能销毁重建")

    rr = ctx.op(box.restore(cp0, verify=False), stage="T21", step="restore-after")
    info = _err_info(rr)
    infos.append(info)
    expect(ctx, "撕了之后 restore 被拒",
           (not rr.get("ok")) and info["code"] == "data_loss" and info["reason"] == "torn",
           "500 data_loss，reason=torn",
           "居然成功了" if rr.get("ok") else "code=%s reason=%s" % (info["code"], info["reason"]),
           "F2")

    lrec = ctx.op(box.list_rec(), stage="T21", step="list-after")
    expect(ctx, "撕了之后 list 仍可用", lrec.get("ok") and cp0 in (lrec.get("ids") or []),
           "list 成功且还看得到 cp0", lrec.get("err") or lrec.get("ids"),
           "refuseIfTorn 只挡 create/restore，读树的两个接口留着给运维看")
    drec = ctx.op(box.delete(cp0), stage="T21", step="delete-after")
    expect(ctx, "撕了之后 delete 仍可用", drec.get("ok"), "成功",
           drec.get("err") or "ok", "同上")

    _log_expect(ctx, orch, r"the sandbox is torn|past the commit point",
                "服务端日志记下了这次撕裂",
                "一行 restore failed past the commit point; the sandbox is torn…",
                "service.go markTorn 的 Error 日志（service.go:623）")

    box.kill()
    fresh = common.spawn(ctx, 1, "t21-new-")[0]
    log("  重建沙箱 %s" % fresh.id)
    fresh.setup(warm_mem=32, warm_file=8)
    fresh.dirty("g0", mem_mb=16, file_mb=4)
    rec = ctx.op(fresh.create("cp0"), stage="T21", step="fresh-create")
    expect(ctx, "撕了一个沙箱之后新沙箱照样能 create", rec.get("ok"), "成功",
           rec.get("err") or "ok", "方案 §4.2 T21：服务端不因为一个沙箱撕了就坏掉")
    fresh_cp0 = rec["id"]

    # 注入还挂着，所以新沙箱的 restore 也会当场撕 —— 这就是 armed 下的正确行为。
    # 摘掉 env 之后这一步应当变成"restore 成功且现场一致"。
    rr = ctx.op(fresh.restore(fresh_cp0, verify=False), stage="T21", step="fresh-restore")
    info = _err_info(rr)
    infos.append(info)
    expect(ctx, "新沙箱的 restore 仍被注入撕（注入还挂着）",
           (not rr.get("ok")) and info["code"] == "data_loss" and info["reason"] == "torn",
           "500 data_loss，reason=torn（armed 下的预期）",
           "居然成功了" if rr.get("ok") else "code=%s reason=%s" % (info["code"], info["reason"]),
           "注入是 env 常开的：摘掉 env 之后这一步应当成功，那是换栈后的回归项")

    _record_reason_source(ctx, infos)
    ctx.results["summary"]["T21"].update({"fault": "torn_assemble", "torn_sandbox": box.id,
                                          "fresh_sandbox": fresh.id})


# ---------------------------------------------------------------- seal_move

def seal_move(ctx):
    """快照写完了，封好的写层却没进 store（A2/F4）。

    armed 时每一次 create 都停在注入点（它在 AppendLayer 之前），所以
    `reason=rootfs_poisoned` 和"之后恢复可用"这两条**原理上看不到**（模块头已说明）。
    能判的是：create 回 500、推进过的 epoch 被救成隐藏条目、隐藏条目不进 list、
    再 create 还是被拒，以及服务端日志里那行"checkpoints are refused until a
    restore reseeds the rootfs bookkeeping"——那是 poison 真被设上的直接证据。
    """
    box = _boot(ctx)
    orch = common.OrchLog()
    infos = []

    rec = ctx.op(box.create("cp0"), stage="T21", step="create-1")
    info = _need_fault(ctx, rec, "seal_move", "create")
    infos.append(info)
    mode = _note_mode(ctx, "seal_move", _inject_mode(info, "seal_move"))
    expect(ctx, "create 回 500", info["code"] in ("internal", "unknown"),
           "500（Connect code internal）",
           "code=%s：%s" % (info["code"], info["message"][-120:]), BASIS)

    ms = _manifests(ctx, box)
    expect(ctx, "失败的 create 留下了一个条目", len(ms) == 1,
           "1 个 checkpoint 目录", "%d 个：%s" % (len(ms), [m.get("id") for m in ms]),
           "A2：epoch 已经推进，那份 diff 是那一代页的唯一副本，不能丢")
    ok, detail = common.judge_hidden_rescue(ms[0] if ms else None)
    expect(ctx, "推进过的 epoch 被救成隐藏条目", ok, "state=committed 且 hidden=true",
           detail, "A2/F5：CommitHidden")

    lrec = ctx.op(box.list_rec(), stage="T21", step="list")
    expect(ctx, "隐藏条目不出现在 list 里",
           lrec.get("ok") and not (lrec.get("ids") or []),
           "list 成功且为空", lrec.get("err") or lrec.get("ids"),
           "store.go：Hidden 条目 List 不显示、Get 不返回")

    _log_expect(ctx, orch, r"sealing the write layer failed",
                "服务端把 rootfs 账本设成了 poisoned",
                "一行 sealing the write layer failed; checkpoints are refused…",
                "service.go failCreate：epochAdvanced && !layerRecorded → PoisonRootfs")

    # 两种模式的期望表（差别只在这一步）：
    #   always —— 注入常开，第二次 create 还是停在注入点，看到的仍是 internal，
    #             `rootfs_poisoned` 轮不到，只能记成 note；
    #   once   —— 注入已经用光，这次挡住 create 的就是 poison 本身，所以
    #             **仍被拒、但 reason 必须是 rootfs_poisoned** —— 那正是常开时
    #             原理上看不到的那一条（模块头 / README §7）。
    rec2 = ctx.op(box.create("cp0-again", record_scene=False), stage="T21", step="create-2")
    info2 = _err_info(rec2)
    infos.append(info2)
    expect(ctx, "第二次 create 仍被拒", not rec2.get("ok"), "失败",
           "居然成功了" if rec2.get("ok") else "code=%s reason=%s" % (info2["code"], info2["reason"]),
           "A2：账本可能少了一层，必须先由一次 restore 重新播种")
    if mode == "once":
        ok, detail = common.reason_ok(info2, "rootfs_poisoned")
        expect(ctx, "注入用光之后，拦住 create 的是 poison 本身", ok,
               "reason=rootfs_poisoned", detail,
               "A2/F4：epochAdvanced && !layerRecorded → PoisonRootfs；once 模式才看得到")
        note(ctx, "poison 之后 create 恢复可用",
             "要一次成功的 restore 重新播种才行",
             "测不了：armed 过的这个沙箱一个可见 checkpoint 都没有，没有 restore 目标",
             "见模块头：once 也解不开这一条，得靠运行期可切的开关")
    else:
        note(ctx, "第二次 create 的 reason",
             "once 模式下应当是 rootfs_poisoned；常开时注入点在 AppendLayer 之前，看到的是 internal",
             "code=%s reason=%s（出处 %s）" % (info2["code"], info2["reason"], info2["reason_src"]),
             "见模块头：poison 只活在内存里，重启摘 env 的同时它也没了")

    ms = _manifests(ctx, box)
    rr = ctx.op(box.restore((ms[0] if ms else {}).get("id", "ckpt_none"), verify=False),
                stage="T21", step="restore-hidden")
    info3 = _err_info(rr)
    infos.append(info3)
    expect(ctx, "隐藏条目不能当 restore 目标",
           (not rr.get("ok")) and bool(common.NOTFOUND_RE.search(info3["message"])),
           "404 not_found",
           "居然成功了" if rr.get("ok") else "code=%s：%s" % (info3["code"], info3["message"][-120:]),
           "store.go Get：Hidden 条目不是 restore 目标。armed 时一个可见 checkpoint 都建不出来，"
           "所以\"restore 到之前任一 checkpoint 成功\"要等摘掉 env 才测得了")

    _record_reason_source(ctx, infos)
    ctx.results["summary"]["T21"].update(
        {"fault": "seal_move", "mode": mode,
         "entries": [{k: m.get(k) for k in
                      ("id", "state", "hidden", "mem_mode")} for m in ms],
         "poison_visible": info2["reason"] == "rootfs_poisoned"})


# ---------------------------------------------------------------- commit_late

def commit_late(ctx):
    """产物都改名到位了，Commit 才失败（A3/F5）。

    判据：create 回 500，但 **checkpoint 目录还在**（隐藏条目），而且**链没断**——
    链断了服务端下一次会改走全量新根，所以第二次 create 在盘上留下的 manifest
    应当还是 `mem_mode=incremental`、`parent_id` 指着第一个隐藏条目。
    """
    box = _boot(ctx)
    orch = common.OrchLog()
    infos = []

    rec = ctx.op(box.create("cp0"), stage="T21", step="create-1")
    info = _need_fault(ctx, rec, "commit_late", "create")
    infos.append(info)
    mode = _note_mode(ctx, "commit_late", _inject_mode(info, "commit_late"))
    expect(ctx, "create 回 500", info["code"] in ("internal", "unknown"),
           "500（Connect code internal）",
           "code=%s：%s" % (info["code"], info["message"][-120:]), BASIS)

    ms = _manifests(ctx, box)
    expect(ctx, "checkpoint 目录还在", len(ms) == 1,
           "1 个 checkpoint 目录", "%d 个：%s" % (len(ms), [m.get("id") for m in ms]),
           "A3：产物已经改成最终名字，丢了就等于丢一代页")
    ok, detail = common.judge_hidden_rescue(ms[0] if ms else None)
    expect(ctx, "它被救成隐藏条目", ok, "state=committed 且 hidden=true", detail,
           "A3/F5：CommitHidden 的二次提交路径")

    lrec = ctx.op(box.list_rec(), stage="T21", step="list")
    expect(ctx, "隐藏条目不出现在 list 里",
           lrec.get("ok") and not (lrec.get("ids") or []),
           "list 成功且为空", lrec.get("err") or lrec.get("ids"), "store.go List")

    # 两种模式的期望表（差别只在第二次 create 的成败；"链没断"两种模式都判，
    # 判据同样是盘上第二条 manifest）：
    #   always —— 注入常开，第二次 create 照样停在 Commit，客户端只看得到 500，
    #             但盘上应当又多一条 incremental、挂在第一个隐藏条目下的条目；
    #   once   —— 注入已经用光，第二次 create **成功**，而且必须是增量、parent 是
    #             刚才那个隐藏条目 —— 这就是 once 要验的自愈路径：一次 Commit 失败
    #             只毁掉那一次调用，不毁掉这条链。
    box.dirty("g1", mem_mb=16, file_mb=4)
    rec2 = ctx.op(box.create("cp1", record_scene=(mode == "once")), stage="T21", step="create-2")
    info2 = _err_info(rec2)
    if mode == "once":
        expect(ctx, "注入用光之后第二次 create 成功", rec2.get("ok"), "成功",
               rec2.get("err") or "成功，id=%s mem_mode=%s" % (rec2.get("id"), rec2.get("mem_mode")),
               "A3 自愈路径：Commit 失败只毁掉那一次调用，不毁掉这条链")
        expect(ctx, "第二次 create 是增量（客户端侧）",
               rec2.get("mem_mode") in ("incremental", "?", None),
               "mem_mode=incremental（SDK 没这个字段时是 ?）",
               "mem_mode=%s" % rec2.get("mem_mode"),
               "A3；盘上的 manifest 下一条再判一遍，那才是硬判据")
    else:
        infos.append(info2)
        expect(ctx, "第二次 create 也在同一处失败（注入常开）", not rec2.get("ok"), "失败",
               "居然成功了" if rec2.get("ok") else "code=%s" % info2["code"], BASIS)

    ms = _manifests(ctx, box)
    first = ms[0] if ms else None
    second = ms[1] if len(ms) > 1 else None
    ok, want, got = common.judge_chain_incremental(first, second)
    expect(ctx, "链没被判 invalid（下一次 create 仍是增量）", ok, want, got,
           "A3：Commit 失败不许把链打断；断了的话服务端会改走全量新根（%s 模式）" % mode)

    if mode == "once":
        expect(ctx, "自愈出来的 checkpoint 就是客户端拿到的那个",
               (second or {}).get("id") == rec2.get("id"),
               "盘上第二条 manifest 的 id = %s" % rec2.get("id"),
               "manifest id = %s" % (second or {}).get("id"),
               "once 模式：第二次 create 成功，客户端与盘上说的得是同一个条目")
        lrec2 = ctx.op(box.list_rec(), stage="T21", step="list-2")
        expect(ctx, "自愈出来的条目进了 list（第一个隐藏条目仍不进）",
               lrec2.get("ok") and (lrec2.get("ids") or []) == [rec2.get("id")],
               "list 只有 %s 一条" % rec2.get("id"),
               lrec2.get("err") or lrec2.get("ids"),
               "store.go List：Hidden 不显示、正常条目显示")

    # 这条路上服务端**没有**专属日志行：`failed to record checkpoint` 只进 HTTP
    # 响应体（`writeError` 不打日志，service.go:537），`failed to keep the advanced
    # epoch` 只在 `CommitHidden` **也**失败时才打（service.go:591），而这里
    # CommitHidden 是成功的（隐藏条目上面已经判过了）。所以硬判据放在客户端拿到的
    # 响应体上，日志那边只作记录。
    expect(ctx, "服务端把这次失败记在了 Commit 那一步（响应体）",
           "failed to record checkpoint" in (info.get("message") or ""),
           "错误 message 里带 failed to record checkpoint",
           "message=%s" % (info.get("message") or "")[-160:],
           "service.go:537：commit 失败分支的 writeError —— 它不打日志，判据只能在客户端")

    _log_expect(ctx, orch, r"failed to keep the advanced epoch|checkpoint create failed",
                "服务端日志里这次 Commit 失败的痕迹",
                "只有 create 收尾那行 checkpoint create failed；commit 失败分支本身不打日志",
                "service.go:421 defer 的 Warn；failed to keep the advanced epoch "
                "只在 CommitHidden 也失败时才打（service.go:591），自愈路径上看不到",
                hard=False)

    rr = ctx.op(box.restore((first or {}).get("id", "ckpt_none"), verify=False),
                stage="T21", step="restore-hidden")
    info3 = _err_info(rr)
    infos.append(info3)
    expect(ctx, "隐藏条目不能当 restore 目标",
           (not rr.get("ok")) and bool(common.NOTFOUND_RE.search(info3["message"])),
           "404 not_found",
           "居然成功了" if rr.get("ok") else "code=%s：%s" % (info3["code"], info3["message"][-120:]),
           "armed 时一个可见 checkpoint 都建不出来，所以\"restore 到更早 checkpoint 成功\""
           "要等摘掉 env 才测得了；隐藏条目按设计就不是目标")

    _record_reason_source(ctx, infos)
    ctx.results["summary"]["T21"].update(
        {"fault": "commit_late", "mode": mode,
         "second_create_ok": bool(rec2.get("ok")),
         "entries": [{k: m.get(k) for k in ("id", "state", "hidden", "parent_id", "mem_mode")}
                     for m in ms]})


HANDLERS = {"envd_timeout": envd_timeout, "torn_assemble": torn_assemble,
            "seal_move": seal_move, "commit_late": commit_late}


def run(ctx):
    fault = ctx.args.fault
    if not ctx.store or not os.path.isdir(ctx.store):
        raise Unmet("读不到 checkpoint 产物目录（%s）——T21 的判定一半在盘上的 manifest 里"
                    % ctx.store,
                    "在宿主机（920B / 950）上跑，别在别的机器上跑（README「怎么跑」）")
    log("  注入 = %s（服务端启动时带 CHECKPOINT_FAULT_INJECT=%s 才有效）" % (fault, fault))
    ctx.results["summary"].setdefault("T21", {})
    ctx.stage("T21", fault=fault)
    HANDLERS[fault](ctx)
