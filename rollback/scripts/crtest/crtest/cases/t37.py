# -*- coding: utf-8 -*-
"""
T37 两道配额闸（infra-arm jll `55a542846`，SDK 映射 `16b7cdeb`）。

服务端在 create 之前有两道与产物盘/条目数有关的闸，都是**动手之前**就把请求打回，
所以沙箱一定还活着、账本一定没动：

  · `CHECKPOINT_MIN_FREE_BYTES`（`checkpoint/service.go` 的 `minFreeEnv`）——产物盘
    空闲低于水位就拒。默认是 `max(4 GiB, 2×guest 内存)`，env 给的是**绝对字节数**，
    0 表示关掉这道闸。回 **HTTP 507** + 错误体 `{"code":"resource_exhausted",
    "reason":"disk_full"}`。**create 与 restore 都走这道闸**（service.go 的
    `refuseIfDiskFull` 两个调用点：create 一处、restore 一处），list / delete 不走。
  · `CHECKPOINT_MAX_PER_SANDBOX`（`checkpoint/store.go` 的 `maxPerSandboxEnv`）——
    每沙箱**可见**条目上限（`atCheckpointLimitLocked` 明确跳过 `Hidden` 条目：
    调用方看不见也删不掉的条目不该算进他的额度）。0（默认）= 不限。超了回
    **HTTP 429** + `{"code":"resource_exhausted","reason":"too_many_checkpoints"}`。
    它只挡 create（在 `store.Prepare` 里），restore / list / delete 不受影响。
    上限在 `NewStore` 里**只读一次**，所以改 env 必须重启 orchestrator。

SDK 侧（e2b-arm `jll`，`e2b/sandbox/checkpoint/errors.py`）把这两个 reason 映射成
`CheckpointDiskFullException` / `CheckpointTooManyException`。两条 reason 挤在同一个
Connect code（`resource_exhausted`）下，所以**只有 reason 分得开它们**：
`_CHECKPOINT_CODE_MAP` 对 `resource_exhausted` 故意只给基类。本用例因此把
「异常类型」「`.reason` 是不是 field 级」两条都判死。

**用例只检查、不切栈**：两道闸都是 orchestrator 启动时读 env 的事。用例读
orchestrator 进程的 environ（`common.orchestrator_pid_env()`）确认当前节点装的是哪
一道，装错了以**退出码 3（前置不满足）**退出并打印怎么设——判 1 会把「没装闸」和
「闸坏了」混成一件事（沿用 T21 的口径）。

两段各自独立跑：

    EXTRA_ENV="CHECKPOINT_MIN_FREE_BYTES=10995116277760" tmp/switch-stack.sh jll
    python -m crtest T37 --quota disk_full

    EXTRA_ENV="CHECKPOINT_MAX_PER_SANDBOX=2" tmp/switch-stack.sh jll
    python -m crtest T37 --quota too_many

判定的「秤」有三把（与 T21 一致）：

  · **SDK 异常对象**：类名 + `.reason`（`common.error_info()` 的 `reason_src` 必须是
    `field`，退化到 guess 就说明 SDK 没把 reason 带出来）；
  · **HTTP 层**：SDK 的异常里**没有** HTTP 状态码（`CheckpointException` 只有
    `reason` / `checkpoint_id` / `sandbox_id`，`ConnectException.status` 是 Connect
    code 不是 HTTP 码），所以 507 / 429 得自己发一次裸请求量——用例借 SDK 已经建好的
    那个 httpx 客户端（`sbx.checkpoint._health_api`，base_url 与流量令牌都在里面）
    POST 一次 `CreateCheckpoint`，读 `status_code` 与错误体。**这一发在两段里都保证
    被闸打回**（disk_full 段永远被拒；too_many 段只在已经顶到上限时发），不会偷偷多
    建一个 checkpoint；
  · **服务端**：启动那行 `checkpoint capabilities`（`main.go:742`）回显
    `min_free_bytes` / `max_checkpoints_per_sandbox` 及各自的 `_source`，用来证明
    env 真的被这个进程读到了。

**服务端日志里没有 `disk_full` / `too_many_checkpoints` 这两个词**：`writeError`
只写响应体不打日志，`record()` 只打点不打日志，两条路径上唯一的日志是 create 收尾
那个 defer 的 `checkpoint create failed`（service.go:421，只带 timings）。所以日志侧
一律 `note`，硬判据在客户端和响应体上。
"""

NAME = "T37"

import os

from .. import common
from ..common import Unmet, expect, log, note

QUOTAS = ["disk_full", "too_many"]

HOWTO = """把对应的 env 加进 orchestrator 的 task 再重起栈（切栈要求活 FC 数为 0，
    切完 API 侧约 2 分钟才 ready）：
      EXTRA_ENV="%s" tmp/switch-stack.sh jll
    起来以后 orchestrator 会打一行 `checkpoint capabilities`，里面 min_free_bytes /
    max_checkpoints_per_sandbox 的 _source 应当是 env。跑完用不带 EXTRA_ENV 的
    tmp/switch-stack.sh jll 把栈换回去。"""

BASIS = ("infra-arm jll 55a542846（service.go minFreeEnv / store.go maxPerSandboxEnv）；"
         "SDK e2b-arm jll 16b7cdeb（errors.py 的 _CHECKPOINT_REASON_MAP）；"
         "报告 2026-09-18 §7.2 的两个新 env")

CREATE_ROUTE = "/checkpoint.Checkpoint/CreateCheckpoint"


# ---------------------------------------------------------------- 小工具

def _boot(ctx, label="t37-"):
    box = common.spawn(ctx, 1, label)[0]
    log("  沙箱 %s" % box.id)
    box.setup(warm_mem=32, warm_file=8)
    box.dirty("g0", mem_mb=16, file_mb=4)
    return box


def _free_bytes(path):
    """产物盘还剩多少可用字节（与服务端 `diskFree` 同口径：f_bavail）。"""
    if not path:
        return None
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    return st.f_bavail * (st.f_frsize or st.f_bsize)


def _orch_env(ctx, name):
    """orchestrator 进程 environ 里这一项的原始值（没有就 None）。"""
    pid, env = common.orchestrator_pid_env()
    ctx.results["meta"]["orchestrator_pid"] = pid
    return env.get(name)


def _capabilities(ctx, orch):
    """启动那行 `checkpoint capabilities`（取最后一条）。读不到返回 ""。"""
    last = ""
    for p in orch.files():
        try:
            size = os.path.getsize(p)
            with open(p, "r", errors="replace") as f:
                f.seek(max(0, size - (4 << 20)))     # 只看末尾 4 MiB，别把整份日志读进来
                for line in f:
                    if "checkpoint capabilities" in line:
                        last = line.rstrip("\n")
        except OSError:
            continue
    ctx.results["summary"].setdefault("T37", {})["capabilities_line"] = last
    return last


def _raw_create(box, name):
    """绕开 SDK 发一次裸的 CreateCheckpoint，只为了拿 HTTP 状态码与原样的错误体。

    借的是 SDK 自己建好的 httpx 客户端（base_url = checkpoint API 地址，headers 里
    已经有沙箱的流量令牌），所以不用自己拼地址、不用自己找 token。**只在这一发一定
    会被闸打回的时候用**，否则会凭空多一个 checkpoint。
    """
    rec = {"op": "raw_create", "box": box.label, "sandbox": box.id, "name": name}
    try:
        cli = box.sbx.checkpoint._health_api      # noqa: SLF001 —— 就是要那个已配好的客户端
        r = cli.post(CREATE_ROUTE, json={"name": name}, timeout=60)
        rec["http_status"] = r.status_code
        rec["retry_after"] = r.headers.get("Retry-After")
        try:
            rec["body"] = r.json()
        except ValueError:
            rec["body"] = {"_raw": r.text[:500]}
        rec["ok"] = r.status_code == 200
    except Exception as e:      # noqa: BLE001
        rec["ok"] = False
        common.record_error(rec, e)
    return rec


def _judge_sdk(ctx, rec, want_type, want_reason, step):
    """一次被闸打回的 create：类型 / reason / reason 出处 三条。返回 error_info。"""
    info = common.rec_error_info(rec)
    expect(ctx, "%s 被拒" % step, not rec.get("ok"), "抛异常",
           "居然成功了（id=%s）" % rec.get("id") if rec.get("ok")
           else "%s: %s" % (info["type"], (info["message"] or "")[-160:]),
           BASIS)
    expect(ctx, "%s 抛的是 %s" % (step, want_type), info["type"] == want_type,
           want_type, info["type"],
           "errors.py `_CHECKPOINT_REASON_MAP`：reason → 异常类；"
           "两条 reason 同属 resource_exhausted，按 code 退化只会得到基类 CheckpointException")
    ok, detail = common.reason_ok(info, want_reason)
    expect(ctx, "%s 的 reason" % step, ok, "reason=%s" % want_reason, detail, BASIS)
    expect(ctx, "%s 的 reason 是异常对象上的字段" % step, info.get("reason_src") == "field",
           "reason_src=field（SDK 把服务端的 reason 带出来了）",
           "reason_src=%s" % info.get("reason_src"),
           "reason 若只能从文案猜，说明 SDK 没有真的带出这个字段")
    return info


def _judge_raw(ctx, box, name, want_status, want_reason, step):
    """裸请求那一发：HTTP 状态码 + 错误体的 code / reason。"""
    rec = ctx.op(_raw_create(box, name), stage="T37", step=step)
    body = rec.get("body") or {}
    expect(ctx, "%s 的 HTTP 状态码" % step, rec.get("http_status") == want_status,
           str(want_status), "%s（body=%s）" % (rec.get("http_status"), body),
           "service.go 的 writeError：%s" % ("StatusInsufficientStorage" if want_status == 507
                                             else "StatusTooManyRequests"))
    expect(ctx, "%s 的错误体 reason" % step, body.get("reason") == want_reason,
           'reason=%s（code=resource_exhausted）' % want_reason,
           "reason=%s code=%s" % (body.get("reason"), body.get("code")), BASIS)
    note(ctx, "%s 带不带 Retry-After" % step,
         "服务端只给 busy（503）设 Retry-After，这两条闸不设",
         rec.get("retry_after") or "没有",
         "service.go writeBusy 是唯一设 Retry-After 的地方")
    return rec


def _judge_alive(ctx, box, step):
    ok, d = box.alive()
    expect(ctx, "%s 沙箱还活着" % step, ok,
           "guest 能执行命令、能读写、心跳还在走", str(d),
           "两道闸都在动手之前就把请求打回（create 的 FC 侧一步都没走），沙箱不该受影响")


def _judge_list(ctx, box, want_n, step):
    rec = ctx.op(box.list_rec(), stage="T37", step=step)
    ids = rec.get("ids") or []
    expect(ctx, "%s list 有 %d 条" % (step, want_n),
           rec.get("ok") and len(ids) == want_n,
           "list 成功且 %d 条" % want_n, rec.get("err") or ids,
           "被闸打回的 create 什么都没写进账本")
    return ids


def _log_note(ctx, orch):
    """服务端日志侧的记录项（不硬判，理由见模块头）。"""
    lines, waited, reads = common.wait_for_log(orch, r"checkpoint create failed")
    note(ctx, "服务端日志里这次失败的痕迹",
         "只有 create 收尾那行 checkpoint create failed（service.go:421 的 defer，只带 timings）",
         "%d 行（等了 %.1f s，读了 %d 次）：%s"
         % (len(lines), waited, reads, (lines[-1][-200:] if lines else "没等到")),
         "writeError 不打日志、record() 只打点")
    hits = orch.since(r"disk_full|too_many_checkpoints")
    note(ctx, "日志里有没有 reason 这两个词",
         "按实现应当没有：reason 只进 HTTP 响应体",
         "%d 行%s" % (len(hits), "：" + hits[-1][-200:] if hits else ""),
         "service.go writeError")
    ctx.results["summary"]["T37"]["log_lines"] = lines[-3:]
    ctx.results["summary"]["T37"]["log_reason_hits"] = hits[-3:]


# ---------------------------------------------------------------- disk_full

def disk_full(ctx):
    """水位闸：产物盘空闲低于 `CHECKPOINT_MIN_FREE_BYTES` 时 create 回 507。"""
    raw = _orch_env(ctx, "CHECKPOINT_MIN_FREE_BYTES")
    if not raw:
        raise Unmet("orchestrator 没带 CHECKPOINT_MIN_FREE_BYTES",
                    HOWTO % "CHECKPOINT_MIN_FREE_BYTES=10995116277760")
    try:
        need = int(raw)
    except ValueError:
        raise Unmet("CHECKPOINT_MIN_FREE_BYTES=%s 不是整数，服务端会忽略它并用默认值" % raw,
                    HOWTO % "CHECKPOINT_MIN_FREE_BYTES=10995116277760")
    free = _free_bytes(ctx.store)
    if need <= 0 or (free is not None and free >= need):
        raise Unmet("水位 %s 拦不住 create（产物盘还空着 %s 字节）" % (need, free),
                    HOWTO % "CHECKPOINT_MIN_FREE_BYTES=10995116277760")
    log("  水位 = %d 字节；产物盘空闲 = %s 字节 → create 应当一律被拒" % (need, free))
    ctx.results["summary"]["T37"].update({"quota": "disk_full", "min_free_bytes": need,
                                          "free_bytes": free})

    orch = common.OrchLog()
    caps = _capabilities(ctx, orch)
    expect(ctx, "启动日志回显了这道闸", ('"min_free_bytes":%d' % need) in caps.replace(" ", ""),
           '"min_free_bytes":%d 且 "min_free_bytes_source":"env"' % need,
           caps[-240:] or "没找到 checkpoint capabilities 那一行",
           "main.go:742 + capabilities.go Fields()")
    note(ctx, "水位的出处", 'min_free_bytes_source=env',
         "env" if '"min_free_bytes_source":"env"' in caps.replace(" ", "") else caps[-120:],
         "capabilities.go：env 解析失败会退回 default，出处能把「设了」和「设错了」分开")

    box = _boot(ctx)
    orch.mark()

    rec = ctx.op(box.create("q0"), stage="T37", step="create-1")
    info = _judge_sdk(ctx, rec, "CheckpointDiskFullException", "disk_full", "第一次 create")
    expect(ctx, "message 说的是水位这件事",
           "keeps in reserve" in (info["message"] or ""),
           "错误文案里带 `bytes this node keeps in reserve`",
           (info["message"] or "")[-200:],
           "service.go refuseIfDiskFull 的 writeError 文案")

    _judge_raw(ctx, box, "q0-raw", 507, "disk_full", "裸请求")
    _judge_alive(ctx, box, "被拒之后")
    _judge_list(ctx, box, 0, "被拒之后")

    ms = common.read_manifests(ctx.store, box.id)
    expect(ctx, "盘上一个 checkpoint 目录都没留", not ms, "0 个",
           "%d 个：%s" % (len(ms), [m.get("id") for m in ms]),
           "闸在 store.Prepare 之前，什么都还没建")

    rec2 = ctx.op(box.create("q1"), stage="T37", step="create-2")
    _judge_sdk(ctx, rec2, "CheckpointDiskFullException", "disk_full", "第二次 create")
    _judge_alive(ctx, box, "第二次被拒之后")

    _log_note(ctx, orch)
    ctx.results["summary"]["T37"]["creates_refused"] = 2


# ---------------------------------------------------------------- too_many

def too_many(ctx):
    """条目数闸：可见 checkpoint 到 `CHECKPOINT_MAX_PER_SANDBOX` 后 create 回 429。"""
    raw = _orch_env(ctx, "CHECKPOINT_MAX_PER_SANDBOX")
    try:
        limit = int(raw) if raw else 0
    except ValueError:
        limit = 0
    if limit <= 0:
        raise Unmet("orchestrator 没带（或带了个服务端会忽略的）CHECKPOINT_MAX_PER_SANDBOX：%r" % raw,
                    HOWTO % "CHECKPOINT_MAX_PER_SANDBOX=2")
    mf = _orch_env(ctx, "CHECKPOINT_MIN_FREE_BYTES")
    free = _free_bytes(ctx.store)
    if mf and mf.isdigit() and free is not None and int(mf) > 0 and int(mf) > free:
        raise Unmet("这个节点同时还开着水位闸（%s > 空闲 %s），create 会先被 507 挡住" % (mf, free),
                    HOWTO % "CHECKPOINT_MAX_PER_SANDBOX=2")
    log("  每沙箱上限 = %d 条" % limit)
    ctx.results["summary"]["T37"].update({"quota": "too_many", "max_per_sandbox": limit})

    orch = common.OrchLog()
    caps = _capabilities(ctx, orch)
    expect(ctx, "启动日志回显了这道闸",
           ('"max_checkpoints_per_sandbox":%d' % limit) in caps.replace(" ", ""),
           '"max_checkpoints_per_sandbox":%d 且 _source":"env"' % limit,
           caps[-240:] or "没找到 checkpoint capabilities 那一行",
           "main.go:742 + capabilities.go Fields()")

    box = _boot(ctx)
    orch.mark()

    ids = []
    for i in range(limit):
        if i:
            box.dirty("g%d" % i, mem_mb=16, file_mb=4)
        rec = ctx.op(box.create("q%d" % i), stage="T37", step="create-%d" % (i + 1))
        expect(ctx, "额度内第 %d 次 create 成功" % (i + 1), rec.get("ok"), "成功",
               rec.get("err") or "id=%s mem_mode=%s" % (rec.get("id"), rec.get("mem_mode")),
               "上限是 %d，前 %d 次不该被挡" % (limit, limit))
        ids.append(rec["id"])
    _judge_list(ctx, box, limit, "额度用满时")

    over = ctx.op(box.create("q-over", record_scene=False), stage="T37", step="create-over")
    info = _judge_sdk(ctx, over, "CheckpointTooManyException", "too_many_checkpoints",
                      "第 %d 次 create" % (limit + 1))
    expect(ctx, "message 说的是额度这件事",
           "of %d allowed checkpoints" % limit in (info["message"] or ""),
           "错误文案里带 `holds %d of %d allowed checkpoints`" % (limit, limit),
           (info["message"] or "")[-200:],
           "store.go atCheckpointLimitLocked 的文案")

    _judge_raw(ctx, box, "q-over-raw", 429, "too_many_checkpoints", "裸请求")
    _judge_alive(ctx, box, "超限被拒之后")
    _judge_list(ctx, box, limit, "超限被拒之后")

    # restore 不走这道闸（它只在 store.Prepare 里）：顶着上限也照样能回滚。
    rr = ctx.op(box.restore(ids[-1]), stage="T37", step="restore-at-limit")
    expect(ctx, "顶着上限 restore 照常成功", rr.get("ok"), "成功",
           rr.get("err") or "wall=%.3f s" % (rr.get("wall_s") or 0),
           "限额只在 store.Prepare 里判，restore 这条路上没有它")
    expect(ctx, "restore 回来的现场对得上", rr.get("verified") is not False,
           "五个字段全等（mem_gen/file_gen/两个 md5/心跳 pid）",
           rr.get("mismatch") or "全等", "Box.restore 的现场校验")
    _judge_alive(ctx, box, "restore 之后")

    # delete 也不走这道闸；删掉一个之后额度就腾出来了。
    drec = ctx.op(box.delete(ids[0]), stage="T37", step="delete")
    expect(ctx, "顶着上限 delete 照常成功", drec.get("ok"), "成功",
           drec.get("err") or "删掉 %s" % ids[0],
           "限额只挡 create")
    left = _judge_list(ctx, box, limit - 1, "删掉一个之后")

    box.dirty("g-after", mem_mb=16, file_mb=4)
    again = ctx.op(box.create("q-after-delete"), stage="T37", step="create-after-delete")
    expect(ctx, "删掉一个之后 create 又能成功", again.get("ok"), "成功",
           again.get("err") or "id=%s" % again.get("id"),
           "store.go：被引用的条目只是转成 Hidden，而 Hidden 不算进额度 —— "
           "「调用方看不见也删不掉的条目不该占他的额度」")

    over2 = ctx.op(box.create("q-over-2", record_scene=False), stage="T37", step="create-over-2")
    _judge_sdk(ctx, over2, "CheckpointTooManyException", "too_many_checkpoints",
               "重新顶满之后再 create")
    _judge_alive(ctx, box, "全程结束时")

    _log_note(ctx, orch)
    ctx.results["summary"]["T37"].update(
        {"ids": ids, "after_delete_list": left, "refused": 2,
         "manifests": [{k: m.get(k) for k in ("id", "state", "hidden", "parent_id", "mem_mode")}
                       for m in common.read_manifests(ctx.store, box.id)]})


HANDLERS = {"disk_full": disk_full, "too_many": too_many}


def add_args(ap):
    ap.add_argument("--quota", required=True, choices=QUOTAS,
                    help="测哪一道闸（服务端一次只装一道，用例只检查不切栈）")
    return ap


def run(ctx):
    quota = ctx.args.quota
    if not ctx.store or not os.path.isdir(ctx.store):
        raise Unmet("读不到 checkpoint 产物目录（%s）——T37 要量这块盘的空闲、要读盘上的条目"
                    % ctx.store,
                    "在宿主机（920B / 950）上跑，别在别的机器上跑（README「怎么跑」）")
    log("  配额 = %s（服务端启动时带对应 env 才有效）" % quota)
    ctx.results["summary"].setdefault("T37", {})
    ctx.stage("T37", quota=quota)
    HANDLERS[quota](ctx)
