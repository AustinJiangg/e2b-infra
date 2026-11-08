"""19 章 §5 的数据图：原生 pause / resume 与 checkpoint / restore 按档画在同一坐标。

- 图 19-1：950（§5.1）。图 19-2：920B（§5.2）。正文把两台平台分在两节、条件和读法不同
  （950 列倍数但只说明量级；920B 跨构建与产物盘，只列数字不算倍数），所以各画一张。
- 颜色按方案：teal = checkpoint / restore（本方案），coral = 原生 pause / resume；平台写在标题里。
- 横轴按档位顺序等距排列（0、16、32、64、128、256 MiB），图里注明。
- 数据集中在下面的 P950、P920B 两个常量，以后重测只改这里。
"""
from common import *  # noqa: F401,F403
from draw_rollback_20 import tier_panel

TIERS = ["0", "16", "32", "64", "128", "256"]   # 档（总改动 MiB）

# 来源：rollback/docs/19-native-pause-resume.md §5.1 表（第 185～190 行）。客户端墙钟 p50，ms。
# checkpoint / restore 列即 rollback/docs/18-950-performance.md §4 表混合 0～256 档（第 86～93 行），
# 每档 n 取自同一表：30 / 30 / 30 / 20 / 20 / 20。pause / resume 为本章 §3.1 的 repeat，每档 n = 15。
P950 = {
    "checkpoint": [17.2, 23.8, 30.5, 41.4, 73.0, 125.5],
    "pause": [251.9, 262.7, 284.6, 308.8, 346.8, 429.9],
    "save_ratio": [14.6, 11.0, 9.3, 7.5, 4.8, 3.4],          # 正文表“倍数”列（pause / checkpoint）
    "restore": [22.4, 30.4, 36.4, 46.0, 67.8, 105.6],
    "resume": [109.3, 107.2, 108.8, 106.1, 108.9, 120.7],
    "restore_ratio": [4.9, 3.5, 3.0, 2.3, 1.6, 1.1],          # 正文表“倍数”列（resume / restore）
    "ck_n": [30, 30, 30, 20, 20, 20],
}

# 来源：同章 §5.2 表（第 206～211 行）。客户端墙钟 p50，ms。checkpoint / restore 列即
# rollback/docs/20-920b-performance.md §2.2 混合档（第 66～74 行，每档 n 为 100 / 100 / 100 / 100 / 60 / 60）；
# pause / resume 列即本章 §4.1 精确增量构建（第 129～134 行），每档 n = 15。正文不算倍数，这里也不画倍数。
P920B = {
    "checkpoint": [14.9, 19.2, 23.3, 34.4, 55.9, 97.3],
    "pause": [277.2, 297.3, 311.2, 333.6, 361.5, 446.4],
    "restore": [18.1, 21.4, 25.1, 30.8, 43.6, 66.1],
    "resume": [87.3, 89.7, 94.0, 93.9, 96.2, 94.7],
    "ck_n": [100, 100, 100, 100, 60, 60],
}

MS = lambda t: f"{t:g} ms"
F1 = lambda vs: [f"{v:.1f}" for v in vs]


def two_panels(d, data, rec_ylim, rec_ticks, ratios):
    """左：checkpoint 与 pause；右：restore 与 resume。ratios=True 时在数值行下加正文的倍数行。"""
    gap = 70
    pw = (d.W - 2 * d.M - gap) / 2
    lab_w = 118
    xs = ["MiB"] * len(TIERS)
    top = d.y + 64
    bottoms = []
    for k, (title, a, b, ylim, ticks, rkey) in enumerate((
            ("保存：checkpoint 与 pause", "checkpoint", "pause", (0, 500), [0, 100, 200, 300, 400, 500], "save_ratio"),
            ("恢复：restore 与 resume", "restore", "resume", rec_ylim, rec_ticks, "restore_ratio"))):
        px = d.M + k * (pw + gap)
        x0, x1 = px + lab_w, px + pw
        rows = [(a, "sandbox", F1(data[a])), (b, "native", F1(data[b]))]
        if ratios:
            rows.append(("倍数（正文）", None, F1(data[rkey])))
        bottoms.append(tier_panel(d, x0, x1, top, 250, TIERS,
                                  [(a, "sandbox", data[a], "line", False), (b, "native", data[b], "line", False)],
                                  ylim, ticks, yfmt=MS, xsub=xs, rows=rows, title=title, title_x=px))
    d.y = max(bottoms)


def fig_950():
    d = Chart()
    d.header("950：checkpoint / restore 与原生 pause / resume 按档的 p50",
             "950，客户端墙钟 p50，ms。checkpoint / restore 取第 18 章 §4 混合档（2026-10-13，内核 6.6.0-159.4.12.166.oe2403sp4，"
             "部署身份待补，每档 n = 30 或 20）；pause / resume 取本章 §3.1（2026-11-06，hdbss，每档 n = 15）。"
             "两组负载构造和宿主条件不同，只说明量级，不能当作同条件的精确倍数。横轴按档位顺序等距排列。")
    d.y += 10
    d.legend_row([("checkpoint / restore（本方案）", "sandbox", "line"), ("原生 pause / resume", "native", "line")])
    two_panels(d, P950, (0, 140), [0, 20, 40, 60, 80, 100, 120, 140], ratios=True)
    d.footer("注：横轴是档（总改动 MiB，内存:文件 = 3:1）。256 MiB 档 restore 105.6 与 resume 120.7 接近，含义不同："
             "restore 返回时内存已写回、保留原 Firecracker 进程、之前的 checkpoint 仍然有效；resume 返回时页还没有加载，"
             "之后读这些页要再付约 112 ms，并换一代新进程，之前的 checkpoint 全部失效。")
    d.save(out("rollback", "19-1-950原生与checkpoint按档"))


def fig_920b():
    d = Chart()
    d.header("920B：checkpoint / restore 与原生 pause / resume 按档的 p50",
             "920B，kvm-wp，客户端墙钟 p50，ms。checkpoint / restore 取第 20 章 §2.2（2026-09-29，deltabox-dev@8ea5322bf，"
             "产物在 ext4 真盘，每档 n = 100 或 60）；pause / resume 取本章 §4.1 精确增量构建（2026-11-06，"
             "deltabox-dev@57a30632c，模板目录在 tmpfs，每档 n = 15）。两列的产物盘和构建都不同（§5.2），只列数字，不算倍数。"
             "横轴按档位顺序等距排列。")
    d.y += 10
    d.legend_row([("checkpoint / restore（本方案）", "sandbox", "line"), ("原生 pause / resume", "native", "line")])
    two_panels(d, P920B, (0, 120), [0, 20, 40, 60, 80, 100, 120], ratios=False)
    d.footer("注：横轴是档（总改动 MiB，内存:文件 = 3:1）。pause / resume 列与同轮对照构建的比较见本章 §4.1。")
    d.save(out("rollback", "19-2-920B原生与checkpoint按档"))


if __name__ == "__main__":
    fig_950()
    fig_920b()
