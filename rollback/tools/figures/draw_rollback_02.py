"""第 02 章配图：一个沙箱在宿主上的样子与 UFFD 填页、请求怎么到达沙箱、快照的保存与冷恢复。

运行：python3 draw_rollback_02.py
"""
from common import *  # noqa: F401,F403
from diagram_kit import RAMPS, INK, SUB, MUTED, text_w, wrap
from draw_rollback_04 import panel, need, elbow, swatches


# ------------------------------------------------------------------ 02-1
def fig_host():
    d = Diagram(width=1600)
    d.header("一个沙箱在宿主上的样子：部件都挂在 orchestrator 里，内存按需经 UFFD 填入",
             "每个沙箱在 orchestrator 里有一个 Sandbox 对象和一个 UFFD handler；Firecracker 进程、NBD 设备、tap 与网络命名空间是它们在宿主上对应的资源。")
    swatches(d, d.y + 34, [("gray", "e2b 原有的部件"), ("teal", "本方案加在 orchestrator 里的部件")])
    d.y += 40
    d.section(1, "orchestrator 进程里有什么", note="下面一排是沙箱在宿主上占用的资源")
    M, W = d.M, d.W
    TB = 44
    top = d.y + 6
    x0, x1 = M + 20, W - M - 20
    n, g = 5, 24
    cw = (x1 - x0 - (n - 1) * g) / n
    cx = [x0 + i * (cw + g) for i in range(n)]
    cc = [x + cw / 2 for x in cx]

    # 第一排：沙箱表、代理、Checkpoint Service
    r1 = [("沙箱表", None, cx[0], cx[1] + cw, "gray"),
          ("代理（端口路由）", "按 (sandboxID, port) 转发进沙箱", cx[2], cx[3] + cw, "gray"),
          ("Checkpoint Service", "本方案的控制面", cx[4], cx[4] + cw, "teal")]
    h1 = max(need(d, t, s, b - a) for t, s, a, b, _ in r1)
    # 第二排：UFFD handler + Sandbox 对象的四个字段
    r2 = [("UFFD handler", "每沙箱一个，按需填充 guest 内存页"),
          ("process", "→ Firecracker 的 API socket"),
          ("Rootfs Provider", "→ Overlay + NBD"),
          ("Slot", "→ 网络命名空间、tap、IP"),
          ("Template", "→ memfile / rootfs / snapfile")]
    h2 = max(need(d, t, s, cw) for t, s in r2)
    y1 = top + TB
    y2 = y1 + h1 + 72
    ph = TB + h1 + 72 + h2 + 26
    panel(d, M, top, W - 2 * M, ph, "orchestrator 进程", "宿主上的管理进程：管沙箱生命周期、网络、存储、代理")
    for t, s, a, b, ramp in r1:
        d.block(a, y1, b - a, h1, t, s, ramp)
    d.rect(cx[1] - 10, y2 - 34, cx[4] + cw - cx[1] + 20, h2 + 44, "none", "#B4B2A9", sw=1, rx=10, dash="5 4")
    d.text(cx[1] + 2, y2 - 14, "Sandbox 对象", 13.5, SUB, 600)
    for i, (t, s) in enumerate(r2):
        d.block(cx[i], y2, cw, h2, t, s, "gray")
    d.arrow(cc[0], y1 + h1 + 2, cc[0], y2 - 5)
    d.arrow(cc[1] + 40, y1 + h1 + 2, cc[1] + 40, y2 - 39)
    bottom = top + ph

    # 第三排：宿主资源
    y3 = bottom + 64
    r3 = [(1, "Firecracker 进程", "每个沙箱一个，真正跑 guest 的 VMM"),
          (2, "/dev/nbdX", "把 Overlay 暴露成块设备给 guest"),
          (3, "tap 设备 + netns", "通过 NAT 出网")]
    h3 = max(need(d, t, s, cw) for _, t, s in r3)
    for i, t, s in r3:
        d.block(cx[i], y3, cw, h3, t, s, "gray")
        d.arrow(cc[i], y2 + h2 + 2, cc[i], y3 - 5)
    y4 = y3 + h3 + 40
    h4 = need(d, "KVM", "/dev/kvm", cw)
    d.block(cx[1], y4, cw, h4, "KVM", "/dev/kvm", "gray")
    d.arrow(cc[1], y3 + h3 + 2, cc[1], y4 - 5)
    # UFFD handler → Firecracker：按需填页
    elbow(d, [(cc[0], y2 + h2 + 2), (cc[0], y3 + h3 / 2), (cx[1] - 6, y3 + h3 / 2)])
    d.text(cc[0] + 12, y3 + h3 / 2 - 12, "填 guest 内存页", 13, SUB, 600)
    d.y = y4 + h4

    # 第二段：缺页时
    d.section(2, "guest 访问一个还没有内容的内存页时", note="UFFD：Linux 让一段内存缺页时由用户态程序来填")
    d.flow([Step("guest 访问一页", "这一页还没有内容", "guest"),
            Step("缺页", "内核不自己处理，转交给用户态", "宿主内核"),
            Step("取这一页的内容", "从本地缓存取；没有就经 chunker 从对象存储按块拉", "UFFD handler（orchestrator 里）"),
            Step("把这一页填进去", "guest 继续跑", "UFFD handler")], min_h=110)
    d.callout("memfile 可能有几个 GB、在对象存储里：沙箱在只拉了很小一部分时就能跑起来，用到哪拉到哪", "gray")
    d.footer("注：只有 template 构建流程用 CreateSandbox（普通内存，冷启动）；所有用户沙箱走 ResumeSandbox，内存后端是真正的 UFFD。")
    d.save(out("rollback", "02-1-一个沙箱在宿主上的样子"))


# ------------------------------------------------------------------ 02-2
def fig_route():
    d = Diagram(width=1600)
    d.header("请求怎么到达沙箱：代理按 Host 头路由，checkpoint 请求在最后一跳之前被截下",
             "以 sandbox.commands.run(\"ls\") 为例走一遍原有路径；本方案的 API 复用这条路径，只在代理处改道。")
    d.section(1, "原有路径：一直转发到 guest 里的 envd")
    d.flow([Step("SDK 发请求", "sandbox.commands.run(\"ls\")", "SDK"),
            Step("沙箱的公开地址", "Host 头里带着端口和沙箱 id", "统一入口"),
            Step("orchestrator 的代理", "解出 (sandboxID, port)", "orchestrator"),
            Step("转发进沙箱", "对应沙箱的对应端口", "orchestrator"),
            Step("envd 应答", "guest 里的守护进程执行命令", "guest")], min_h=110)
    xs, bw = d._flow
    top = d.y
    y = top + 84
    t, s = "宿主直接应答，不进沙箱", "checkpoint 要暂停虚机，沙箱内的程序没法暂停自己"
    w = xs[4] + bw - xs[2]
    h = need(d, t, s, w, actor=True)
    d.block(xs[2], y, w, h, t, s, "teal", actor="orchestrator · Checkpoint Service")
    d.arrow(xs[2] + bw / 2, top + 2, xs[2] + bw / 2, y - 5, "flow", "端口 49984 + checkpoint 路径")
    d.y = y + h
    d.footer("注：被截走的请求绕过了代理自己的 e2b-traffic-access-token 校验，所以 Checkpoint Service 自己再校验一次（第 4 章）。")
    d.save(out("rollback", "02-2-请求怎么到达沙箱"))


# ------------------------------------------------------------------ 02-3
def fig_snapshot():
    d = Diagram(width=1600)
    d.header("快照的两个基本动作：一次暂停内保存，冷恢复要重建整台虚机",
             "保存时内存、磁盘、vCPU / 设备状态必须是同一瞬间的镜像；从快照冷恢复则要从新建进程开始，一步步把整台虚机建回来。")
    M, W = d.M, d.W

    # 第一段：一次暂停内完成
    d.section(1, "保存：三样东西在一次暂停内完成", note="暂停的这段时间叫冻结窗口")
    top = d.y + 6
    bw = 220
    inner = ["保存内存", "保存磁盘", "保存 vCPU / 设备状态"]
    fx0, fx1 = M + bw + 60, W - M - bw - 60
    iw = (fx1 - fx0 - 40 - 2 * 24) / 3
    ih = 64
    fh = 44 + ih + 22
    d.rect(fx0, top, fx1 - fx0, fh, RAMPS["coral"][0], RAMPS["coral"][1], sw=1, rx=12, dash="6 4")
    d.text(fx0 + 18, top + 27, "冻结窗口（freeze window）", 15, RAMPS["coral"][2], 600)
    d.text(fx0 + 18 + text_w("冻结窗口（freeze window）", 15) + 14, top + 27,
           "guest 内业务直接感受到的停顿", 13, RAMPS["coral"][3])
    for k, s in enumerate(inner):
        d.block(fx0 + 20 + k * (iw + 24), top + 44, iw, ih, s, None, "gray", "outline")
    d.block(M, top + 44, bw, ih, "暂停虚机", None, "gray")
    d.block(W - M - bw, top + 44, bw, ih, "恢复虚机", None, "gray")
    d.arrow(M + bw + 4, top + 44 + ih / 2, fx0 + 14, top + 44 + ih / 2)
    d.arrow(fx1 - 14, top + 44 + ih / 2, W - M - bw - 6, top + 44 + ih / 2)
    d.y = top + fh
    d.callout("三者是同一瞬间的镜像：guest 尚未刷盘的数据留在内存镜像里，位置完全正确", "gray")
    d.callout("反例：内存在 T1、磁盘在 T2 保存，中间 guest 继续跑 → 恢复后文件系统日志、页缓存、inode 与磁盘对不上，"
              "可能报文件系统损坏，也可能静默读到错的数据", "coral")

    # 第二段：重建链
    d.section(2, "冷恢复：新建一个进程去加载", note="上游 Firecracker 的 PUT /snapshot/load 和 e2b 原生的 resume 都是这种做法",
              highlight="本图重点")
    rows = [("建虚机", ["新建进程", "建 KVM VM", "建 vCPU", "建中断控制器", "建设备"]),
            ("接上宿主资源", ["注册各种 fd", "建网卡", "挂内存后端"]),
            ("装入快照状态", ["加载 vmstate", "恢复寄存器与设备状态", "跑起来"]),
            ("运行之后", [("guest 开始运行", None), ("缺页", "UFFD handler 把这一页填进来"),
                          ("逐页把工作集换回内存", "一段时间内持续缺页")])]
    LW, gap = 150, 40
    bx0 = M + LW
    n = 5
    w = (W - M - bx0 - (n - 1) * gap) / n
    h = 60
    vg = 46
    y = d.y + 10
    prev = None
    for ri, (lab, steps) in enumerate(rows):
        last = ri == len(rows) - 1
        h = 84 if last else 60
        d.text(M, y + h / 2 + 5, lab, 14.5, INK, 600)
        for k, s in enumerate(steps):
            x = bx0 + k * (w + gap)
            t, sub = s if isinstance(s, tuple) else (s, None)
            d.block(x, y, w, h, t, sub, "coral", "outline" if last else "fill")
            if k:
                d.arrow(x - gap + 4, y + h / 2, x - 6, y + h / 2)
        if prev is not None:
            px, py = prev
            mid = y - vg / 2
            elbow(d, [(px, py + 2), (px, mid), (bx0 + w / 2, mid), (bx0 + w / 2, y - 5)])
        prev = (bx0 + (len(steps) - 1) * (w + gap) + w / 2, y + h)
        y += h + vg
    d.y = y - vg
    d.callout("成本 = 建一台虚机的固定开销 + 把工作集换回内存；后者与虚机规格成正比，与“回退了多少”完全无关", "coral")
    d.footer("注：本方案的 restore 是另一种形态 —— 原地回写：虚机暂停 → 把变了的页写回活着的内存映射 → 把 vCPU / 设备状态写回既有对象 → 恢复运行，"
             "一个宿主资源都不重建（§6.4）。")
    d.save(out("rollback", "02-3-快照的保存与冷恢复"))


if __name__ == "__main__":
    fig_host()
    fig_route()
    fig_snapshot()
