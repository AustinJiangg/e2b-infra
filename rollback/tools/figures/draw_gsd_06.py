"""进程级 06：overlay-init 把 ext4 根盘装配成 overlay 根（替换 06 §2 的 mermaid）。"""
from common import *  # noqa: F401,F403
from diagram_kit import RAMPS, ARROWS, INK, SUB, MUTED, text_w, wrap

FLOW = ARROWS["flow"][0]


def poly(d, pts, kind="flow"):
    col, dash = ARROWS[kind]
    d.kinds.add(kind)
    p = "M" + " L".join(f"{x:.1f} {y:.1f}" for x, y in pts)
    d.o.append(f'<path d="{p}" fill="none" stroke="{col}" stroke-width="1.8" stroke-linejoin="round" '
               f'marker-end="url(#ar-{kind})"/>')


def lbl(d, x, y, s, anchor="middle"):
    d.text(x, y, s, 13, FLOW, 600, anchor)


d = Diagram()
d.header("overlay-init：先把 ext4 根盘搭成 overlay 根，再 chroot 进去启动 systemd",
         "内核以 init=/usr/bin/overlay-init 启动它，此时 / 还是 ext4 根盘。紫色是组成 overlay 根的部分，checkpoint 和 restore 换的就是它的层；"
         "灰色是 bind 或 tmpfs 挂进新根、不经过 overlay 的东西。图中步骤号与下表一致。",
         legend=[("flow", "建目录、挂载、bind")])

M, W = d.M, d.W
LX, LW = M, 330           # 来源列
MX, MW = M + 330 + 250, 380   # 中间列
RX = MX + MW + 60         # 右列
RW = W - M - RX

# ---------------------------------------------------------------- 1. 旧根上搭 overlay
d.section(1, "在旧根上搭出 overlay 根", note="步骤 1–3", highlight="本图重点")
y = d.y + 8
bh, rg = 96, 40
MX, MW = M + 330 + 220, 380
RX = MX + MW + 150
RW = W - M - RX
d.block(LX, y, LW, 2 * bh + rg, "ext4 根盘", "内核启动时的 /；之后降为只读的最底层")
ya, yb = y, y + bh + rg
d.block(MX, ya, MW, bh, "/mnt/lower", "mount --bind / /mnt/lower：给 ext4 根一个可以当 lowerdir 的路径", ramp="purple", style="outline")
d.block(MX, yb, MW, bh, "/overlay/workspace/upper 与 work", "初始 upper：根目录属主、权限与 /mnt/lower 相同",
        ramp="purple", style="outline")
d.block(RX, y, RW, 2 * bh + rg, "/mnt/newroot：overlay 根", "唯一 lower 是 /mnt/lower；chroot 之后它就是 /",
        ramp="purple")
d.arrow(LX + LW + 4, ya + bh / 2, MX - 6, ya + bh / 2)
lbl(d, (LX + LW + MX) / 2, ya + bh / 2 - 10, "步骤 2：bind")
d.arrow(LX + LW + 4, yb + bh / 2, MX - 6, yb + bh / 2)
lbl(d, (LX + LW + MX) / 2, yb + bh / 2 - 10, "步骤 1：建目录")
d.arrow(MX + MW + 4, ya + bh / 2, RX - 6, ya + bh / 2)
lbl(d, (MX + MW + RX) / 2, ya + bh / 2 - 10, "lowerdir")
d.arrow(MX + MW + 4, yb + bh / 2, RX - 6, yb + bh / 2)
lbl(d, (MX + MW + RX) / 2, yb + bh / 2 - 10, "upperdir")
d.text((MX + MW + RX) / 2, y + bh + rg / 2 + 5, "步骤 3", 13, FLOW, 600, "middle")
d.y = y + 2 * bh + rg

# ---------------------------------------------------------------- 2. 往新根里挂不经过 overlay 的东西
MX, MW = M + 330 + 250, 380
RX = MX + MW + 60
RW = W - M - RX
d.section(2, "在新根里补齐 overlay 之外的东西", note="步骤 4–9，都挂在 /mnt/newroot 下面")
y = d.y + 6
d.text(LX, y + 14, "来源", 14, MUTED, 600)
d.text(MX, y + 14, "新根里的位置", 14, MUTED, 600)
d.text(RX, y + 14, "为什么", 14, MUTED, 600)
y += 30
rows = [
    ("步骤 4：bind", "ext4 上的三个系统目录", "/var/log、/var/lib/systemd、/var/lib/chrony",
     "日志、systemd 状态、时钟漂移数据不随 restore 回滚（§2.2）"),
    ("步骤 5：挂载", "伪文件系统与 tmpfs", "/dev、/proc、/sys，/run 上的 tmpfs",
     "内核不会替 init= 指定的程序挂这些"),
    ("步骤 6：写 sysfs", "新根里刚挂好的 sysfs", "透明大页设为 never，回读确认",
     "必须在任何用户进程出现之前关掉（§2.3）"),
    ("步骤 7：挂 tmpfs", "tmpfs，size=2G", "snapshot store：/var/lib/gsd/snapshots",
     "CRIU 镜像绝不能被回滚，restore 要读的正是它们（§2.4）"),
    ("步骤 8：bind", "ext4 上的 /overlay", "/overlay：层目录与 upper 所在",
     "gsd 在 chroot 之后要看见并管理层目录"),
    ("步骤 9：bind", "旧根里的 /mnt/lower", "/mnt/lower",
     "chroot 后 gsd 传的层路径从新根解析（§2.5）"),
]
rh, gp = 66, 14
for step, src, tgt, why in rows:
    d.block(LX, y, LW, rh, src)
    d.block(MX, y, MW, rh, tgt, style="outline")
    d.arrow(LX + LW + 4, y + rh / 2, MX - 6, y + rh / 2)
    lbl(d, (LX + LW + MX) / 2, y + rh / 2 - 10, step)
    wl = wrap(why, RW, 14)
    for k, s in enumerate(wl):
        d.text(RX, y + rh / 2 + 5 - 10 * (len(wl) - 1) + 20 * k, s, 14, SUB)
    y += rh + gp
d.y = y - gp
d.callout("步骤 7 同时整理 /tmp：它留在 overlay 上随 restore 回滚；每次开机清空、权限 1777、屏蔽 tmp.mount，"
          "这是 tmpfs 原来顺带提供的三样东西", "purple")

# ---------------------------------------------------------------- 3. chroot
d.section(3, "切进新根", note="步骤 10")
d.flow([
    Step("cd /mnt/newroot", "进入装配好的新根", actor="overlay-init"),
    Step("exec chroot . /sbin/init", "overlay-init 被 systemd 取代", actor="overlay-init"),
    Step("systemd 成为 PID 1", "从此 / 是 overlay，gsd.service 随后启动", actor="systemd", ramp="purple"),
], min_h=110, gap=56)

d.footer("注：脚本对大多数挂载失败是“尽力而为”的（|| true），不中途退出；全部输出写到 /dev/kmsg，前缀 [overlay-init]。"
         "沙箱从快照恢复时不会再运行这个脚本。")
d.save(out("gsd", "06-1-overlay-init装配根文件系统"))
