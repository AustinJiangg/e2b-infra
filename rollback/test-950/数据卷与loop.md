# 数据卷：为什么是 loop、loop 怎么调、两台机器为什么不对称

## 一、为什么非用 loop 不可

两套方案要各自跑在自己的文件系统上，而**两台机器都切不出真盘**：

| | 920B | 950 |
|---|---|---|
| 根盘 | XFS 3.0T | **ext4 988G** |
| `vgs` 的 `VFree` | **0** | **0** |
| 空闲裸盘 / 空闲分区 | 无 | 无（只有一块 7T NVMe，已分完） |
| 能不能缩出空间 | XFS **不能缩** | `/home` 是 ext4，**不能在线缩**，6T 已挂载 |

所以"另一种文件系统"只能是**镜像文件 + loop 设备**。

> "在 /home 下创建一个分区文件"这个想法，就是 loop —— Linux 里文件要变成块设备，
> 只有 loop（或 dm）这一条路。能优化的不是"躲开 loop"，而是**把 loop 配对**。

## 二、loop 怎么建的

全部由 `02-prepare-loop-volume.sh` 完成，**两种文件系统共用同一段 loop 代码**：

```bash
fallocate -l 300G  <img>                      # ② 预分配
losetup -b 4096 --direct-io=on -f <img>       # ③ 4K 扇区  ① direct-io
mkfs.<fs>  <见下>  /dev/loopN
mount -o noatime /dev/loopN <mnt>
```

只有 mkfs 那一行分岔：

| | mkfs 选项 |
|---|---|
| **XFS** | `-f -K -m reflink=1,crc=1 -b size=4096` |
| **ext4** | `-F -E nodiscard -b 4096 -L e2b-ext4dev` |

`reflink=1` 只有 XFS 有，那是 XFS 套的全部意义（`crc=1` 是它的前提）。
`-K` / `-E nodiscard` 是同一件事的两种写法，见第四节。

## 三、三项调优，各自消掉什么

默认参数建出来的 loop，比裸盘慢得多，主要来自这三处：

| # | 做法 | 不做的话 |
|---|---|---|
| ① | `--direct-io=on` | loop 写宿主镜像走宿主页缓存 → 同一份数据在内存里两份、回写路径走两遍 |
| ② | `fallocate` 预分配（不是 `truncate` 稀疏） | 每碰一块新区域都要在宿主文件系统里现分配 extent → 延迟抖动 + 碎片。checkpoint 恰恰一直在写新区域 |
| ③ | `losetup -b 4096` | loop 逻辑扇区默认 512B，内层 4K 块的写变成读改写 |

外加 `mount -o noatime`。

脚本跑完会**自己核对这三项并打勾**：

```
  direct-io ✓
  逻辑扇区 4096 ✓
  预分配 ✓（实占/标称 = 100%）
  FICLONE 实测可用 ✓        ← 只有 XFS 有这一行
```

**不打勾就是没生效**，不要当成噪声跳过。

## 四、`nodiscard`：不加会把预分配当场捅穿

第一次在 920B 上建 300G，`fallocate` 明明成功了，自检却报"只占了标称的 0%"。

原因：**`mkfs` 默认会对整个设备发一次 discard**，而 loop 把 discard 翻译成
**对宿主镜像文件打洞**。刚分配好的 300G 被 mkfs 一句话全部还回去，文件又变回稀疏的，
第 ② 项调优白做。

`filefrag` 能直接看出来：

```
打洞后： 33 extents，实占 1.0 GB / 标称 300 GB
加了 nodiscard： 25 extents，实占 = 标称
```

所以 ext4 要 `-E nodiscard`，XFS 要 `-K`。

## 五、两台机器为什么不对称，以及这对数字意味着什么

| | 根盘 | ext4 套跑在 | XFS 套跑在 |
|---|---|---|---|
| **920B** | XFS | 调优过的 **loop** ext4 | 真根盘 XFS |
| **950** | ext4 | **真根盘 ext4** | 调优过的 **loop** XFS |

**这是有意的**：950 上的 ext4 套是我们真正要交付的东西，它的数字必须来自真盘，
不能掺 loop 的影响。而 950 上 XFS 卷只能是 loop，没得选。

**后果要说清楚**：

- 950 上 **ext4 套的绝对耗时是实数**，可以直接对性能指标。
- 950 上 **XFS 套的绝对耗时偏悲观**，两套横向比较时要记住这一点。
- **正确性和 reflink 省空间的效果不受 loop 影响** —— reflink 发生在镜像内部的 XFS 里。
- 要拿 XFS 套的产品级性能数，得单独配一块真盘走 `--device`。

顺带一个好处：loop 挂载点上 `df` 只看得见我们自己的数据；落在根盘上的话，
`df /` 会被机器上其它活动污染。`bench-ckpt.py` 的"新增物理空间"那一列就靠这个。

## 六、用法与撤销

```bash
# 建（950 的 XFS 套）
bash 02-prepare-loop-volume.sh --fs xfs --mnt /mnt/xfsdev --size 300G

# 建（920B 的 ext4 套）
bash 02-prepare-loop-volume.sh --fs ext4 --mnt /mnt/ext4dev \
     --img /home/j30059180/projects/e2b-repo/ext4dev.img --size 300G

# 重建（换参数时必须加 --force，否则会复用旧镜像）
... --force

# 真盘（有空闲盘时，会抹掉该设备）
bash 02-prepare-loop-volume.sh --fs xfs --device /dev/sdX
```

**撤销，不动宿主任何东西**：

```bash
umount /mnt/xfsdev
losetup -d /dev/loopN
rm -f <镜像>
```

300G 是预分配的，撤销之前它真占着 300G 磁盘。测完就撤。

## 七、XFS 还要额外设一项：`cowextsize`

跟 loop 无关，但属于同一类"卷层面的配置"，放在这里免得分散。

XFS 的 CoW extent 大小提示默认 **128 KB**。往 reflink 克隆里写**零散**的 4 KB 脏页时，
每一处都按 128 KB 的粒度分配，于是零散那部分被放大 32 倍。实测每次 checkpoint
固定多花约 **33 MB**（各档一致，与脏页量无关；同时段空闲 60 秒根盘 `df` 净增 0 MB，
已排除测量噪声）。

**`02-prepare-loop-volume.sh --fs xfs` 现在会自动设**，设在挂载点根上，
后面创建的目录和文件全部继承，自检里会打印 `cowextsize 4096 ✓`。

早先的写法是等 orchestrator 把 `build/checkpoints` 建出来之后再对那个目录补设一次：

```bash
xfs_io -c "cowextsize 4096" <ORCHESTRATOR_BASE_PATH>/build/checkpoints
```

这个办法能用，但要求人记得在"造完卷"和"开始测"之间插一步，忘了不报错、
只表现为物理占用莫名其妙比 ext4 套大一截。**用真盘（不走本脚本）时仍需手工设**，
对准挂载点根即可。

| 脏页 | 默认 128 KB | cowextsize=4 KB | ext4 套 |
|---|---|---|---|
| 0 MB | 36.03 MB | **4.78 MB** | 4.58 MB |
| 32 MB | 74.03 MB | **40.19 MB** | 39.81 MB |
| 256 MB | 307.07 MB | **272.41 MB** | 271.9 MB |

那 33 MB 整个消失，物理占用和 ext4 几乎逐字节一样。代价是创建耗时涨约 **6%**
（CoW 分配变多变碎）。**值得换。**
