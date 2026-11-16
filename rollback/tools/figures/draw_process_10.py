"""进程级 10：用户进程树——从 /proc 扫描结果里找根、划树（10 §1～§3，新增）。"""
from common import *  # noqa: F401,F403
from diagram_kit import RAMPS, INK, SUB, MUTED, text_w

PF, PS, PT, PSUB = RAMPS["purple"]
GF, GS, GT, GSUB = RAMPS["gray"]

d = Diagram()
d.header("谁是用户进程、谁是根：一次 /proc 扫描划出几棵树",
         "用户进程是 uid 1000、且不是 PID 1、gsd、envd 的进程；根是父进程不是用户进程、祖先里也没有别的根的用户进程；"
         "一棵树是根加上它的全部子孙，不看 uid。CRIU 的 --tree 按同样的范围取。",
         legend=[("flow", "判定顺序 / 父进程 → 子进程")])
M, W = d.M, d.W

# ---------------------------------------------------------------- 1. 判定规则
d.section(1, "判定顺序", note="一次操作只扫一次 /proc，checkpoint 和 restore 都用同一次扫描的结果")
d.flow([
    Step("扫描 /proc", "每个进程读 stat 和 status；kthreadd 的子进程（内核线程）整批跳过", actor="scanProcs"),
    Step("去掉排除集合", "PID 1、gsd 自己、envd；envd 取 envd.service 的主进程号，缓存并用启动时间校验", actor="systemPIDs"),
    Step("用户进程", "真实 uid 等于 1000；以 root 运行的命令和服务都不是", actor="userPIDsIn", ramp="purple"),
    Step("候选根 → 根", "候选根：父进程不是用户进程。根：祖先链上没有别的候选根", actor="userRootsIn", ramp="purple"),
    Step("划树、编号", "根加全部子孙，不看 uid；按根的进程号数值升序编号 t0、t1……", actor="subtree", ramp="purple"),
], min_h=150, gap=36)

# ---------------------------------------------------------------- 2. 例子
d.section(2, "一个沙箱里的例子：三棵树", highlight="本图重点", note="对应 §3.4 表里的几种形状")
T = d.y + 10
NW, NH = 214, 82
VG = 58                       # 层间距
slot = [M + 20 + i * ((W - 2 * M - 40 - NW) / 5) for i in range(6)]
lvl = [T + 16 + k * (NH + VG) for k in range(5)]

# 树的底色区域（先画）
def region(x0, y0, x1, y1, name):
    d.rect(x0, y0, x1 - x0, y1 - y0, "#F8F7FF", PS, sw=1, rx=14, dash="6 4")
    tw = text_w(name, 14) + 20
    d.rect(x1 - tw - 16, y1 - 12, tw, 24, PS, PS, sw=0, rx=12)
    d.text(x1 - tw / 2 - 16, y1 + 5, name, 14, "#FFFFFF", 600, "middle")

pad = 14
region(slot[0] - pad, lvl[2] - pad, slot[1] + NW + pad, lvl[4] + NH + pad, "树 t0")
region(slot[2] - pad, lvl[2] - pad, slot[2] + NW + pad, lvl[2] + NH + pad, "树 t1")
region(slot[4] - pad, lvl[1] - pad, slot[4] + NW + pad, lvl[1] + NH + pad, "树 t2")

STY = {
    "excl": ("gray", "dashed"),
    "sys": ("gray", "outline"),
    "kthr": ("gray", "dashed"),
    "root": ("purple", "fill"),
    "member": ("purple", "outline"),
}
TAG = {"excl": ("排除集合，永远不碰", MUTED), "sys": ("不是用户进程", MUTED), "kthr": ("整批跳过，不读 status", MUTED)}
nodes = {}


def node(key, s, l, title, sub, kind, tag=None):
    x, y = slot[s], lvl[l]
    ramp, style = STY[kind]
    f, st, tc, sc = RAMPS[ramp]
    if style == "fill":
        d.rect(x, y, NW, NH, f, st, sw=1.8)
    elif style == "outline":
        d.rect(x, y, NW, NH, "#FFFFFF", st, sw=1.2)
    else:
        d.rect(x, y, NW, NH, "#FFFFFF", st, sw=1.2, dash="6 4")
    d.text(x + NW / 2, y + 26, title, 15, tc if ramp != "gray" else INK, 600, "middle")
    d.text(x + NW / 2, y + 47, sub, 13, sc, 400, "middle")
    t, col = tag if tag else TAG.get(kind, ("", MUTED))
    if t:
        d.text(x + NW / 2, y + 68, t, 12.5, col, 600, "middle")
    nodes[key] = (x, y)


def edge(a, b):
    xa, ya = nodes[a]
    xb, yb = nodes[b]
    x1, y1 = xa + NW / 2, ya + NH
    x2, y2 = xb + NW / 2, yb
    my = y1 + (y2 - y1) * 0.42
    col = "#5F5E5A"
    d.o.append(f'<path d="M{x1:.1f} {y1 + 2:.1f} L{x1:.1f} {my:.1f} L{x2:.1f} {my:.1f} L{x2:.1f} {y2 - 6:.1f}" '
               f'fill="none" stroke="{col}" stroke-width="1.6" marker-end="url(#ar-flow)"/>')
    d.kinds.add("flow")


node("pid1", 2, 0, "PID 1（systemd）", "root", "excl")
node("kthreadd", 5, 0, "kthreadd（PID 2）", "内核线程的父进程", "sys", ("读一次它的 children 名单", MUTED))
node("gsd", 0, 1, "gsd", "root", "excl")
node("envd", 1, 1, "envd", "root；envd.service 主进程", "excl")
node("svc", 3, 1, "系统服务", "root", "sys")
node("nohup", 4, 1, "nohup … & 起的进程", "uid 1000；已被 PID 1 收养", "root", ("根：父进程 PID 1 不是用户进程", PT))
node("kthr", 5, 1, "内核线程", "约 60 个", "kthr")
node("A", 0, 2, "后台命令 A", "uid 1000；envd 的子进程", "root", ("根：父进程 envd 不是用户进程", PT))
node("B", 2, 2, "后台命令 B", "uid 1000；envd 的子进程", "root", ("根", PT))
node("rootcmd", 3, 2, "以 root 执行的命令", "uid 0；envd 的子进程", "sys", ("不是用户进程，不保存也不杀", MUTED))
node("Ac", 0, 3, "子进程", "uid 1000", "member", ("树成员", PSUB))
node("sudo", 1, 3, "sudo", "uid 0", "member", ("树成员：子孙不看 uid", PSUB))
node("inner", 1, 4, "切回用户身份的进程", "uid 1000；父进程是 sudo", "member", ("候选根，但祖先 A 是候选根", PT))

for a, b in [("pid1", "gsd"), ("pid1", "envd"), ("pid1", "svc"), ("pid1", "nohup"), ("kthreadd", "kthr"),
             ("envd", "A"), ("envd", "B"), ("envd", "rootcmd"), ("A", "Ac"), ("A", "sudo"), ("sudo", "inner")]:
    edge(a, b)

# 右下角说明
nx = slot[2]
ny = lvl[3]
d.rect(nx, ny, W - M - nx, NH * 2 + VG, "#FFFFFF", "#D3D1C7", sw=1, rx=10)
notes = [
    ("最里面那个进程为什么不算根", INK, 600),
    ("它满足候选根的条件（父进程 sudo 不是用户进程），但它已经在 t0 里。", SUB, 400),
    ("如果再当成一棵树单独 dump，restore 时同一个进程号要被创建两次，", SUB, 400),
    ("后一次必然失败，而那时进程已经被杀掉了。", SUB, 400),
    ("", SUB, 400),
    ("编号顺序就是 dump 的顺序、镜像子目录名，也是 restore 的顺序", INK, 600),
    ("按根的进程号数值升序：初始实现按目录名字符串排序，\"1203\" 排在 \"850\" 前面。", SUB, 400),
]
yy = ny + 30
for s, c, w in notes:
    if s:
        d.text(nx + 22, yy, s, 14 if w == 600 else 13.5, c, w)
    yy += 24
d.y = lvl[4] + NH + pad + 12

d.footer("注：图中省略进程号；t0、t1、t2 的编号假定三棵树的根进程号从左到右递增。"
         "§3.4 表里前四行（0、1、2、3 棵树）是 2026-10-08 确认验证里用探针脚本实际数过的形状，sudo 那一行是按规则推出的。")
d.save(out("process", "10-1-用户进程树"))
