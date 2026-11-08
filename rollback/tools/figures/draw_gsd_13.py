"""进程级手册第 13 章 端到端走查。

13-1 checkpoint 的跨组件时序、13-2 restore 的跨组件时序（两张一起替换 §1 的 Mermaid 时序图）；
13-3 三棵树 checkpoint 时各棵树的冻结与停止区间（替换 §7.2 的文本示意）。
"""
from common import *  # noqa: F401,F403
from diagram_kit import RAMPS, ARROWS, INK, SUB, MUTED, text_w, wrap

PARTS = [  # (名字, 小字, 色阶)
    ("Python SDK", "调用方", "gray"),
    ("宿主代理", "只转发请求和响应", "gray"),
    ("gsd", "guest 里，编排全程", "purple"),
    ("CRIU", "每棵树一个子进程", "purple"),
    ("guest 内核", "/proc、overlay 切换", "gray"),
    ("用户进程树", "回滚范围内的进程", "gray"),
]
S, P, G, C, K, U = range(6)


class Seq:
    """简单的时序图：参与者 + 生命线 + 消息 + 自身动作 + 横跨备注 + 循环框 + 右侧计时键栏。"""
    LANE_R = 1330          # 参与者区域右边界，右侧留给计时键栏
    KEY_X = 1360

    def __init__(self, d):
        self.d = d
        n = len(PARTS)
        self.step = (self.LANE_R - d.M) / n
        self.x = [d.M + self.step * (i + 0.5) for i in range(n)]
        top = d.y + 24
        for i, (name, sub, ramp) in enumerate(PARTS):
            f, st, tc, sc = RAMPS[ramp]
            w = self.step - 22
            d.rect(self.x[i] - w / 2, top, w, 58, f, st, sw=1.4, rx=10)
            d.text(self.x[i], top + 25, name, 16.5, tc, 600, "middle")
            d.text(self.x[i], top + 46, sub, 12.5, sc, 400, "middle")
        d.text(self.KEY_X, top + 25, "计时键", 15, INK, 600)
        d.text(self.KEY_X, top + 46, "gsd 在 guest 里自己量", 12.5, MUTED)
        self.top = top + 58
        self.lifeline_at = len(d.o)        # 生命线最后插到这里，压在其他元素下面
        self.y = self.top + 22
        self.loops = []
        self.brackets = []

    # ----- 行
    def msg(self, a, b, label, kind="flow", key=None):
        d = self.d
        x1, x2 = self.x[a], self.x[b]
        lo, hi = min(x1, x2), max(x1, x2)
        lines = wrap(label, max(hi - lo - 24, 260), 13.5)
        col = ARROWS[kind][0]
        y = self.y + 18 * len(lines) + 4
        for k, s in enumerate(lines):
            d.text((lo + hi) / 2, self.y + 14 + 18 * k, s, 13.5, INK if kind == "flow" else col, 600 if kind != "flow" else 400,
                   "middle")
        sgn = 1 if x2 > x1 else -1
        d.arrow(x1 + 3 * sgn, y, x2 - 4 * sgn, y, kind)
        if key:
            self.key(key, y)
        self.y = y + 20
        return y

    def act(self, p, title, key=None, w=None):
        d = self.d
        w = w or 300
        lines = wrap(title, w - 24, 13.5)
        h = 16 + 19 * len(lines)
        f, st, tc, _ = RAMPS["gray"]
        x = self.x[p] - w / 2
        d.rect(x, self.y + 4, w, h, f, st, sw=1.1, rx=8)
        for k, s in enumerate(lines):
            d.text(self.x[p], self.y + 4 + 22 + 19 * k, s, 13.5, INK, 400, "middle")
        mid = self.y + 4 + h / 2
        if key:
            self.key(key, mid + 4)
        self.y += h + 16
        return mid

    def note(self, a, b, text, ramp="purple", kind=None, key=None):
        """横跨 a..b 的备注条。kind='warn' 时用珊瑚色边框（提交点）。"""
        d = self.d
        f, st, tc, _ = RAMPS[ramp]
        if kind:
            st = tc = ARROWS[kind][0]
            f = "#FFFFFF"
        x0, x1 = self.x[a] - self.step / 2 + 8, self.x[b] + self.step / 2 - 8
        d.rect(x0, self.y + 4, x1 - x0, 32, f, st, sw=1.4, rx=8, dash=None)
        d.text((x0 + x1) / 2, self.y + 25, text, 14.5, tc, 600, "middle")
        mid = self.y + 20
        if key:
            self.key(key, mid + 4)
        self.y += 48
        return mid

    def key(self, text, y, color=SUB):
        self.d.text(self.KEY_X, y + 4, text, 13.5, color, 600 if color != SUB else 400)

    # ----- 循环框
    def loop_begin(self, a, b, label):
        self.loops.append((a, b, label, self.y))
        self.y += 34

    def loop_end(self, key=None):
        d = self.d
        a, b, label, y0 = self.loops.pop()
        x0, x1 = self.x[a] - self.step / 2 + 14, self.x[b] + self.step / 2 - 14
        y1 = self.y + 2
        d.rect(x0, y0 + 4, x1 - x0, y1 - y0 - 4, "none", RAMPS["purple"][1], sw=1.2, rx=8, dash="6 4")
        w = text_w(label, 13) + 22
        d.rect(x0, y0 + 4, w, 24, RAMPS["purple"][0], RAMPS["purple"][1], sw=1, rx=8)
        d.text(x0 + w / 2, y0 + 21, label, 13, RAMPS["purple"][2], 600, "middle")
        if key:
            self.key(key, (y0 + y1) / 2)
        self.y = y1 + 12

    # ----- 跨多行的计时括号（画在最右侧）
    def bracket(self, y0, y1, x, label_top, label_bot):
        self.brackets.append((y0, y1, x, label_top, label_bot))

    def end(self):
        d = self.d
        col = RAMPS["purple"][1]
        for y0, y1, x, lt, lb in self.brackets:
            d.o.append(f'<path d="M{x - 8} {y0} H{x} V{y1} H{x - 8}" fill="none" stroke="{col}" stroke-width="1.8"/>')
            if lt:
                self.key(lt, y0 - 4, RAMPS["purple"][2])
            if lb:
                self.key(lb, y1 - 4, RAMPS["purple"][2])
        bottom = self.y + 6
        lines = [f'<line x1="{x:.1f}" y1="{self.top}" x2="{x:.1f}" y2="{bottom}" stroke="#B4B2A9" '
                 f'stroke-width="1.2" stroke-dasharray="4 4"/>' for x in self.x]
        d.o[self.lifeline_at:self.lifeline_at] = lines
        d.y = bottom


def d13_1():
    d = Diagram()
    d.header("一次 checkpoint 的跨组件时序：请求进到 guest，由 gsd 独自完成",
             "宿主代理只在最外圈转发；虚机全程不暂停，Firecracker 不参与。用户进程只在 CRIU 冻结到 SIGCONT 之间不运行。",
             legend=[("flow", "调用、读写"), ("warn", "冻结用户进程"), ("back", "返回、放开进程")])
    q = Seq(d)
    q.msg(S, P, "CreateCheckpoint")
    q.msg(P, G, "按端口 49984 原样转发")
    q.msg(G, K, "读 MMDS，校验 access token")
    y_lock = q.act(G, "排队取操作锁，然后检查健康状态")
    q.msg(G, K, "扫描 /proc，找根，记录各进程状态", key="find_pids_ms")
    q.act(G, "多树预检、容量预检", key="precheck_ms")
    q.note(G, U, "以上什么都没动", ramp="gray")
    q.loop_begin(G, U, "逐棵：按根进程号升序")
    q.msg(G, C, "criu dump --leave-stopped")
    q.msg(C, U, "冻结，写镜像，清 soft-dirty，留在停止状态", "warn")
    y_first = q.msg(C, G, "退出码", "back")
    q.loop_end(key="criu_dump_ms（各棵之和）")
    q.note(G, U, "全部树停着")
    q.msg(G, K, "upper 改名成层，再发 ckpt 模式的 ioctl", key="overlay_sink_ms")
    q.msg(G, K, "对停着的进程写 clear_refs")
    y_cont = q.msg(G, U, "SIGCONT", "back")
    q.note(G, U, "用户进程恢复运行", ramp="gray")
    y_resp = q.act(G, "量镜像大小，更新增量父镜像，条目入池，组响应")
    q.act(G, "defer：写 state.json，放锁")
    q.msg(G, S, "经代理返回 checkpoint_id 与 timings", "back")
    q.bracket(y_first, y_cont, d.W - d.M - 26, "stopped_ms 起", "stopped_ms 止")
    q.bracket(y_lock, y_resp, d.W - d.M, "total_ms 起", "total_ms 止")
    q.end()
    d.footer("注：total_ms 从拿到操作锁算起，stopped_ms 从第一棵树 dump 完算起。SDK 量到的墙钟时间 = 网络与代理 + 鉴权 + 排队 + total_ms，再加末尾写 state.json 的时间。"
             "stopped_ms 与 criu_dump_ms、overlay_sink_ms 有重叠，不能相加。各步骤的细节见第 11 章。")
    d.save(out("gsd", "13-1-checkpoint时序"))


def d13_2():
    d = Diagram()
    d.header("一次 restore 的跨组件时序：杀进程之后的失败都是 DataLoss",
             "检查与准备都在杀进程之前；杀掉当前用户进程是提交点。用户进程从被杀到被 criu restore 恢复出来之间不存在。",
             legend=[("flow", "调用、读写"), ("warn", "杀掉用户进程"), ("back", "返回、恢复进程")])
    q = Seq(d)
    q.msg(S, P, "RestoreCheckpoint")
    q.msg(P, G, "按端口 49984 原样转发")
    q.msg(G, K, "读 MMDS，校验 access token")
    y_lock = q.act(G, "排队取操作锁，检查健康状态，查条目")
    q.act(G, "检查镜像与父链，建好新的 upper 与 work", key="precheck_ms")
    q.msg(G, K, "扫描 /proc，列出全部用户进程", key="find_pids_ms")
    q.note(G, U, "以上什么都没动", ramp="gray")
    q.msg(G, U, "先 SIGSTOP 冻住，清点，再 SIGKILL，等进程号释放", "warn", key="kill_ms")
    q.note(G, U, "提交点：之后的失败都是 DataLoss", kind="warn")
    q.msg(G, K, "restore 模式的 ioctl，切回目标层栈", key="overlay_restore_ms")
    q.loop_begin(G, U, "逐棵：按 dump 时的顺序")
    q.msg(G, C, "criu restore --restore-detached")
    q.msg(C, U, "按原进程号重建，写回内存，重新打开文件", "back")
    q.msg(C, G, "退出码与根进程号", "back")
    q.loop_end(key="criu_restore_ms（各棵之和）")
    y_resp = q.act(G, "重建增量父镜像，解除 degraded，组响应")
    q.act(G, "defer：写 state.json，放锁")
    q.msg(G, S, "经代理返回 success 与 timings", "back")
    q.bracket(y_lock, y_resp, d.W - d.M, "total_ms 起", "total_ms 止")
    q.end()
    d.footer("注：total_ms 从拿到操作锁算起。precheck_ms 与 find_pids_ms 在 restore 里的顺序与 checkpoint 相反、内容也不同，不要跨操作比较。"
             "目标没有进程镜像时，切换文件系统之后就结束，没有 criu restore。各步骤的细节见第 12 章。")
    d.save(out("gsd", "13-2-restore时序"))


# ---------------------------------------------------------------- 13-3 三棵树的冻结区间
def d13_3():
    d = Diagram()
    d.header("三棵树的 checkpoint：先 dump 的树停得最久",
             "根进程号从小到大是 A、B、C，各自的 dump 耗时是 dA、dB、dC。CRIU 一次 dump 一棵，"
             "dump 完的树被 --leave-stopped 留在停止状态，直到全部下沉、重写 clear_refs 之后一起放开。")
    M, W = d.M, d.W
    run_f, run_s = "#FFFFFF", RAMPS["gray"][1]
    frz_f, frz_s, frz_t, _ = RAMPS["purple"]
    # 图例
    ly = d.y + 30
    lx = M
    for name, f, s, dash in [("运行", run_f, run_s, None), ("被 CRIU 冻结（自己的 dump）", frz_s, frz_s, None),
                             ("停着（--leave-stopped）", frz_f, frz_s, "5 4")]:
        d.rect(lx, ly - 12, 34, 16, f, s, sw=1.2, rx=3, dash=dash)
        d.text(lx + 44, ly + 1, name, 13.5, SUB)
        lx += 44 + text_w(name, 13.5) + 40
    d.y = ly + 10

    LX = M + 150            # 时间轴起点
    seg = [("dA", 250), ("dB", 250), ("dC", 250), ("下沉 + clear_refs", 160)]
    xs = [LX]
    for _, w in seg:
        xs.append(xs[-1] + w)
    xend = xs[-1]
    xrun = xend + 110       # 放开之后再画一小段运行
    top = d.y + 34
    # 第一行：gsd 在做什么
    d.text(M, top + 27, "gsd 在做什么", 15, INK, 600)
    labels = ["criu dump A", "criu dump B", "criu dump C", "下沉 + clear_refs"]
    for i, lab in enumerate(labels):
        f, s, tc, _ = RAMPS["gray"]
        d.rect(xs[i] + 3, top, xs[i + 1] - xs[i] - 6, 40, f, s, sw=1.1, rx=6)
        d.text((xs[i] + xs[i + 1]) / 2, top + 25, lab, 14, tc, 600, "middle")
    d.rect(xend + 3, top, xrun - xend - 6, 40, "#FFFFFF", ARROWS["back"][0], sw=1.1, rx=6)
    d.text((xend + xrun) / 2, top + 25, "SIGCONT", 14, ARROWS["back"][0], 600, "middle")
    # 刻度：各段名称
    ty = top + 72
    for i, (name, _) in enumerate(seg):
        d.text((xs[i] + xs[i + 1]) / 2, ty, f"← {name} →" if i < 3 else name, 14, SUB, 600, "middle")
    # 三棵树
    rows = [("A", "根进程号最小", 0), ("B", "", 1), ("C", "根进程号最大", 2)]
    ry = ty + 22
    RH, RG = 44, 22
    vline_at = len(d.o)
    for name, sub, k in rows:
        y = ry
        d.text(M, y + (RH / 2 if not sub else RH / 2 - 3), f"树 {name}", 16, INK, 600)
        if sub:
            d.text(M, y + RH / 2 + 15, sub, 12.5, MUTED)
        # 运行段
        if k > 0:
            d.rect(xs[0], y, xs[k] - xs[0], RH, run_f, run_s, sw=1.2, rx=4)
            d.text((xs[0] + xs[k]) / 2, y + 27, "运行", 14, SUB, 400, "middle")
        # 冻结段
        d.rect(xs[k], y, xs[k + 1] - xs[k], RH, frz_s, frz_s, sw=1.2, rx=4)
        d.text((xs[k] + xs[k + 1]) / 2, y + 27, "冻结", 14, "#FFFFFF", 600, "middle")
        # 停着段
        d.rect(xs[k + 1], y, xend - xs[k + 1], RH, frz_f, frz_s, sw=1.2, rx=4, dash="5 4")
        d.text((xs[k + 1] + xend) / 2, y + 27, "停着", 14, frz_t, 600, "middle")
        # 放开之后
        d.rect(xend, y, xrun - xend, RH, run_f, run_s, sw=1.2, rx=4)
        d.text((xend + xrun) / 2, y + 27, "放开，运行", 14, ARROWS["back"][0], 600, "middle")
        ry += RH + RG
    rows_bottom = ry - RG
    # 两条竖线：tFirstStopped、SIGCONT
    for x, lab in [(xs[1], "tFirstStopped"), (xend, "SIGCONT")]:
        d.o.insert(vline_at, f'<line x1="{x}" y1="{ty + 12}" x2="{x}" y2="{rows_bottom + 30}" stroke="{INK}" '
                   f'stroke-width="1.6" stroke-dasharray="3 3"/>')
        d.text(x, rows_bottom + 50, lab, 14, INK, 600, "middle")

    # 区间括号
    def span(y, x0, x1, label, color):
        d.o.append(f'<path d="M{x0} {y - 8} V{y} H{x1} V{y - 8}" fill="none" stroke="{color}" stroke-width="1.8"/>')
        d.text(x1 + 14, y + 5, label, 14, color, 600)

    by = rows_bottom + 92
    PC = RAMPS["purple"][2]
    span(by, xs[0], xs[3], "criu_dump_ms = dA + dB + dC", INK)
    span(by + 40, xs[1], xend, "stopped_ms = dB + dC + 下沉 + clear_refs", PC)
    span(by + 80, xs[0], xend, "A 不在运行 = dA + stopped_ms，约等于整个 criu_dump_ms", PC)
    span(by + 120, xs[2], xend, "C 不在运行 ≈ dC + 零头", PC)
    d.y = by + 130
    d.footer("注：各段宽度只表示先后，不按比例。2026-10-08 一轮多树测量（新 920B，每棵 64 MiB，首次全量，8 轮中位数，早于最终提交）："
             "三棵树时 criu_dump_ms 138.87、stopped_ms 91.82，差约 47 ms，与单棵全量 dump 的 49.66 ms 相近，差的正是 A 自己的 dump 时间。")
    d.save(out("gsd", "13-3-多树冻结区间"))


if __name__ == "__main__":
    d13_1()
    d13_2()
    d13_3()
