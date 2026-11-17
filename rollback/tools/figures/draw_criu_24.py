"""进程级 24：24-1 两套方案在同一沙箱里分别作用于哪一层（替换 §1 的 mermaid）；24-2 成本模型的形状对比（新增，§3）；
24-3 920B 同平台对比，两套方案的分档折线画在同一坐标（新增，§4，数据在 d24_3 前的常量里）。
§5 的 950 对比里进程级全是待填，不画 24-4。"""
from common import *  # noqa: F401,F403
from diagram_kit import RAMPS, ARROWS, INK, SUB, MUTED, text_w, wrap

TEAL, PURPLE = RAMPS["teal"], RAMPS["purple"]
ARROWS["sbx"] = (TEAL[1], None)
ARROWS["proc"] = (PURPLE[1], None)


def poly(d, pts, kind):
    col, dash = ARROWS[kind]
    d.kinds.add(kind)
    p = "M" + " L".join(f"{x:.1f} {y:.1f}" for x, y in pts)
    d.o.append(f'<path d="{p}" fill="none" stroke="{col}" stroke-width="1.8" stroke-linejoin="round" '
               f'marker-end="url(#ar-{kind})"/>')


def panel(d, x, y, w, h, name, note=None, stroke="#E2E0D8", tc=INK, sw=1):
    d.rect(x, y, w, h, "#FAFAF7", stroke, sw=sw, rx=12)
    d.text(x + 18, y + 28, name, 16, tc, 600)
    if note:
        d.text(x + 18 + text_w(name, 16) + 14, y + 28, note, 13.5, tc if tc != INK else MUTED)


d = Diagram()
d.header("沙箱级在虚拟机外恢复整台虚拟机，进程级在虚拟机里只恢复用户进程和文件",
         "SDK 入口和端口号相同，请求在宿主的代理处由不同组件应答。沙箱级的 Firecracker 操作整个“沙箱内部”方框；"
         "进程级只触及方框里的用户进程树和文件系统。",
         legend=[("flow", "请求入口"), ("sbx", "沙箱级的路径"), ("proc", "进程级的路径")])
d.section(1, "两套各自在哪一侧动手", highlight="本图重点")

M, W = d.M, d.W
bw, gap = 280, 96
col = [88 + i * (bw + gap) for i in range(4)]
mid = lambda i: col[i] + bw / 2

# 调用方
cp = d.y + 8
sy = cp + 22
sh = 84
panel(d, M, cp, W - 2 * M, sh + 44, "调用方")
d.block(col[2] - 50, sy, bw + 100, sh, "Python SDK 的 checkpoint 接口", "两套的入口和端口号相同")

# 宿主
hp = cp + sh + 44 + 46
h1 = hp + 46
hh = 96
h2 = h1 + hh + 56
host_bot = h2 + hh + 22
panel(d, M, hp, W - 2 * M, host_bot - hp, "宿主")
d.block(col[2], h1, bw, hh, "代理", "按端口 49984 寻址")
d.block(col[1], h1, bw, hh, "orchestrator", "沙箱级的服务与账本", ramp="teal")
d.block(col[1], h2, bw, hh, "Firecracker（分叉版）", "读写整台虚拟机的内存与设备状态", ramp="teal")
d.block(col[0], h2, bw, hh, "宿主本地存储", "内存差分与磁盘层", ramp="teal", style="outline")
d.arrow(mid(2), sy + sh + 4, mid(2), h1 - 6, "flow", "HTTP 49984")
d.arrow(col[2] - 4, h1 + hh / 2, col[1] + bw + 6, h1 + hh / 2, "sbx")
d.text((col[1] + bw + col[2]) / 2, h1 + hh / 2 - 10, "在宿主截下", 13, TEAL[3], 600, "middle")
d.arrow(mid(1), h1 + hh + 4, mid(1), h2 - 6, "sbx")
d.arrow(col[1] - 4, h2 + hh / 2, col[0] + bw + 6, h2 + hh / 2, "sbx")

# 沙箱内部
gp = host_bot + 64
r1 = gp + 54
rh = 104
r2 = r1 + rh + 62
r3 = r2 + rh + 62
g_bot = r3 + rh + 26
panel(d, M, gp, W - 2 * M, g_bot - gp, "沙箱内部", "沙箱级 restore：方框里的一切一起回到 checkpoint 时刻",
      stroke=TEAL[1], tc=TEAL[2], sw=2.4)
d.block(col[2], r1, bw, rh, "gsd", "进程级的服务与状态", ramp="purple")
d.block(col[1], r2, bw, rh, "用户进程树", "进程级回滚的对象：杀掉后按镜像重建", ramp="purple", style="outline")
d.block(col[2], r2, bw, rh, "CRIU", "保存与重建用户进程", ramp="purple")
d.block(col[3], r2, bw, rh, "guest 内核（打了补丁）", "soft-dirty 与 overlay 热切层", ramp="purple")
d.block(col[0], r3, col[1] + bw - col[0], rh, "内核、PID 1、envd、系统服务",
        "进程级不动，照常运行；沙箱级 restore 时随整台虚拟机一起恢复", style="dashed")
d.block(col[2], r3, col[3] + bw - col[2], rh, "沙箱内的镜像目录与层目录", "进程级的状态存在沙箱内部",
        ramp="purple", style="outline")

# 连线
d.arrow(mid(2), h1 + hh + 4, mid(2), r1 - 6, "proc")
d.text(mid(2) + 12, (h1 + hh + r1) / 2 + 10, "转进沙箱", 13, PURPLE[3], 600)
d.arrow(mid(1), h2 + hh + 4, mid(1), gp - 6, "sbx")
d.text(mid(1) + 12, (h2 + hh + gp) / 2 + 5, "整台虚拟机", 13, TEAL[3], 600)
d.arrow(mid(2), r1 + rh + 4, mid(2), r2 - 6, "proc")
poly(d, [(col[2] + bw + 4, r1 + rh / 2), (mid(3), r1 + rh / 2), (mid(3), r2 - 6)], "proc")
d.arrow(col[2] - 4, r2 + rh / 2, col[1] + bw + 6, r2 + rh / 2, "proc")
d.text((col[1] + bw + col[2]) / 2, r2 + rh / 2 - 10, "dump、重建", 13, PURPLE[3], 600, "middle")
d.arrow(mid(2), r2 + rh + 4, mid(2), r3 - 6, "proc")
d.arrow(mid(3), r2 + rh + 4, mid(3), r3 - 6, "proc")
d.y = g_bot

d.footer("注：两套的入口是同一个端口，同一时刻一台宿主上的这个端口只能由其中一套应答（§8）。"
         "沙箱级的事实以沙箱级手册为准。")
d.save(out("criu", "24-1-两套方案作用的层"))


# ---------------------------------------------------------------- 24-2 成本模型的形状
def shape_panel(d, x, y, w, h, title, xlabel, lines, captions, xticks=None):
    """一个示意坐标：没有数值刻度。lines: [(色阶, [(fx, fy), ...], 虚线?)]，坐标按 0～1 归一。"""
    panel(d, x, y, w, h, title)
    px0, py0 = x + 64, y + 66                 # 绘图区左上
    pw, ph = w - 64 - 36, 200
    ax = "#888780"
    d.o.append(f'<line x1="{px0}" y1="{py0 + ph}" x2="{px0 + pw}" y2="{py0 + ph}" stroke="{ax}" stroke-width="1.4"/>')
    d.o.append(f'<line x1="{px0}" y1="{py0}" x2="{px0}" y2="{py0 + ph}" stroke="{ax}" stroke-width="1.4"/>')
    d.text(px0 - 12, py0 + 6, "耗时", 13, MUTED, 400, "end")
    d.text(px0 + pw, py0 + ph + 26, xlabel, 13.5, SUB, 400, "end")
    if xticks:
        for fx, lab in xticks:
            tx = px0 + fx * pw
            d.o.append(f'<line x1="{tx:.1f}" y1="{py0 + ph}" x2="{tx:.1f}" y2="{py0 + ph + 6}" stroke="{ax}" stroke-width="1.2"/>')
            d.text(tx, py0 + ph + 26, lab, 13.5, SUB, 400, "middle")
    for ramp, pts, dash in lines:
        col = RAMPS[ramp][1]
        p = " L".join(f"{px0 + fx * pw:.1f} {py0 + ph - fy * ph:.1f}" for fx, fy in pts)
        da = ' stroke-dasharray="7 5"' if dash else ""
        d.o.append(f'<path d="M{p}" fill="none" stroke="{col}" stroke-width="3" stroke-linecap="round"{da}/>')
        if not dash:
            for fx, fy in pts:
                d.o.append(f'<circle cx="{px0 + fx * pw:.1f}" cy="{py0 + ph - fy * ph:.1f}" r="4" fill="{col}"/>')
    cy = py0 + ph + 62
    for ramp, text in captions:
        col = RAMPS[ramp][3] if ramp != "gray" else MUTED
        for k, s in enumerate(wrap(text, w - 36, 14)):
            d.text(x + 18, cy, s, 14, col, 600 if ramp != "gray" else 400)
            cy += 21
        cy += 6
    return cy


def d24_2():
    d = Diagram()
    d.header("两套 restore 的耗时由不同的量决定：沙箱级看回滚量，进程级看进程大小和树数",
             "示意图：只画随什么增长，没有数值刻度；两条线的高低和交点不表示哪套更快。R：restore 时当前状态与目标之间的回滚量；"
             "P：被保存的用户进程自己的内存大小（一棵树）；N：用户进程树的数量。")
    # 图例：两条线（不带箭头）
    ly = d.y + 30
    lx = d.M
    for ramp, name in (("teal", "沙箱级"), ("purple", "进程级（gsd）")):
        d.o.append(f'<line x1="{lx}" y1="{ly - 5}" x2="{lx + 40}" y2="{ly - 5}" stroke="{RAMPS[ramp][1]}" '
                   f'stroke-width="3" stroke-linecap="round"/>')
        d.text(lx + 50, ly, name, 13.5, SUB)
        lx += 50 + text_w(name, 13.5) + 40
    d.y = ly + 2

    d.section(1, "每次只变一个量，看两套的 restore 怎么变", highlight="本图重点")
    gap = 28
    pw = (d.W - 2 * d.M - 2 * gap) / 3
    top = d.y + 6
    ph = 400
    xs = [d.M + i * (pw + gap) for i in range(3)]
    ends = []
    ends.append(shape_panel(d, xs[0], top, pw, ph, "横轴：回滚量 R", "回滚量 R →",
                            [("teal", [(0, 0.12), (1, 0.5)], False), ("purple", [(0, 0.66), (1, 0.66)], False)],
                            [("teal", "沙箱级：固定开销 + 与 R 成正比，退得近就快"),
                             ("purple", "进程级：杀进程 + 与 P 成正比，与改动量无关，只退一步也要整个重建")]))
    ends.append(shape_panel(d, xs[1], top, pw, ph, "横轴：进程内存 P", "进程内存 P →",
                            [("teal", [(0, 0.4), (1, 0.4)], False), ("purple", [(0, 0.12), (1, 0.88)], False)],
                            [("teal", "沙箱级：R 不变时，与进程多大无关"),
                             ("purple", "进程级：重建时读回全部内存，杀进程也随内存增长")]))
    ends.append(shape_panel(d, xs[2], top, pw, ph, "横轴：进程树数量 N", "",
                            [("teal", [(0.2, 0.35), (0.96, 0.35)], False),
                             ("purple", [(0.2, 0.3), (0.45, 0.55), (0.7, 0.8)], False),
                             ("purple", [(0.7, 0.8), (0.96, 0.98)], True)],
                            [("teal", "沙箱级：与树数无关"),
                             ("purple", "进程级：逐棵串行，checkpoint 和 restore 都随 N 线性增长"),
                             ("gray", "虚线：依据只到三棵树")],
                            xticks=[(0.2, "1"), (0.45, "2"), (0.7, "3"), (0.96, "N →")]))
    d.y = top + ph

    d.footer("注：checkpoint 两套都随改动量增长，沙箱级与整台 guest 改过的内存量 D 成正比，进程级与其中属于用户进程的 d 成正比（d ≤ D）；"
             "两者的单位数据处理代价不同，不画在同一坐标里，实测见 §4。第一次 checkpoint 都是全量：沙箱级与 guest 内存 M 成正比，"
             "进程级与 P 成正比。文件一侧两套都不搬数据。")
    d.save(out("criu", "24-2-成本模型的形状"))


d24_2()


# ---------------------------------------------------------------- 24-3 920B 同平台对比
# 数据（以后只改这里）。客户端墙钟，单位 ms。None = 待填，不画点。
# 档位：总改动 MiB 与 内存 / 文件 MiB，来源：criu-rollback/docs/24-vs-sandbox-level.md §4.3 表（第 207～212 行）第 1、2 列
CMP920_TIERS = [("0", "0 / 0"), ("16", "12 / 4"), ("32", "24 / 8"), ("64", "48 / 16"),
                ("128", "96 / 32"), ("256", "192 / 64")]
CMP920 = {
    # 来源：criu-rollback/docs/24-vs-sandbox-level.md §4.3 表（第 207～212 行）
    "增量 checkpoint": {
        "沙箱级 p50": [14.9, 19.2, 23.3, 34.4, 55.9, 97.3],
        "沙箱级 p99": [19.5, 23.5, 29.1, 40.4, 63.3, 118.7],
        "进程级 10-16 p50": [12.5, 20.1, 28.6, 39.7, 62.1, 108.9],
        "进程级 11-06 p50": [11.3, 27.2, 27.2, 42.3, 67.1, 120.0],
    },
    # 来源：criu-rollback/docs/24-vs-sandbox-level.md §4.4 表（第 220～225 行）
    "restore": {
        "沙箱级 p50": [18.1, 21.4, 25.1, 30.8, 43.6, 66.1],
        "沙箱级 p99": [23.8, 27.2, 31.1, 36.3, 49.5, 71.8],
        "进程级 10-16 p50": [11.2, 16.8, 21.8, 33.1, 54.6, 96.4],
        "进程级 11-06 p50": [10.9, 17.8, 21.6, 32.6, 53.8, 96.8],
    },
}
# 样本量，来源：§4.1 表“每档样本”一行（第 183 行）
CMP920_N = "沙箱级每档 n = 100（0～64 MiB）、60（128、256 MiB）；进程级每档 n = 3"


def d24_3():
    from draw_criu_20 import tier_chart
    # 只画三条线；虚线只表示“进程级的另一轮”。沙箱级 p99 只进数值表
    style = {"沙箱级 p50": ("teal", "line"),
             "进程级 10-16 p50": ("purple", "line"), "进程级 11-06 p50": ("purple", "dash")}
    d = Chart()
    d.header("920B 同平台：两套方案的增量 checkpoint 与 restore 客户端墙钟随档位变化",
             "新 920B，客户端墙钟，单位 ms。沙箱级 2026-09-29；进程级 2026-10-16、2026-11-06 两轮，取三轮中位数。"
             + CMP920_N + "。两侧的内存与文件负载、沙箱与次序、缓存状态、模板和脏页后端（kvm-wp 与 soft-dirty）"
             "都不同（§4.6），图中的高低只描述这组实测。")
    d.legend_row([(k, r, s) for k, (r, s) in style.items()])
    xl = [f"{t} MiB" for t, _ in CMP920_TIERS]
    xs = [f"内存 / 文件 {s}" for _, s in CMP920_TIERS]
    notes = {"增量 checkpoint": "§4.3", "restore": "§4.4，改动之后回到全量基点"}
    for k, op in enumerate(CMP920, 1):
        d.section(k, op, note=notes[op] + "；横轴是总改动量，按档位等距排列")
        S = [(name, style[name][0], vs, style[name][1]) for name, vs in CMP920[op].items() if name in style]
        T = [("沙箱级 p99（未画线）", "teal", CMP920[op]["沙箱级 p99"], "none")]
        tier_chart(d, xl, xs, S, (0, 125), table_only=T)
    d.footer("注：沙箱级 p99 只列在数值表里，未画线；n ≤ 100 时 p99 即最大值。进程级 n = 3 没有 p99，三轮的最小值～最大值见正文表。"
             "第一次全量两侧不是同一个量，不在图中（§4.5）。950 的同机对比见 §5 与图 24-4。")
    d.save(out("criu", "24-3-920B同平台对比"))



# ---------------------------------------------------------------- 24-4 950 同机对比
# 数据（以后只改这里）。客户端墙钟，单位 ms。
# 来源：criu-rollback/docs/24-vs-sandbox-level.md §5.1 表（2026-11-17 test950.sh standard 同一轮）
CMP950 = {
    "增量 checkpoint": {
        "沙箱级 p50": [22.1, 26.2, 32.3, 42.2, 60.1, 107.0],
        "沙箱级 p99": [31.1, 40.1, 45.1, 58.0, 89.6, 151.5],
        "进程级 p50": [9.4, 14.9, 23.0, 34.1, 51.7, 91.0],
    },
    "restore": {
        "沙箱级 p50": [21.3, 26.4, 29.7, 38.3, 68.5, 93.2],
        "沙箱级 p99": [32.2, 36.7, 38.7, 56.9, 94.1, 128.2],
        "进程级 p50": [9.7, 15.1, 24.1, 31.9, 47.5, 82.4],
    },
}
CMP950_N = "沙箱级每档 n = 30（0～32 MiB）、20（64～256 MiB）；进程级每档 n = 3"


def d24_4():
    from draw_criu_20 import tier_chart
    style = {"沙箱级 p50": ("teal", "line"), "进程级 p50": ("purple", "line")}
    d = Chart()
    d.header("950 同机：两套方案的增量 checkpoint 与 restore 客户端墙钟随档位变化",
             "950（Kunpeng 950 7592C），2026-11-17 test950.sh standard 同一轮：沙箱级 16:48 起，进程级 17:03 起。"
             "客户端墙钟，单位 ms；进程级取三轮中位数。" + CMP950_N + "。两侧的内存与文件负载、沙箱与次序、模板和脏页后端"
             "（hdbss 与 soft-dirty）都不同（§4.6），图中的高低只描述这组实测。")
    d.legend_row([(k, r, s) for k, (r, s) in style.items()])
    xl = [f"{t} MiB" for t, _ in CMP920_TIERS]
    xs = [f"内存 / 文件 {s}" for _, s in CMP920_TIERS]
    notes = {"增量 checkpoint": "§5.1", "restore": "§5.1，改动之后回到全量基点"}
    for k, op in enumerate(CMP950, 1):
        d.section(k, op, note=notes[op] + "；横轴是总改动量，按档位等距排列")
        S = [(name, style[name][0], vs, style[name][1]) for name, vs in CMP950[op].items() if name in style]
        T = [("沙箱级 p99（未画线）", "teal", CMP950[op]["沙箱级 p99"], "none")]
        tier_chart(d, xl, xs, S, (0, 125), table_only=T)
    d.footer("注：沙箱级 p99 只列在数值表里，未画线；n ≤ 30 时 p99 即最大值。进程级 n = 3 没有 p99。"
             "第一次全量两侧不是同一个量，不在图中。")
    d.save(out("criu", "24-4-950同机对比"))


d24_3()
d24_4()
