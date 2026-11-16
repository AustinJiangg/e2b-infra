"""进程级 05：组件与分工（替换 05 §1 的 mermaid）。"""
from common import *  # noqa: F401,F403
from diagram_kit import RAMPS, ARROWS, INK, SUB, MUTED, text_w

ARROWS["build"] = (RAMPS["gray"][1], "6 4")
FLOW = ARROWS["flow"][0]


def poly(d, pts, kind="flow", label=None, lpos=None, anchor="start"):
    col, dash = ARROWS[kind]
    d.kinds.add(kind)
    p = "M" + " L".join(f"{x:.1f} {y:.1f}" for x, y in pts)
    da = f' stroke-dasharray="{dash}"' if dash else ""
    d.o.append(f'<path d="{p}" fill="none" stroke="{col}" stroke-width="1.8" stroke-linejoin="round"{da} '
               f'marker-end="url(#ar-{kind})"/>')
    if label:
        d.text(lpos[0], lpos[1], label, 13, col if kind != "build" else SUB, 600, anchor)


def panel(d, x, y, w, h, name, sub=None):
    d.rect(x, y, w, h, "#FAFAF7", "#E2E0D8", sw=1, rx=12)
    d.text(x + 18, y + 28, name, 16, INK, 600)
    if sub:
        d.text(x + 18 + text_w(name, 16) + 14, y + 28, sub, 13, MUTED)


d = Diagram()
d.header("进程级 checkpoint 的组件：编排只在 guest 里的 gsd 一处",
         "SDK 的请求经宿主代理原样转进 guest，由 gsd 鉴权并编排 CRIU 与 overlayfs 热切层。"
         "orchestrator 只在建模板时出现，沙箱运行期间的请求路径上没有它的专用代码。",
         legend=[("flow", "请求与调用"), ("build", "仅建模板时：注入并自检")])
d.section(1, "组件在哪、谁调用谁", highlight="本图重点")

M, W = d.M, d.W
bw, gap = 280, 104
cx0 = 88
col = [cx0 + i * (bw + gap) for i in range(4)]
mid = lambda i: col[i] + bw / 2
lane = col[0] + bw + gap / 2

# 调用方
cp = d.y + 8
y0 = cp + 22
sh = 92
panel(d, M, cp, W - 2 * M, sh + 44, "调用方")
d.block(col[2] - 40, y0, bw + 80, sh, "Python SDK", "四个 RPC 与 /health，带沙箱的 access token")

# 宿主
hp = cp + sh + 44 + 46
hb = hp + 46
hh = 104
panel(d, M, hp, W - 2 * M, hb + hh + 22 - hp, "宿主")
d.block(col[0], hb, bw + 100, hh, "orchestrator 模板构建", "放入 gsd、gsd.service、overlay-init，内核参数 init= 指向 overlay-init",
        style="outline")
d.block(col[2] - 40, hb, bw + 80, hh, "client-proxy 与 orchestrator 沙箱代理",
        "按“端口 + 沙箱 id”把请求转进 guest，不理解 checkpoint 语义")
d.arrow(mid(2), y0 + sh + 4, mid(2), hb - 6, "flow", "HTTP 49984")
host_bottom = hb + hh + 22

# guest
gp = host_bottom + 52
r1 = gp + 50
rh = 116
r2 = r1 + rh + 70
kp = r2 + rh + 50
kb = kp + 46
kh = 100
g_bottom = kb + kh + 22 + 22
panel(d, M, gp, W - 2 * M, g_bottom - gp, "guest 沙箱")

d.block(col[0], r1, bw, rh, "overlay-init", "开机时挂好 overlay 根，chroot 后 exec systemd",
        actor="PID 1 的前身，只在开机时运行")
gx0, gx1 = col[1], col[3] + bw
d.block(gx0, r1, gx1 - gx0, rh, "gsd 守护进程",
        "鉴权、串行化、找用户进程树、编排 CRIU 与热切层的先后、失败分级、状态持久化、回收",
        ramp="purple", actor="root，systemd 服务")

d.block(col[1], r2, bw, rh, "snapshot store 与层目录", "/var/lib/gsd/snapshots（2 GiB tmpfs）与 /overlay/layers",
        style="outline")
d.block(col[2], r2, bw, rh, "CRIU 4.x", "一次 dump 或 restore 一棵进程树", ramp="purple", actor="gsd 的子进程")
d.block(col[3], r2, bw, rh, "用户进程树", "被 dump、被杀掉、被 CRIU 重建的对象", style="outline")

# 补丁内核
kx0, kx1 = col[0] - 16, col[2] + bw + 16
d.rect(kx0, kp, kx1 - kx0, kb + kh + 22 - kp, "#FFFFFF", RAMPS["purple"][1], sw=1.2, rx=12)
d.text(kx0 + 18, kp + 28, "补丁内核", 16, RAMPS["purple"][2], 600)
d.block(col[0], kb, bw, kh, "overlayfs 热切层 ioctl", "把层栈换成 gsd 给的一串目录", ramp="purple")
d.block(col[2], kb, bw, kh, "soft-dirty", "arm64 上靠补丁加；记录哪些页被写过", ramp="purple")

# 连线
d.arrow(mid(2), hb + hh + 4, mid(2), r1 - 6, "flow", "按端口原样转发")
d.arrow(mid(0), hb + hh + 4, mid(0), r1 - 6, "build")
poly(d, [(col[0] + bw + 104, hb + hh / 2), (mid(1), hb + hh / 2), (mid(1), r1 - 6)], "build")
d.text(mid(0) + 14, hb + hh + 36, "仅建模板时注入并自检", 13, SUB, 600)

d.arrow(mid(0), r1 + rh + 4, mid(0), kb - 6, "flow", "开机时挂好 overlay 根")
gy = r1 + rh - 26
poly(d, [(gx0 - 4, gy), (lane, gy), (lane, kb + kh / 2), (col[0] + bw + 6, kb + kh / 2)], "flow",
     "ioctl", (lane + 10, kb + kh / 2 - 10))
d.arrow(mid(1), r1 + rh + 4, mid(1), r2 - 6, "flow", "读写状态与镜像")
d.arrow(mid(2), r1 + rh + 4, mid(2), r2 - 6, "flow", "fork 并等待")
d.arrow(mid(3), r1 + rh + 4, mid(3), r2 - 6, "flow", "信号与 proc 扫描")
d.arrow(col[2] - 4, r2 + rh / 2, col[1] + bw + 6, r2 + rh / 2, "flow", "读写镜像")
d.arrow(col[2] + bw + 4, r2 + rh / 2, col[3] - 6, r2 + rh / 2, "flow", "dump、restore")
d.arrow(mid(2), r2 + rh + 4, mid(2), kb - 6, "flow")
d.text(mid(2) + 12, r2 + rh + 32, "读 pagemap", 13, FLOW, 600)
d.rect(col[1], kb, bw, kh, "#FFFFFF", RAMPS["gray"][1], dash="6 4")
d.text(mid(1), kb + 42, "不记录有哪些 checkpoint", 13.5, SUB, 400, "middle")
d.text(mid(1), kb + 66, "不决定切到哪个层栈", 13.5, SUB, 400, "middle")
d.y = g_bottom

d.footer("注：CRIU 每次只收到“dump 这棵树到这个目录”或“从这个目录 restore”，内核每次只收到“把层栈换成这一串目录”；"
         "哪几棵树算用户进程、增量的父镜像是谁、checkpoint 对应哪个层栈，只有 gsd 知道。")
d.save(out("process", "05-1-组件与分工"))
