# -*- coding: utf-8 -*-
"""
T18 orchestrator 重启（方案 §4.1，F8）。

**checkpoint 账本不过夜**：orchestrator 一起来就把整个 store 根删掉重建 ——
`checkpoint.NewStore()` 的第一句就是 `os.RemoveAll(root)`
（`packages/orchestrator/internal/checkpoint/store.go:231-234`），root 是
`filepath.Join(config.DefaultCacheDir, "checkpoints")`（`main.go:393`）。
内存里的 `bySandbox` / `bases` / `rootfs` 也都是当场 `make` 的空 map
（store.go:245-249），没有任何从盘上重建账本的路径。于是重启之后：

  · 旧沙箱的 checkpoint 目录**在盘上没了**；
  · 任何沙箱的 `list` 都是空的；
  · 第一次 `create` 没有 parent 可挂 → **全量新根**（manifest `mem_mode=full`、
    `parent_id` 空）。

**沙箱也一起没了**：orchestrator 不在退出路径上主动 kill 沙箱（main.go 的 closers
里没有这一条），但 Firecracker 是它 `exec.CommandContext` 起的**子进程**
（`internal/sandbox/fc/process.go:117`，`SysProcAttr.Setsid=true`，第 170–175 行那段
`CLONE_INTO_CGROUP` 是注释掉的），而栈是用 nomad job 重跑的 —— nomad 杀 task 是按
task 的 cgroup 整棵树杀，FC 跟着一起走。`switch-stack.sh` 因此在换栈前**要求活 FC
数为 0**（脚本里那句 `[ "$(pgrep -c -x firecracker)" = 0 ] || 还有沙箱在跑，先清掉`）。

这带来一个用例必须处理的矛盾：需求书要的是「**有 checkpoint 的沙箱存活时**重启」，
而唯一正规的重启方式在有活沙箱时会拒绝执行。`--pre-restart` 三档：

  · `auto`（默认）：先带着沙箱 A 原样执行重启命令；命令要是以「还有沙箱在跑」拒了，
    就 kill 掉 A 再重来一次，并把这件事记进 JSON（`pre_killed=true`）——
    此时「重启带走了沙箱 A」这条退化成记录项（是我们自己 kill 的）；
  · `keep`：只试带沙箱的那一次，被拒就算前置不满足（退出码 3）。想验「真·存活时
    重启」（比如 `nomad alloc restart`、直接 kill -TERM orchestrator）用这一档配
    `--restart-cmd`；
  · `kill`：不试，直接先 kill A 再重启（最快，只验 store/账本那三条）。

**用例自己绝不执行重启命令，除非显式给了 `--allow-restart`**：这台机器上还有别人的
沙箱，重启是要放行的事（pending.py 里留位的理由）。没给就打印命令并以 **3** 退出。

跑法：

    python -m crtest T18 --no-probe           # 只打印要执行的命令，退出码 3，一个沙箱都不建
                                              # （不加 --no-probe 的话，公共前置那次
                                              #   「脏页后端」探测还是会建一个又删掉）
    python -m crtest T18 --allow-restart      # 真重启（默认 switch-stack.sh jll）
    python -m crtest T18 --allow-restart --pre-restart keep \\
        --restart-cmd 'nomad alloc restart <alloc>'

判定：
  1. 重启前 create ×2 = 全量 + 增量（建立"账本确实在"的前提）；
  2. orchestrator 换了进程（pid 变）；
  3. 重启前 store 根下的沙箱目录**一个不剩**（`judge_store_cleared`）；
  4. 沙箱 A 没了（connect 用不了 / 没有它的 FC 进程）—— `pre_killed` 时降为记录项；
  5. 新沙箱 B 的 `list` 为空；
  6. B 的第一次 create 是**全量新根**（`judge_full_root`）；
  7. 回这个 checkpoint 成功且现场逐项一致；
  8. netns 不比开跑前多（`judge_netns_steady`）。

记录项：重启命令耗时、5008 health 回 200 的耗时、API 侧能建出沙箱的耗时。
"""

NAME = "T18"

import os

from .. import common
from ..common import Unmet, expect, log, note

# 重启命令没有对所有部署都成立的默认值：源码树上是切栈脚本，RPM 部署上是
# nomad / systemd。所以默认空，必须由 --restart-cmd 或环境变量
# CRTEST_RESTART_CMD 给出；命令里的 $R 会换成 R / CRTEST_REPO。
REPO = os.environ.get("CRTEST_REPO", "")
DEFAULT_RESTART_CMD = os.environ.get("CRTEST_RESTART_CMD", "")
DEFAULT_HEALTH = "http://127.0.0.1:5008/health"

BASIS = ("infra-arm jll：checkpoint/store.go:231-234 `NewStore` 的 `os.RemoveAll(root)`；"
         "main.go:393 root=DefaultCacheDir/checkpoints；"
         "sandbox/fc/process.go:117 FC 是 orchestrator 的子进程（nomad 按 task cgroup 整树杀）")

HOWTO = """这一条会重起整台机器上的 orchestrator（别人的沙箱也会跟着没），所以要显式放行：

      python -m crtest T18 --allow-restart

    重启命令默认取环境变量 CRTEST_RESTART_CMD，本机解析成 `%s`（空 = 必须显式给）。
    要换别的重启方式（比如只重起 alloc、或者带着活沙箱重启）用 --restart-cmd，
    并配 --pre-restart keep。"""


def add_args(ap):
    ap.add_argument("--allow-restart", action="store_true",
                    help="真的执行重启命令。不给就只打印命令并以退出码 3 退出（默认）")
    ap.add_argument("--restart-cmd", default=DEFAULT_RESTART_CMD,
                    help="重启 orchestrator 的命令，走 shell（默认取环境变量 "
                         "CRTEST_RESTART_CMD，本机当前 %r；命令里的 $R 取环境变量 "
                         "R 或 CRTEST_REPO，本机当前 %r）"
                         % (DEFAULT_RESTART_CMD, REPO))
    ap.add_argument("--pre-restart", choices=["auto", "keep", "kill"], default="auto",
                    help="重启时拿沙箱 A 怎么办：auto=先带着试、被拒再 kill 重来（默认）；"
                         "keep=只试带着的那一次，被拒即前置不满足；kill=直接先 kill 再重启")
    ap.add_argument("--health-url", default=DEFAULT_HEALTH,
                    help="重启后等哪个 health 回 200（默认 %s）" % DEFAULT_HEALTH)
    ap.add_argument("--restart-timeout", type=float, default=600.0,
                    help="重启命令本身的上限秒数（默认 600）")
    ap.add_argument("--health-timeout", type=float, default=300.0,
                    help="等 health 200 的上限秒数（默认 300）")
    ap.add_argument("--ready-timeout", type=float, default=300.0,
                    help="等 API 侧能建出沙箱的上限秒数（默认 300；920B 实测约 2 分钟）")
    return ap


# ---------------------------------------------------------------- 小工具

def _restart_cmd(a):
    """把 `$R` 换成仓库根（环境变量 R 优先），别的原样交给 shell。"""
    return a.restart_cmd.replace("$R", os.environ.get("R") or REPO)


def _store_children(store):
    """store 根下的条目名（= 沙箱 id）。读不到返回 []。"""
    try:
        return sorted(os.listdir(store))
    except OSError:
        return []


def _connect_dead(ctx, sandbox_id, label):
    """同 id 再 connect() 应当用不了（抄 T11 的 `_connect_dead` 口径）。"""
    try:
        box = common.connect_box(ctx, sandbox_id, label)
    except Exception as e:      # noqa: BLE001
        return True, "connect() 就抛了：%s: %s" % (type(e).__name__, e)
    try:
        box.run("echo probe=1", timeout=30)
    except Exception as e:      # noqa: BLE001
        return True, "connect() 通了但命令抛了：%s: %s" % (type(e).__name__, e)
    finally:
        common.forget(box)
    return False, "居然还能在里面跑命令"


def _spawn_ready(ctx, timeout, label="t18-b"):
    """等 API 侧真的 ready：反复试着建沙箱，直到建出来或等满 timeout。

    这比"看某个端口"靠谱 —— 换栈脚本自己就说切完 API 侧还要约 2 分钟才重连上，
    而"能不能建沙箱"正是我们下一步要用的能力。返回 `(box, 等了多久, 试了几次)`。
    """
    holder = {}

    def once():
        try:
            sbx = common.sandbox_create(ctx.args.template, private=not ctx.args.public,
                                        timeout=3600)
        except Exception as e:      # noqa: BLE001
            holder["err"] = "%s: %s" % (type(e).__name__, e)
            return None
        box = common.Box(sbx, ctx.store, label)
        common._ALL_BOXES.append(box)       # noqa: SLF001 —— 兜底 kill 名单
        holder["box"] = box
        return box

    box, waited, tries = common.wait_until(once, timeout, interval=10.0)
    if box is None:
        raise common.Failed("重启后 API 能建出沙箱", "%g s 内建出一个" % timeout,
                            "试了 %d 次都没成，最后一次：%s" % (tries, holder.get("err")),
                            "switch-stack.sh 自己也说切完约 2 分钟才 ready；超过 --ready-timeout 就是没起来")
    return box, waited, tries


def _do_restart(ctx, cmd, box_a):
    """执行重启。返回 `(记录, 是否事先 kill 了 A)`。"""
    a = ctx.args
    pre_kill = a.pre_restart == "kill"
    if pre_kill:
        log("  --pre-restart kill：先把沙箱 A kill 掉再重启")
        box_a.kill()
        common.forget(box_a)
        _wait_no_fc(ctx, box_a.id)
    log("  执行重启命令：%s" % cmd)
    rec = common.run_shell(cmd, timeout=a.restart_timeout)
    rec["pre_killed"] = pre_kill
    if rec["rc"] == 0 or pre_kill:
        return rec, pre_kill
    idle = bool(common.RESTART_NEEDS_IDLE_RE.search(rec["out"] or ""))
    if not idle:
        raise common.Failed("重启命令跑通", "退出码 0",
                            "退出码 %s：%s" % (rec["rc"], (rec["out"] or "")[-500:]),
                            "T18 前置：重启命令是 --restart-cmd 给的")
    if a.pre_restart == "keep":
        raise Unmet("重启命令要求先把活沙箱清干净，而 --pre-restart keep 不许 kill 沙箱 A",
                    "想验「带着活沙箱重启」就换一个不要求空载的重启方式：\n"
                    "      --restart-cmd 'nomad alloc restart <alloc-id>'\n"
                    "    想照常验 store/账本那几条就用默认的 --pre-restart auto。\n"
                    "    刚才那条命令的输出：%s" % (rec["out"] or "")[-300:])
    log("  重启命令拒了（还有活沙箱）：kill 掉 A 再来一次")
    ctx.op(dict(rec, op="restart", stage="T18", step="restart-refused"))
    box_a.kill()
    common.forget(box_a)
    _wait_no_fc(ctx, box_a.id)
    rec2 = common.run_shell(cmd, timeout=a.restart_timeout)
    rec2["pre_killed"] = True
    rec2["refused_first"] = True
    if rec2["rc"] != 0:
        raise common.Failed("重启命令跑通（kill 掉 A 之后）", "退出码 0",
                            "退出码 %s：%s" % (rec2["rc"], (rec2["out"] or "")[-500:]),
                            "T18 前置")
    return rec2, True


def _wait_no_fc(ctx, sandbox_id, timeout=60.0):
    """等这个沙箱的 firecracker 进程走干净（换栈脚本要求活 FC 为 0）。"""
    left, waited, _ = common.wait_until(
        lambda: not common.fc_processes(sandbox_id), timeout, interval=2.0)
    log("    等它的 FC 退出：%.1f s，%s" % (waited, "干净了" if left else "还有残留"))
    return left


# ---------------------------------------------------------------- 主流程

def run(ctx):
    a = ctx.args
    cmd = _restart_cmd(a)
    if not a.allow_restart:
        raise Unmet("没有放行重启（--allow-restart 没给）", HOWTO % (cmd or "<未设>"))
    if not cmd.strip():
        raise Unmet("没有给重启 orchestrator 的命令",
                    "本机怎么重启没有通用默认值：用 --restart-cmd 指定，或设环境变量 "
                    "CRTEST_RESTART_CMD。源码树部署是切栈脚本，RPM 部署是 "
                    "`nomad job restart orchestrator` 一类。")
    if not ctx.store:
        raise Unmet("不在宿主机上（读不到 orchestrator 的 environ / checkpoint store）",
                    "T18 要读 store 根目录和 orchestrator 的 pid，必须在 920B 上跑。")

    netns_before = common.netns_count_steady()
    # 暖池状态必须取在 **before 这一刻**：跑完时 orchestrator 早已跑够 35 分钟，
    # 但 before 是在暖池填充中采的，后面的 after 必然更大 —— 那是暖池在长不是泄漏。
    netns_warm, netns_warm_why = common.netns_pool_warm()
    pid_before, _ = common.orchestrator_pid_env()
    store_before = _store_children(ctx.store)
    ctx.stage("before-restart", orchestrator_pid=pid_before,
              store_children=len(store_before), netns=netns_before,
              live_fc=common.live_fc_count())
    log("  重启前：orchestrator pid=%s，store 根下 %d 个沙箱目录，netns=%s"
        % (pid_before, len(store_before), netns_before))

    # ---- 1. 沙箱 A：写个标记，建两层（全量 + 增量）
    box_a = common.spawn(ctx, 1, "t18-a")[0]
    log("  沙箱 A = %s" % box_a.id)
    box_a.setup(warm_mem=32, warm_file=8)
    box_a.dirty("a0", mem_mb=16, file_mb=4)
    r1 = ctx.op(box_a.create("a0"), stage="T18", step="a-create-full")
    expect(ctx, "A 的第一次 create", r1.get("ok"), "成功",
           r1.get("err") or "%.3f s" % r1.get("wall_s", 0), "T18 前置")
    box_a.dirty("a1", mem_mb=16, file_mb=4)
    r2 = ctx.op(box_a.create("a1"), stage="T18", step="a-create-incr")
    expect(ctx, "A 的第二次 create", r2.get("ok"), "成功",
           r2.get("err") or "%.3f s" % r2.get("wall_s", 0), "T18 前置")

    man_a = common.read_manifests(ctx.store, box_a.id)
    dir_a = box_a.store_dir()
    ok, want, got = common.judge_full_root(r1, man_a[0] if man_a else None)
    expect(ctx, "重启前 A 的第一层是全量根", ok, want, got, BASIS)
    ok, want, got = common.judge_chain_incremental(
        man_a[0] if man_a else None, man_a[1] if len(man_a) > 1 else None)
    expect(ctx, "重启前 A 的第二层是增量", ok, want, got,
           "重启前账本是好的 —— 不先立这条，「重启后走全量」就说明不了问题")
    log("  A 的产物目录 %s（盘上 %d 条）" % (dir_a, len(man_a)))
    ctx.results["summary"].setdefault("T18", {})["before"] = {
        "sandbox": box_a.id, "dir": dir_a,
        "entries": [{"id": m.get("id"), "mem_mode": m.get("mem_mode"),
                     "parent_id": m.get("parent_id"), "hidden": m.get("hidden")}
                    for m in man_a],
        "store_children": store_before,
    }
    store_before = _store_children(ctx.store)       # A 的目录也算进"重启前那批"

    # ---- 2. 重启
    rrec, pre_killed = _do_restart(ctx, cmd, box_a)
    ctx.op(dict(rrec, op="restart", stage="T18", step="restart"))
    log("  重启命令退出码 %s，耗时 %.1f s%s"
        % (rrec["rc"], rrec["wall_s"], "（事先 kill 了 A）" if pre_killed else ""))

    code, health_s, health_tries = common.wait_until(
        lambda: common.http_ok(a.health_url) == 200, a.health_timeout, interval=2.0)
    expect(ctx, "重启后 5008 health 回 200", bool(code),
           "%g s 内 200" % a.health_timeout,
           "等了 %.1f s（%d 次探测）%s" % (health_s, health_tries, "，到了" if code else "，没等到"),
           "switch-stack.sh 自己也用 /health 判新进程起没起来")

    pid_after, _ = common.orchestrator_pid_env()
    ok, want, got = common.judge_restarted(pid_before, pid_after)
    if ok is None:
        note(ctx, "orchestrator 换了进程", want, got, BASIS)
    else:
        expect(ctx, "orchestrator 换了进程", ok, want, got, BASIS)

    store_after, _, _ = common.checkpoint_store()
    expect(ctx, "重启后 store 路径没变", store_after == ctx.store,
           ctx.store, store_after,
           "路径变了的话下面「旧目录被清」比的就不是同一个地方（main.go:393 用的是 DefaultCacheDir）")

    # ---- 3. 旧账本没了
    children_after = _store_children(ctx.store)
    ok, want, got = common.judge_store_cleared(store_before, children_after)
    expect(ctx, "重启清空了旧 store 根", ok, want, got, BASIS)
    a_dir_gone = not os.path.isdir(dir_a)
    expect(ctx, "A 的产物目录没了", a_dir_gone, "不存在",
           "没了：%s" % dir_a if a_dir_gone else "还在：%s" % dir_a, BASIS)

    left = common.fc_processes(box_a.id)
    gone, detail = _connect_dead(ctx, box_a.id, "t18-a-dead")
    if pre_killed:
        note(ctx, "沙箱 A 已经不存在", "重启带走沙箱（本轮是我们自己先 kill 的，只作记录）",
             "%s；残留 FC 进程 %s" % (detail, left or "0 个"), BASIS)
    else:
        expect(ctx, "重启带走了沙箱 A", gone and not left,
               "connect 用不了、没有它的 FC 进程",
               "%s；残留 FC 进程 %s" % (detail, left or "0 个"), BASIS)
    common.forget(box_a)

    # ---- 4. 新沙箱 B：list 空、create 走全量根、能回
    box_b, ready_s, ready_tries = _spawn_ready(ctx, a.ready_timeout)
    log("  沙箱 B = %s（API ready 等了 %.1f s，试了 %d 次）" % (box_b.id, ready_s, ready_tries))

    lrec = ctx.op(box_b.list_rec(), stage="T18", step="b-list")
    expect(ctx, "重启后新沙箱 list 为空", lrec.get("ok") and not (lrec.get("ids") or []),
           "0 条", lrec.get("err") or "%d 条：%s" % (len(lrec.get("ids") or []), lrec.get("ids")),
           "F8：NewStore 的 map 是空的，没有从盘上重建账本的路径")

    box_b.setup(warm_mem=32, warm_file=8)
    box_b.dirty("b0", mem_mb=16, file_mb=4)
    b1 = ctx.op(box_b.create("b0"), stage="T18", step="b-create")
    expect(ctx, "B 的第一次 create", b1.get("ok"), "成功",
           b1.get("err") or "%.3f s" % b1.get("wall_s", 0), "T18")
    man_b = common.read_manifests(ctx.store, box_b.id)
    first_b = next((m for m in man_b if m.get("id") == b1.get("id")), man_b[0] if man_b else None)
    ok, want, got = common.judge_full_root(b1, first_b)
    expect(ctx, "重启后第一次 create 走全量新根", ok, want, got, BASIS)

    box_b.dirty("b1", mem_mb=16, file_mb=4)
    rr = ctx.op(box_b.restore(b1["id"], verify=True), stage="T18", step="b-restore")
    expect(ctx, "回重启后建的 checkpoint", rr.get("ok"), "成功",
           rr.get("err") or "%.3f s" % rr.get("wall_s", 0),
           "F8：清账本不能把「新建的还能回」一起清掉；失败现场 %s" % box_b.store_dir(b1["id"]))
    expect(ctx, "回完现场一致", rr.get("verified") is not False, "五项全同",
           rr.get("mismatch") or "全同", "现场口径同 checkpoint_verify.py")
    alive, d = box_b.alive()
    expect(ctx, "B 跑完还活着", alive, "命令能跑、能写盘、心跳在推进", d, "活体判据同 T22")

    netns_after = common.netns_count_steady()
    ok, want, got = common.judge_netns_steady(netns_before, netns_after, slack=8)
    warm, warm_why = netns_warm, netns_warm_why
    if ok is None:
        note(ctx, "netns 槽位不泄漏", want, got, "09-16 清过 8729 个泄漏槽位")
    elif warm is not True:
        # 暖池没填满（或读不到启动时间）时 before 取在填充中，after 必然更大 —— 那是
        # 暖池在长，不是泄漏。降级成记录项，不判失败。
        log("  注意：netns 判定降级为记录项 —— %s" % warm_why)
        note(ctx, "netns 槽位不泄漏（降级为记录项）", want,
             "%s；不判失败，原因：%s" % (got, warm_why),
             "09-16 清过 8729 个泄漏槽位；orchestrator 启动不足 %.0f 分钟时这条判定不成立"
             % (common.NETNS_POOL_WARMUP_S / 60.0))
    else:
        expect(ctx, "netns 槽位不泄漏", ok, want, got,
               "09-16 清过 8729 个泄漏槽位；容差 8 是给机器上别人的沙箱留的")

    ctx.results["summary"]["T18"].update({
        "restart_cmd": cmd, "pre_restart": a.pre_restart, "pre_killed": pre_killed,
        "restart_rc": rrec["rc"], "restart_s": rrec["wall_s"],
        "restart_refused_first": bool(rrec.get("refused_first")),
        "health_s": health_s, "health_tries": health_tries,
        "api_ready_s": ready_s, "api_ready_tries": ready_tries,
        "orchestrator_pid_before": pid_before, "orchestrator_pid_after": pid_after,
        "store_children_before": len(store_before), "store_children_after": len(children_after),
        "sandbox_a": box_a.id, "sandbox_b": box_b.id,
        "b_first_create_mem_mode": b1.get("mem_mode"),
        "b_first_manifest": {"mem_mode": (first_b or {}).get("mem_mode"),
                             "parent_id": (first_b or {}).get("parent_id")},
        "netns_before": netns_before, "netns_after": netns_after,
        "netns_pool_warm": warm, "netns_pool_warm_why": warm_why,
        "orchestrator_uptime_s": common.orchestrator_uptime_s(),
    })
    log("")
    log("  重启 %.1f s，health 200 用了 %.1f s，API 能建沙箱用了 %.1f s"
        % (rrec["wall_s"], health_s, ready_s))
