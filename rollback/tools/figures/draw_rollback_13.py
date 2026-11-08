"""第 13 章配图：13-1 原生 Sandbox.Pause 的步骤，以及内存增量、磁盘增量各存什么。
ResumeSandbox 的逐步重建不再画，正文引用 09-1。"""
from common import *  # noqa: F401,F403
from diagram_kit import RAMPS, INK, SUB, MUTED, wrap, text_w


def fig1():
    d = Diagram(width=1600)
    d.header("原生 Sandbox.Pause：暂停之后不再恢复，内存增量从进程里拷出，磁盘增量要先关掉沙箱",
             "六步按执行顺序；产物是一份 snapfile 加两份紧凑增量，靠 header 逐块寻址。"
             "Pause 要等上传完成才返回，所以客户端看到的耗时包含停沙箱、导出和上传。",
             legend=[("flow", "执行顺序"), ("warn", "关掉沙箱")])

    # ------------------------------------------------------------ 第一段
    d.section(1, "Sandbox.Pause 的六步", note="internal/sandbox/sandbox.go")
    d.flow([
        Step("① 停健康检查", "快照期间健康检查会失败", "orchestrator", ramp="coral"),
        Step("② 暂停虚机", "此后 guest 不再产生新状态；之后一直没有 resume", "Firecracker", ramp="coral"),
        Step("③ 写出 snapfile", "vCPU、GIC、设备状态，不含内存", "Firecracker", ramp="coral"),
    ], min_h=96)
    d.y += 26
    d.flow([
        Step("④ 取内存脏页集合", "问内存后端（UFFD）哪些页算脏", "orchestrator", ramp="coral"),
        Step("⑤ 导出内存增量", "按脏页集合从 Firecracker 进程地址空间拷出", "orchestrator", ramp="coral"),
        Step("⑥ 导出磁盘增量", "把写层导出成紧凑 diff；参数之一就是关掉沙箱", "orchestrator", ramp="coral",
             tag=("这一步关掉沙箱", "coral")),
    ], min_h=96)
    d.callout("Pause 的语义是「沙箱到此为止」：两个 RPC 都 defer stopSandboxAsync；"
              "Checkpoint RPC 里的「继续跑」是从新快照另起一个新进程，LifecycleID 换新", "coral")

    # ------------------------------------------------------------ 第二段
    d.section(2, "两份增量各存什么", highlight="本图重点", note="§3、§4")
    c = d.compare([("内存增量", "pauseProcessMemory", "coral"),
                   ("磁盘增量", "pauseProcessRootfs → NBDProvider.ExportDiff", "coral")])
    c.row("哪些要存", [
        ("脏页", "present（bit 63）= 1 且 uffd-wp（bit 57）= 0；GET /memory/dirty 先用 mincore 筛常驻页再读 pagemap"),
        ("写层里写过的块", "Overlay 的写层；打过本方案 checkpoint 的沙箱先把全部层压平")])
    c.stack("怎么搬", [
        [("process_vm_readv 读 Firecracker 进程内存", "脏区间组织成 RemoteIovec，按 IOV_MAX 和单次上限分批"),
         ("写进本地 cache 文件", "脏了多少就拷多少"),
         Note("沙箱此时只是暂停", "导出完就是产物", span=2)],
        [("EjectLayers 摘出写层", "Overlay 就此作废，之后任何写都没有去处"),
         ("停沙箱，等 NBD 设备释放", "closeSandbox 另起协程调 s.Close"),
         ("逐块 DiffMetadataBuilder.Process", "全零块只在 empty 位图记一位；非零块紧凑追加"),
         ("关闭写层", None)],
    ], inner=[["flow", None], ["warn", "flow", "flow"]])
    c.row("产物", [
        Box("紧凑 diff + 链式 header", "只含脏块，按顺序排列；ToDiffHeader 把原 header 与本次脏块合成新 header", "outline"),
        Box("紧凑 diff + 映射表", "存储偏移 ≠ 设备偏移，必须靠映射表换算", "outline")],
        links=[None, "flow"])
    c.end()
    d.callout("ARM 适配基线注释掉了填页时带写保护位的几行，bit 57 恒为 0，判据塌缩成「常驻即脏」；"
              "本书交付的代码在跟踪开着时改用写跟踪位图按 4 KiB 页判脏（第 14 章）", "gray")

    # ------------------------------------------------------------ 第三段：链式 header
    d.section(3, "链式 header：每一块指向写它的那一代", note="恢复时按 header 逐块寻址")
    M = d.M
    y0 = d.y
    bh, gapv = 74, 18
    lw = 420
    rx = M + lw + 140
    rw = d.W - M - rx
    d.block(M, y0 + (2 * bh + gapv - 96) / 2, lw, 96, "新 build 的 header", "本次 Pause 产出；按区间记录每一块在哪", "gray", "outline",
            actor="orchestrator")
    targets = [("本次写过的块 → 本次的 diff 文件", "这一代 Pause 导出的紧凑增量", "coral"),
               ("没写过的块 → 上一代 build", "上一代的 header 里，这一块可能再往上指", "gray")]
    for k, (t, s_, r) in enumerate(targets):
        yy = y0 + k * (bh + gapv)
        d.block(rx, yy, rw, bh, t, s_, r, "fill")
        d.arrow(M + lw + 4, y0 + bh + gapv / 2, rx - 6, yy + bh / 2, "flow")
    d.y = y0 + 2 * bh + gapv
    d.footer("注：ResumeSandbox 新建 Firecracker 进程、按 header 取块、工作集靠缺页换回的逐步重建见图 09-1；"
             "本方案的 checkpoint 不停虚机，写层原地封存，不搬数据（第 7 章）。")
    d.save(out("rollback", "13-1-原生Pause与两份增量"))


if __name__ == "__main__":
    fig1()
