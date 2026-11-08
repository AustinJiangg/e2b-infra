"""第 08 章 Firecracker 接口契约的配图（新增）。

08-1 分叉 Firecracker 的三个扩展：各在 checkpoint / restore 的哪一步被调用，读写什么文件，位图怎么处理（§1.3、§2～§4）。
08-2 FCDB 位图格式：逐字节布局、位与页的对应、尾字掩码、页索引所在的空间（§5）。格子画法参考 HDBSS 仓库的 23-2。
内容只取本章正文。三个扩展都是本方案的一部分，用 teal；共同部分（暂停、恢复运行、orchestrator 侧动作）用灰色。
"""
from common import *  # noqa: F401,F403
from diagram_kit import RAMPS, INK, SUB, MUTED, text_w  # noqa: E402

TF, TS, TT, TSUB = RAMPS["teal"]
GF, GS, GT, GSUB = RAMPS["gray"]
MONO = "'DejaVu Sans Mono','Consolas',monospace"


def mono(d, x, y, s, size=13, color=INK, anchor="middle", weight=400):
    from xml.sax.saxutils import escape
    fw = f' font-weight="{weight}"' if weight != 400 else ""
    d.o.append(f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{color}" font-family="{MONO}"{fw} '
               f'text-anchor="{anchor}">{escape(s)}</text>')


# ------------------------------------------------------------------ 08-1
def f08_1():
    d = Diagram()
    d.header("分叉 Firecracker 的三个扩展：orchestrator 在暂停期间调用，Firecracker 只做无状态的读写",
             "一个可选字段加两个新端点，既有行为只改了 fsync 与暂停两处（见 §1.3）。哪些页该回滚、内容从哪一代取，由 orchestrator 算好再递过去；"
             "Firecracker 不知道 checkpoint 树是什么。",
             legend=[("flow", "调用顺序")])

    d.section(1, "checkpoint：快照与它写了哪些页，在同一次调用里交出", note="扩展一；第一个 checkpoint 另外先调扩展三")
    d.flow([
        Step("暂停虚机", "PATCH /vm", "orchestrator"),
        Step("PUT /snapshot/save-dirty-bitmap", "只在全量 checkpoint 之前：导出自启动以来的活跃脏图", "orchestrator → FC",
             tag=("扩展三", "teal")),
        Step("PUT /snapshot/create", "带 mem_file_path 与 dirty_bitmap_path", "orchestrator → FC",
             tag=("扩展一", "teal")),
        Step("恢复运行", "封存文件移进 layers/ 等在这之后", "orchestrator"),
    ], min_h=150, gap=40)
    d.section(2, "restore：先导出活跃脏图，物化之后再回滚", note="顺序约束：先 Pause，再 SaveDirtyBitmap，再物化，再 RollbackSnapshot")
    d.flow([
        Step("暂停虚机", "PATCH /vm", "orchestrator"),
        Step("导出活跃脏图", "PUT /snapshot/save-dirty-bitmap；FC 回滚时要并入这些页", "orchestrator → FC",
             tag=("扩展三", "teal")),
        Step("物化回滚集", "回滚集 = 累积集合 ∪ 活跃脏页，写成只含这些页的稀疏文件", "orchestrator"),
        Step("原地回滚内存与状态", "PUT /snapshot/rollback，resume_vm: false：磁盘视图还没换", "orchestrator → FC",
             tag=("扩展二", "teal")),
        Step("换磁盘视图、恢复运行", "清完 conntrack 之后由 orchestrator 恢复", "orchestrator"),
    ], min_h=150, gap=36)

    d.section(3, "三个扩展各读写什么", highlight="本图重点")
    c = d.compare([("dirty_bitmap_path", "CreateSnapshotParams 的可选字段", "teal"),
                   ("PUT /snapshot/rollback", "往活着的虚机写回状态", "teal"),
                   ("PUT /snapshot/save-dirty-bitmap", "导出还没进快照的脏页", "teal")])
    c.row("前提", [("必须同时给 mem_file_path", "否则拒绝：DirtyBitmapWithoutMemFile"),
                  ("虚机 Paused", "拓扑一致、内存文件长度与位图几何对上、回滚集每页都有数据"),
                  ("虚机 Paused", "否则活跃脏页集在导出之后还会增长")], links=[None, None, None])
    c.row("读", [("KVM 位图 ∪ 用户态位图", "dump_dirty 遍历两者的并集"),
                ("snapshot_path、mem_file_path、revert_bitmap_path", "目标时刻的 vmstate、稀疏物化文件、累积集合（FCDB）"),
                ("KVM 位图", "破坏性读，取走后立刻折回用户态位图")], links=[None, None, None])
    c.row("写", [("snapfile、内存文件、侧车", "侧车 = merged 位图 E_x（FCDB）；全量快照写全 1"),
                ("guest 内存、vCPU、GIC、设备状态", "回滚集 = 传入位图 ∪ 活跃脏页；返回 restored_pages、timings_us"),
                ("path 指定的 FCDB 文件", "当前的活跃脏页位图")], links=[None, None, None])
    c.row("之后的位图", [("写成功才 reset_dirty", "写文件失败时折回用户态位图"),
                       ("阶段 9 reset_dirty", "队列页重新标脏"),
                       ("不清位图", "之后的 Diff 快照或回滚仍看到完整集合")], links=[None, None, None])
    c.row("失败时", [("快照失败", "取出来的脏页信息不丢"),
                    ("提交点前 400 / 提交点后 500 + fault: true", "可恢复，或撕裂只能替换进程"),
                    ("restore 失败", "虚机 resume，沙箱原样继续")], links=[None, None, None])
    c.end()
    d.footer("注：不使用这三处的调用方，看到的行为与上游基本一致，只有快照不 fsync、暂停前补完 MMIO 退出两处不同。内存文件与侧车都只 flush、不 sync_all；"
             "未打补丁的 Firecracker 对新路由回 400 或 404，orchestrator 识别为 RollbackUnsupportedError。")
    d.save(out("rollback", "08-1-三个扩展"))


# ------------------------------------------------------------------ 08-2
def f08_2():
    d = Diagram()
    d.header("FCDB 位图：24 字节头加 u64 位图字，页索引是内存快照文件里的偏移除以页大小",
             "这是 orchestrator 与 Firecracker 之间唯一的二进制格式。Rust 侧 serialize_dirty_bitmap / deserialize_dirty_bitmap，"
             "Go 侧 dirtyBitmap.writeTo / readDirtyBitmap，两侧镜像实现，改一个必须同时改另一个。",
             legend=[("flow", "对应关系")])
    M, W = d.M, d.W

    # ---------------- 1 逐字节
    d.section(1, "逐字节布局", note="整数一律小端")
    y = d.y + 40
    segs = [(0, 4, "magic", "\"FCDB\"", 150), (4, 4, "version", "u32，当前 1", 180),
            (8, 8, "page_size", "u64", 260), (16, 8, "num_pages", "u64", 260),
            (24, None, "位图字", "u64，共 (num_pages + 63) / 64 个", None)]
    used = sum(s[4] for s in segs if s[4])
    x0 = M + 150
    total = W - M - x0
    x = x0
    d.text(M, y + 36, "FCDB 文件", 16, INK, 600)
    d.text(M, y + 58, "（侧车、活跃脏图）", 13, MUTED)
    for off, ln, name, sub, w in segs:
        w = w or total - used
        head = off < 24
        f, s = (GF, GS) if head else (TF, TS)
        d.rect(x, y, w, 80, f, s, sw=1.4, rx=4)
        d.text(x + w / 2, y + 34, name, 16, GT if head else TT, 600, "middle")
        d.text(x + w / 2, y + 58, sub, 13, GSUB if head else TSUB, 400, "middle")
        mono(d, x, y - 10, str(off), 13, MUTED, "start")
        x += w
    d.text(x - 4, y - 10, "文件长度 = 24 + 8 × 字数，读时必须恰好相等", 13, MUTED, 400, "end")
    d.text(x0, y + 108, "例：2 GiB guest 内存、4 KiB 页 → num_pages = 524288，位图 8192 个字，文件长 24 + 65536 字节。"
                       "magic、version 不符，或长度不对，两侧一律拒绝。", 14, SUB)
    d.y = y + 118

    # ---------------- 2 位与页
    d.section(2, "第 w 个字的第 i 位 = 页 w × 64 + i；最后一个字超出 num_pages 的位必须为 0", highlight="本图重点")
    y = d.y + 24
    d.text(M, y + 30, "位图字", 16, INK, 600)
    d.text(M, y + 52, "每字 64 位", 13, MUTED)
    words = [("字 0", "页 0 – 63"), ("字 1", "页 64 – 127"), ("…", None), ("最后一个字", None)]
    ww = [250, 250, 90, None]
    ww[3] = (W - M - x0) - sum(ww[:3]) - 3 * 14
    x = x0
    for k, ((t, s), w) in enumerate(zip(words, ww)):
        if t == "…":
            d.text(x + w / 2, y + 44, "…", 20, MUTED, 600, "middle")
        elif k < 3:
            d.rect(x, y, w, 70, TF, TS, sw=1.2, rx=6)
            d.text(x + w / 2, y + 30, t, 15.5, TT, 600, "middle")
            d.text(x + w / 2, y + 52, s, 13, TSUB, 400, "middle")
        else:
            vw = w * 0.42
            d.rect(x, y, vw, 70, TF, TS, sw=1.2, rx=6)
            d.text(x + vw / 2, y + 30, "低 r 位：对应页", 15.5, TT, 600, "middle")
            d.text(x + vw / 2, y + 52, "r = num_pages % 64", 13, TSUB, 400, "middle")
            d.rect(x + vw, y, w - vw, 70, "#FFFFFF", GS, sw=1.2, rx=6, dash="5 4")
            d.text(x + vw + (w - vw) / 2, y + 30, "其余位：不对应任何页，必须为 0", 15.5, SUB, 600, "middle")
            d.text(x + vw + (w - vw) / 2, y + 52, "全 1 位图的尾字 = (1 << r) − 1", 13, MUTED, 400, "middle")
        x += w + 14
    y += 94
    d.text(x0, y + 4, "不掩码时单看一个位图不会出错；但 Firecracker 的全 1 侧车与 orchestrator 的 allOnesBitmap 尾字处理不同，"
                    "求并后做字级比较或计数就会不一致。", 14, SUB)
    y += 14
    d.y = y

    # ---------------- 3 页索引的空间
    d.section(3, "页索引在哪个空间：内存快照文件的偏移，不是 guest 物理地址", note="§5.2")
    y = d.y + 30
    LBL = 150
    xa = M + LBL
    full = W - M - xa
    # guest 物理地址：region 0，保留洞，region 1
    r0, hole = 520, 220
    r1 = full - r0 - hole - 220
    d.text(M, y + 34, "guest 物理地址", 15.5, INK, 600)
    d.text(M, y + 54, "可能不连续", 13, MUTED)
    d.rect(xa, y, r0, 64, GF, GS, sw=1.2, rx=6)
    d.text(xa + r0 / 2, y + 38, "region 0", 15.5, GT, 600, "middle")
    d.rect(xa + r0, y, hole, 64, "#FFFFFF", GS, sw=1, rx=6, dash="4 4")
    d.text(xa + r0 + hole / 2, y + 38, "架构保留洞", 14, MUTED, 400, "middle")
    d.rect(xa + r0 + hole, y, r1, 64, GF, GS, sw=1.2, rx=6)
    d.text(xa + r0 + hole + r1 / 2, y + 38, "region 1", 15.5, GT, 600, "middle")
    y2 = y + 150
    d.text(M, y2 + 30, "快照文件偏移", 15.5, INK, 600)
    d.text(M, y2 + 50, "按顺序首尾相接", 13, MUTED)
    d.rect(xa, y2, r0, 64, TF, TS, sw=1.2, rx=6)
    d.text(xa + r0 / 2, y2 + 38, "region 0", 15.5, TT, 600, "middle")
    d.rect(xa + r0, y2, r1, 64, TF, TS, sw=1.2, rx=6)
    d.text(xa + r0 + r1 / 2, y2 + 38, "region 1", 15.5, TT, 600, "middle")
    d.arrow(xa + r0 / 2, y + 66, xa + r0 / 2, y2 - 6, "flow")
    d.arrow(xa + r0 + hole + r1 / 2, y + 66, xa + r0 + r1 / 2 + 4, y2 - 6, "flow")
    d.text(xa + r0 + hole + r1 / 2 + 14, y + 112, "Firecracker 换算：region 起始页号 + region 内页号", 13.5, SUB, 600)
    d.text(xa + r0 + r1 + 20, y2 + 26, "页索引 = 文件偏移 ÷ 页大小", 15, TT, 600)
    d.text(xa + r0 + r1 + 20, y2 + 48, "num_pages 长的平坦位空间", 13, TSUB)
    y3 = y2 + 64 + 30
    bw = (W - 2 * M - 2 * 24) / 3
    cards = [("orchestrator 不需要知道内存布局", "只在平坦位空间上求并，在平坦文件上 pread / pwrite", "teal"),
             ("Firecracker 负责两个空间的换算", "dump_dirty 写页、userspace_bitmap_flat 展平、restore_dirty 写回", "gray"),
             ("几何不符：报错，不猜测", "entryBitmap、merge、回滚端点三处核对页大小与页数", "gray")]
    for k, (t, s, r) in enumerate(cards):
        d.block(M + k * (bw + 24), y3, bw, 100, t, s, r, "outline")
    d.y = y3 + 100
    d.footer("注：位图与某个具体的内存布局绑定，换了 region 划分，老位图就不能用了，几何校验会挡住它。"
             "格式定义在 firecracker/src/vmm/src/vstate/memory.rs:533-596 与 internal/checkpoint/bitmap.go。")
    d.save(out("rollback", "08-2-FCDB位图格式"))


if __name__ == "__main__":
    f08_1()
    f08_2()
