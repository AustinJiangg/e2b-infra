"""进程级第 02 章配图：回滚范围的分界线（§2 两张表、§5 三个后果）。"""
from common import *  # noqa: F401,F403


def fig1():
    d = Diagram(width=1600)
    d.header("回滚范围的分界线：只有 uid 1000 的用户进程树和 overlay 根文件系统回到过去",
             "restore 之后，分界线左侧回到 checkpoint 那一刻，右侧一直在运行或保持 restore 那一刻的内容。"
             "两侧不再属于同一时刻，这是后面许多设计和限制的来源。")

    d.section(1, "沙箱里哪些回滚、哪些不回滚", highlight="本图重点", note="§2.1、§2.2 两张表")
    M, W = d.M, d.W
    from diagram_kit import RAMPS, INK, MUTED
    top = d.y + 6
    HEAD = 64
    LW = 470          # 左侧“回滚”面板宽
    LINE_GAP = 70     # 分界线两侧留白
    rx0 = M + LW + LINE_GAP
    cg = 24
    rcw = (W - M - rx0 - 20 - cg) / 2 - 10
    rcx = [rx0 + 20, rx0 + 20 + rcw + cg]
    left = [("用户进程树", "uid 1000 的进程连同全部子孙；子孙不看 uid，sudo 起的 root 子进程也算", "CRIU：杀掉后按镜像重建"),
            ("overlay 根文件系统", "/etc、/home、/opt、/usr、/var 的大部分，以及 /tmp", "overlayfs 热切层：换回层栈")]
    cols = [
        ("有意划出去的", "不属于“任务的状态”", [
            ("PID 1（systemd）", "所有进程的祖先，杀不得也重建不得"),
            ("envd", "SDK 的通道，以 root 运行"),
            ("gsd 自己", "执行回滚的一方；checkpoint 列表不随 restore 变化"),
            ("root 起的其他服务", "不是 uid 1000；仍在运行，但用的文件系统被换掉了"),
            ("内核状态", "页缓存、网络栈、时钟、内核线程不变，时钟不倒退"),
        ]),
        ("不在 overlay 上的目录", "根文件系统搭建方式的结果", [
            ("/run、/dev/shm", "独立挂载的内存文件系统，保持 restore 那一刻的内容"),
            ("/var/log、/var/lib/systemd、/var/lib/chrony", "开机时从 ext4 直接绑定进来，日志不会被回滚掉"),
            ("镜像目录 /var/lib/gsd/snapshots", "独立挂载的 2 GiB 内存文件系统，不变"),
            ("/overlay、/mnt/lower", "overlay 的层目录和基础镜像，从 ext4 绑定进来，由 gsd 管理"),
        ]),
    ]
    CG = 14
    hs = [[d._need(t, s, rcw, ts=15, ss=13, pad=20) for t, s in items] for _, _, items in cols]
    ch = max(hs[0] + hs[1])
    body = max(len(cols[0][2]), len(cols[1][2])) * (ch + CG) - CG
    panel_h = HEAD + 16 + body + 20
    # 左面板
    f, st, tc, sc = RAMPS["purple"]
    d.rect(M, top, LW, panel_h, "#FBFBF9", st, sw=1.2, rx=16)
    d.rect(M + 20, top + 16, LW - 40, 52, f, st, sw=1.6)
    d.text(M + LW / 2, top + 40, "回滚", 19, tc, 600, "middle")
    d.text(M + LW / 2, top + 60, "范围内：回到 checkpoint 那一刻", 13.5, sc, 400, "middle")
    lh = (body - CG) / 2
    for k, (t, s, a) in enumerate(left):
        d.block(M + 20, top + HEAD + 16 + k * (lh + CG), LW - 40, lh, t, s, "purple", "fill", actor=a)
    # 右面板
    f, st, tc, sc = RAMPS["gray"]
    d.rect(rx0, top, W - M - rx0, panel_h, "#FBFBF9", st, sw=1.2, rx=16)
    for i, (name, tag, items) in enumerate(cols):
        x = rcx[i]
        d.rect(x, top + 16, rcw, 52, f, st, sw=1.6)
        d.text(x + rcw / 2, top + 40, "不回滚 · " + name, 19, tc, 600, "middle")
        d.text(x + rcw / 2, top + 60, tag, 13.5, sc, 400, "middle")
        for k, (t, s) in enumerate(items):
            d.block(x, top + HEAD + 16 + k * (ch + CG), rcw, ch, t, s, "gray", "outline", ts=15, ss=13)
    # 分界线
    lx = M + LW + LINE_GAP / 2
    d.o.append(f'<line x1="{lx:.1f}" y1="{top - 4:.1f}" x2="{lx:.1f}" y2="{top + panel_h + 4:.1f}" '
               f'stroke="#D85A30" stroke-width="2.4" stroke-dasharray="10 6"/>')
    lab_y = top + panel_h / 2
    d.rect(lx - 30, lab_y - 14, 60, 28, "#FFFFFF", "#D85A30", sw=1.2, rx=14)
    d.text(lx, lab_y + 5, "分界线", 14, RAMPS["coral"][2], 600, "middle")
    y = top + panel_h + 16
    d.block(rx0, y, W - M - rx0, 66, "沙箱之外的一切", "已经发出去的请求、写进外部数据库的数据，任何快照都退不回来",
            "gray", "dashed", ts=15, ss=13)
    d.y = y + 66

    d.section(2, "分界线带来的三个后果", note="§5；都不是实现疏漏，而是只回滚一部分的必然结果")
    cards = [
        ("范围外的进程跨越切换继续运行", "换层那一刻 root 服务、envd 的文件接口可能正在读写；手里的目录和文件可能属于被丢弃的时间线",
         "08 · 09 · 26"),
        ("范围内外的状态可能对不上", "恢复的进程回到过去，连着的 envd、/run 里的文件、系统服务停在现在；最常见的是通向 envd 的标准输出管道",
         "10"),
        ("范围内部也不是一个整体", "多棵树分别保存、分别恢复，彼此有很短的先后差；树之间用管道或 socket 直接相连时 checkpoint 被拒绝",
         "10 · 11"),
    ]
    gap = 32
    cw = (d.W - 2 * d.M - 2 * gap) / 3
    h = max([130] + [d._need(t, s, cw, actor=True) for t, s, _ in cards])
    for i, (t, s, a) in enumerate(cards):
        d.block(d.M + i * (cw + gap), d.y, cw, h, t, s, "purple", "outline", actor=a)
    d.y += h
    d.footer("注：框左上角的数字是展开讨论的章号；三个后果作为取舍逐条列在第 04 章，由此而来的限制汇总在第 16 章。"
             "哪些目录在 overlay 上由开机脚本 overlay-init 决定（第 06 章）。")
    d.save(out("criu", "02-1-回滚范围的分界线"))


if __name__ == "__main__":
    fig1()
