# 09 增量快照实现与 ARM 实测分析

> 读者：想搞清楚 e2b 的快照到底存了什么、"增量"是怎么算出来的、以及本仓库 ARM 版
> 为什么"增量不精确"的人。
>
> 配套脚本：`benchmark/snapshot.py`（本文第 5 节的实测就是用它跑的）。
> 相关文档：`01-整体架构与组件总览.md`（目录地图）、`08-源码开发与出包流程.md`（改这些代码怎么出包）。
>
> 结论先行：**ARM 版的内存增量是真实脏页集的"超集"——只要这一轮被 UFFD 换入过的页（读也算）
> 都会进增量**；磁盘增量两边都是精确的。原因只有一处代码改动，见第 4 节。

---

## 0. 三套实现一句话对比

| | Firecracker 原生 | e2b x86 版 | e2b ARM 版（本仓库 patch） |
|---|---|---|---|
| 内存产物 | `mem_file` 全量 dump；或 Diff 快照的稀疏文件 | 只把脏页导出成 `memfile` diff | 同 x86 |
| 内存脏页判据 | KVM dirty log（需 `track_dirty_pages=true`） | pagemap：`present && !uffd-wp` ⇒ **被写过** | pagemap：`present`（uffd-wp 从不置位）⇒ **被换入过（读也算）** |
| 内存增量精度 | 精确（写） | 精确（写） | **保守超集**（读或写） |
| 磁盘产物 | 无（快照不含磁盘，靠外部保证一致） | overlay COW cache 里被写过的块 | 同 x86 |
| 磁盘增量精度 | — | 精确（写） | 精确（写） |
| 谁来写内存文件 | Firecracker 自己 | orchestrator 用 `process_vm_readv` 从 FC 进程里拷 | 同 x86 |
| 恢复 | `/snapshot/load` + File 或 Uffd backend | `/snapshot/load` + Uffd，页按 header 映射从各代 diff 按需取 | 同 x86 |

正确性不受影响：超集只是多存了一些没被改的页，恢复时覆盖成相同内容；代价是**空间和时间**。

---

## 1. 一次快照到底产生哪些文件

### 1.1 对象存储里的布局

来自 `packages/shared/pkg/storage/template.go:10-15,27-52`，键名以 **build ID**（UUID）为前缀：

```
<buildID>/snapfile              # Firecracker 的 vmstate（vCPU 寄存器、设备状态…）
<buildID>/memfile               # 本次快照的内存 diff（只含脏页）
<buildID>/memfile.header        # 内存的 block 映射表（见 3.3）
<buildID>/rootfs.ext4           # 本次快照的磁盘 diff（只含被写过的块）
<buildID>/rootfs.ext4.header    # 磁盘的 block 映射表
<buildID>/metadata.json         # 模板元数据（kernel/FC 版本等）
```

> 注意 **build ID ≠ snapshot ID**。SDK 里 `create_snapshot()` 返回的
> `l1xjsvf1d8h3umr8d11y:default` 是 `templateID:tag`；存储前缀是这次快照对应的 build ID。
> 想对上号：跑完立刻按时间倒排看最新目录，或去 Postgres 查 build 表。

### 1.2 节点本地的落地路径

打快照时产物**先写到节点本地缓存**，再由 provider 上传到持久化存储。
下面这三条是**缓存**，跟 `STORAGE_PROVIDER` 是什么无关（MinIO 模式下同样有）：

| 路径 | 代码 | 内容 |
|---|---|---|
| `/orchestrator/build/<buildID>-memfile-<rand>` | `build/diff.go:77` `GenerateDiffCachePath` | 内存 diff |
| `/orchestrator/build/<buildID>-rootfs.ext4-<rand>` | 同上 | 磁盘 diff |
| `/orchestrator/template/<buildID>/cache/<uuid>/` | `storage/template_cache.go:50` `cacheDir` | `snapfile` + `metadata.json` |

`<rand>` / `<uuid>` 是每次进缓存时生成的标识，用来避免"旧缓存项被关闭时删掉新缓存项的文件"。
这就是第 5 节 `ll /orchestrator/build` 看到的那些文件名的来历。缓存有 TTL（25h）、容量驱逐，
而且 orchestrator 启动时会 `cleanDir(DEFAULT_CACHE_DIR)` 整个清空
（`sandbox/template/cache.go:78`），所以它**不是**快照的家。

真正的持久化落点由 `STORAGE_PROVIDER` 决定：本仓库单机部署里它被
`e2b-deploy/dep/template-manager.hcl` **写死成 `Local`**（`.env` 里的 `MinioBucket`
在 nomad 路径上没有 job 消费），Local 的默认 base path 是 `/tmp/templates`
（构建层缓存 `/tmp/build-cache`）。完整梳理见 `10-模板与快照存储位置梳理.md`。

---

## 2. 对照组：Firecracker 原生快照

原生 FC 的接口是 `PUT /snapshot/create`，参数 `snapshot_path` + **`mem_file_path`（原生必填）** +
`snapshot_type: Full | Diff`：

- **Full**：把整个 guest 内存原样写成一个文件。1 GiB 内存的 microVM 就是 1 GiB 文件，
  每次快照都要写一遍。
- **Diff**：需要先在 `machine-config` 里打开 `track_dirty_pages=true`，FC 通过 **KVM dirty log**
  记录自上次快照以来被写过的页，只写这些页。产物是一个稀疏文件，**不能直接 load**，
  必须自己按顺序把 base + 各代 diff 合并成一个完整 memfile。
- **恢复**：`PUT /snapshot/load`，内存后端可以是 `File`（整份 mmap 进来）或 `Uffd`
  （FC 建 userfaultfd 并通过 unix socket 把 fd 交给外部 handler，缺页时由 handler 供页）。

对 e2b 的场景，原生方案有三个不合适的地方：

1. 全量 dump 的 IO 和空间开销直接和内存大小挂钩，和"改了多少"无关；
2. Diff 链要自己 merge，且 merge 是线性的，链一长恢复就变慢；
3. 产物是本地文件语义，往对象存储上放、多节点共享、按需取页都要另外做一层。

---

## 3. e2b x86 版：自己实现的一套内存 + 磁盘快照

### 3.1 打快照的完整流程

代码里 `packages/orchestrator/internal/sandbox/sandbox.go:864-874` 的注释就是权威描述，
对应实现如下：

| 步 | 做什么 | 代码 |
|---|---|---|
| 1 | 暂停 VM | `sandbox.go:905` `s.process.Pause` |
| 2 | 调 `PUT /snapshot/create`，**只传 `snapshot_path`，不传 `mem_file_path`** → 自定义 FC 只写 vmstate 并 flush 磁盘，**不 dump 内存** | `fc/client.go:121-139`；接口定义见 `packages/shared/pkg/fc/firecracker.yml:1312-1316`（`mem_file_path` 在这份 spec 里是可选的） |
| 3 | 调 `GET /memory/dirty` 取脏页位图（必须 paused 才能调） | `fc/client.go:328`、`uffd/uffd.go:219` |
| 4 | 若沙箱不是 resume 来的（`NoopMemory`，即模板构建时的第一次快照），改调 `GET /memory` 拿 resident/empty 位图（mincore 语义） | `uffd/noop.go:35`、`fc/client.go:311` |
| 5 | 调 `GET /memory/mappings` 拿 guest 页 → host 虚拟地址的映射，然后用 `process_vm_readv` **直接从 FC 进程地址空间**把脏页拷进本地 cache 文件 | `fc/memory.go:24-57`、`block/cache.go:367` `NewCacheFromProcessMemory` |
| 6 | 磁盘：把 overlay 的 COW cache "弹出来"作为 rootfs diff | `rootfs/nbd.go:72-83` `EjectCache` |
| 7 | 用「上一代 header + 本次 diff」合成新 header | `header/metadata.go:52-96` `ToDiffHeader` |
| 8 | 异步上传 `snapfile/memfile/rootfs/两个 header/metadata.json` | `sandbox/snapshot.go:24-75`、`server/sandboxes.go:604-620` |

两个值得注意的设计：

- **内存不经过 FC 落盘**。FC 只负责"停下来 + 报告脏页 + 报告地址映射"，真正的数据搬运由
  orchestrator 用 `process_vm_readv` 从 FC 进程里读。省掉了 FC 写文件这一跳，也让
  "只导出脏页"变得简单。
- **快照之后沙箱是"重启"的**。导出 rootfs diff 时会把旧沙箱停掉
  （`rootfs/nbd.go:86-91` 里的 `closeSandbox`），`Checkpoint` RPC 随后用刚生成的 build
  重新 `ResumeSandbox` 一次，沙箱 ID 不变但 FC 进程和 UFFD 都是新的
  （`server/sandboxes.go:430-508`）。**这条对第 5 节的实测结果非常关键。**

### 3.2 脏页判据：UFFD 写保护位

这是 x86 版"精确增量"的核心，链条是这样的：

1. UFFD handler 只注册 MISSING 事件（`uffd/userfaultfd/userfaultfd.go:230-252`，
   代码里明说 "MINOR and WP flags are not expected as we don't register the uffd with these flags"）。
2. **读缺页**时，`UFFDIO_COPY` 带上 `UFFDIO_COPY_MODE_WP`，把页填进去但**保留 uffd-wp 写保护位**：

   ```go
   // packages/orchestrator/internal/sandbox/uffd/userfaultfd/userfaultfd.go:349-356
   var copyMode CULong

   // Performing copy() on UFFD clears the WP bit unless we explicitly tell
   // it not to. We do that for faults caused by a read access. Write accesses
   // would anyways cause clear the write-protection bit.
   if accessType != block.Write {
       copyMode |= UFFDIO_COPY_MODE_WP
   }
   ```

3. **写缺页**时不带 WP，页直接可写 ⇒ 就是脏页。
4. 之前只读过的页后来被写 ⇒ uffd-wp 位被清掉 ⇒ 变成脏页。
5. 打快照时 FC 侧统计：`GET /memory/dirty` 的语义在 spec 里写得很清楚——
   *"Returns a bitmap of dirty pages (**pages written to since write-protection was enabled**)"*
   （`packages/shared/pkg/fc/firecracker.yml:653`）。

FC 内部是怎么算的？本仓库随包发的 `firecracker.arm` 没有 strip，调试符号里能看到：

```
vmm::Vmm::get_dirty_memory                       # GET /memory/dirty 的实现
vmm::rpc_interface::RuntimeApiController::get_dirty_memory_info
firecracker::api_server::request::memory::parse_get_memory_dirty
vmm::utils::pagemap::PagemapReader               # 读 /proc/<pid>/pagemap
vmm::utils::pagemap::PagemapEntry::is_present
vmm::utils::pagemap::PagemapEntry::is_write_protected   # pagemap 的 uffd-wp 位
vmm::vstate::vm::mincore_bitmap                  # GET /memory 的 resident 位图走 mincore
```

也就是说：**脏 = 该页 present 且 uffd-wp 位已被清掉**。x86 上"只读过的页"因为第 2 步保住了
uffd-wp，所以不算脏——这就是精确增量的来源。

> 说明：自定义 Firecracker 的**源码**不在 `AustinJiangg/e2b-infra` 和 `AustinJiangg/infra`
> 这两个仓库里（版本号见 `.github/actions/build-sandbox-template/action.yml` 的
> `FIRECRACKER_VERSION: v1.12.1_a41d3fb`）。本节 FC 侧的判据来自**接口 spec 的描述**
> 和**随包二进制的调试符号**，够用来解释行为；要逐行确认得去看那个 FC fork。

### 3.3 增量链是"拍平"的，不是链表

每一代 header 里是一组 block 映射：

```go
// packages/shared/pkg/storage/header/mapping.go:14-20
type BuildMap struct {
    Offset             uint64     // 本层文件里的偏移
    Length             uint64
    BuildId            uuid.UUID  // 这段数据实际存在哪个 build 的 diff 里
    BuildStorageOffset uint64     // 在那个 diff 文件里的偏移
}
```

新 header = `MergeMappings(上一代 mapping, 本次 diff 的 mapping)` 再 `NormalizeMappings`
（`header/metadata.go:66-76`）。所以：

- **恢复时不需要逐代回溯**：按 offset 查一次映射表就知道该去哪个 build 的哪个偏移取这一页，
  链再长也是一次查表 + 一次读；
- **但被引用到的每一代 diff 文件都必须还在**。删中间某个快照会打断它的所有后代
  （这也是为什么 `delete_snapshot` 要走 API，不要手工删存储里的目录）；
- 全零页有单独的处理：`Empty` 位图映射到 `uuid.Nil`（`header/metadata.go:38-43`），
  读到这些 range 直接给零页，不占存储。不过 **resume 路径上 `Empty` 传的是空位图**
  （`fc/client.go:339`），只有非 resume 的首次快照（`NoopMemory`）才会真正做零页剔除。

---

## 4. ARM 版改了什么

`0001-adapted-for-arm-architecture.patch` 里跟快照路径相关的功能性改动**只有一处**：

```diff
--- a/packages/orchestrator/internal/sandbox/uffd/userfaultfd/userfaultfd.go
+++ b/packages/orchestrator/internal/sandbox/uffd/userfaultfd/userfaultfd.go
@@ -351,10 +351,10 @@ func (u *Userfaultfd) faultPage(
     // Performing copy() on UFFD clears the WP bit unless we explicitly tell
     // it not to. We do that for faults caused by a read access. Write accesses
     // would anyways cause clear the write-protection bit.
-    if accessType != block.Write {
-        copyMode |= UFFDIO_COPY_MODE_WP
-    }
-
+    //if accessType != block.Write {
+    //  copyMode |= UFFDIO_COPY_MODE_WP
+    //}
+
     copyErr := u.fd.copy(addr, pagesize, b, copyMode)
```

配套的另外三处是环境适配，不影响增量语义：

| 改动 | 位置 | 原因 |
|---|---|---|
| `machine-config` 去掉 `TrackDirtyPages`，`smt` 改 false | `fc/client.go:232-243` | arm64 上 FC 不支持 SMT；反正也没用 KVM dirty log |
| `uffdMsgListenerTimeout` 10s → 120s | `uffd/uffd.go:30` | ARM 上 FC 起得慢，等 uffd socket 的窗口放宽 |
| `Cache.WriteAtWithoutLock` 增加 nil/范围检查 + recover | `block/cache.go:263` | 防 mmap 失效时 SIGBUS 打崩进程 |

**后果链条**（把 3.2 的五步代进去）：

```
不带 UFFDIO_COPY_MODE_WP
        ↓
换入的页没有 uffd-wp 位（内核也不会因为写它而发 WP fault）
        ↓
FC 的判据 present && !uffd-wp 恒为真
        ↓
"这一轮被 UFFD 换入过的页" 全部计入脏页位图
        ↓
内存增量 = 真实脏页集的超集（读过 = 脏）
```

几个推论，测之前就能预判：

- **只读操作也会产生内存增量**，而且量级等于"这一轮的常驻工作集"，不是 0。
- **prefetch / prefault 会被直接算进增量**。`Prefault` 走的是同一个 `faultPage`
  （`userfaultfd.go:279-294`，`accessType = block.Prefetch`）：x86 上带 WP 不算脏，
  ARM 上预取多少页就脏多少页。ARM 上调大预取 = 直接放大每次快照的体积。
- **磁盘侧完全不受影响**。rootfs 增量来自 overlay 的 COW cache（只记录写进来的块），
  和 uffd-wp 没有关系，所以 ARM 上磁盘增量依然精确。
- **正确性不受影响**：多存的页内容和 base 里一样，恢复时覆盖成相同的值。

---

## 5. ARM 实测

### 5.1 环境与命令

- 单台 ARM 服务器，本仓库 RPM 部署（`build.sh -i` + `build.sh -s`）；
- 模板 `base`：2 vCPU / **2048 MiB 内存**（`benchmark/build_template.py`，2026-09-22 起；此前为 1 vCPU / 1024 MiB，第 09 篇的实测数据是在旧规格下采的）；
- 存储 provider 实际生效值为 `Local`，产物在 `/orchestrator/`；
- 脚本：`benchmark/snapshot.py --mode diff`——对同一个沙箱连打三次快照，
  #1 基线、#2 中间什么都不做、#3 写入 200 MiB `/dev/urandom` 之后。

```bash
python snapshot.py --mode diff --keep-snapshot --blob-mb 200
```

```text
[Diff] Sandbox created with ID: i4fok94emdlks0orekny1
[Diff] #1 基线: o5o26zjbkdxfbq8va94f:default  耗时 0.42s
[Diff] #2 无任何改动: xqnuedu5m8ybo7d2h847:default  耗时 0.41s
[Diff] $ dd if=/dev/urandom of=/home/user/blob.bin bs=1M count=200 ... 287 MB/s
[Diff] #3 写入 200MB 之后: ndecfaeo3m7o2g0dzb3e:default  耗时 1.10s
```

产物（`ll -th /orchestrator/build | head -7`，按时间倒排，最新的在最上面）：

```text
-rw-r--r--. 201M  64ed7d77-...-rootfs.ext4-q8ny0qtlmxnvwmnf7qzi
-rw-r--r--. 338M  64ed7d77-...-memfile-120vfpykatcrf4rc4y1u
-rw-r--r--. 112M  741c57cf-...-memfile-0hcj4n95xzasxs8x8tmt
-rw-r--r--.    0  741c57cf-...-rootfs.ext4-roqxfb4t0wuo5me7l7vb
-rw-r--r--. 112M  766fde2a-...-memfile-jwb4gxwgklxqltkfktlw
-rw-r--r--.    0  766fde2a-...-rootfs.ext4-msfl0af0encx21f8qr6a
```

`/orchestrator/template/<buildID>/` 三个目录各 24K（`cache/` 里是 `snapfile` + `metadata.json`）。

### 5.2 结果表

| 快照 | 场景 | memfile diff | rootfs diff | snapfile 等 | 耗时 |
|---|---|---|---|---|---|
| #1 | 沙箱刚起，基线 | **112 MiB** | 0 | 24 K | 0.42 s |
| #2 | 中间什么都没做 | **112 MiB** | **0** | 24 K | 0.41 s |
| #3 | 写了 200 MiB 随机数据 | **338 MiB** | **201 MiB** | 24 K | 1.10 s |

### 5.3 逐条解读

**① #2 = 112 MiB，是 ARM 语义最直接的证据。**
两次快照之间一条命令都没跑，如果是精确增量，这里应该接近 0（x86 版的预期值，本环境未实测）。
112 MiB 对应的正是"从 #1 恢复之后到 #2 之间被 UFFD 换入的页"。

**② 为什么"什么都不做"还会有换入？**
因为第 3.1 节最后那条：`create_snapshot` 之后沙箱不是原地继续跑，而是**被停掉再从刚生成的
快照 resume 一次**（`server/sandboxes.go:430-508`）。新的 FC 进程 + 新的 UFFD ⇒ 内存全部回到
missing 状态 ⇒ 系统跑起来必然要重新换入自己的工作集。x86 上这些是"读进来的干净页"不计入增量，
ARM 上全部算脏。

**③ 112 MiB ≈ 1 GiB 模板的 11%**，就是这个沙箱（内核 + systemd + envd + 后台进程）的常驻工作集。
换句话说：**ARM 上每次快照的内存增量有个下限，约等于工作集大小，和你改没改东西无关。**

**④ #3 memfile 338 MiB ≈ 112 + 200 + 26。**
`dd` 写文件走 page cache，guest 的页缓存本身就是 guest 内存，所以这 200 MiB 会**同时**出现在
memfile 和 rootfs 两份增量里。剩下 ~26 MiB 是 dd 缓冲、文件系统元数据和这段时间新换入的页。

**⑤ #3 rootfs 201 MiB，#1/#2 rootfs 0 字节——磁盘侧是精确增量。**
0 字节这条尤其能说明问题：如果磁盘侧也有"读即脏"，#1/#2 不可能是 0（沙箱启动必然读了一堆盘）。
这正好印证第 4 节的推论：rootfs 走 overlay COW cache，只记写入。

**⑥ 耗时。**
#3 比 #2 多 0.7 s，多出的数据是 (338-112) + 201 ≈ 427 MiB，约 600 MB/s，
和 `process_vm_readv` + 写本地 cache 文件的量级吻合。#1/#2 的 0.4 s 里，
真正搬 112 MiB 只占一部分，其余是 pause / snapshot / 导出 rootfs / 重新 resume 的固定开销。

> 想复现 #2 应该多小才算"精确"，最省事的对照是拿同一份脚本在 x86 部署上跑一遍 `--mode diff`，
> 直接比 #2 的 memfile 大小。

---

## 6. 影响与优化方向

**影响**

- 每次快照/暂停恢复的内存增量下限 ≈ 工作集（本例 112 MiB），链越长放大越明显：
  连打 N 次快照约占 `N × 工作集 + 真实改动`，x86 上则是 `工作集 + 真实改动`。
- 开了自动 pause/resume 的用法（`lifecycle={"on_timeout":"pause"}`）在 ARM 上尤其吃存储。
- 占的是**持久化存储**（本部署是 Local ⇒ `/tmp/templates`）。快照必须走 API 删
  （`Sandbox.delete_snapshot` 或 `python snapshot.py --mode cleanup`），手工删存储目录会
  打断后代快照的映射。`/orchestrator/build` 那份是缓存（TTL 25h + 容量驱逐 +
  orchestrator 重启清空），看着大不代表泄漏。

**优化方向（按性价比排序）**

1. **恢复 uffd-wp**（最彻底）。前提是内核 + FC 在 arm64 上支持 userfaultfd 写保护
   （`UFFDIO_REGISTER_MODE_WP` / `UFFDIO_COPY_MODE_WP`）。验证路径：
   - `grep -i userfault /boot/config-$(uname -r)`，确认 `CONFIG_USERFAULTFD=y`；
   - 写个最小 C 程序试 `UFFDIO_REGISTER_MODE_WP` 注册 + `UFFDIO_COPY_MODE_WP` 拷贝，
     看是否返回 `EINVAL`；
   - 都通过再把 `userfaultfd.go:354-356` 那两行注释放开，重新出包，
     用本文 #2 的判据回归（#2 的 memfile 应该掉到 MiB 级）。
2. **收敛 prefetch**。ARM 上预取的页会直接进增量，预取带来的启动收益要和存储放大一起算账。
3. **减小模板内存 / 工作集**。1 GiB 模板 112 MiB 工作集，模板越大工作集通常越大。
4. **定期压平长链**。链太长时重新 build 一个全量模板，避免映射表和文件数无限增长。

---

## 7. 自己复现 / 自查

```bash
# 0. 确认 orchestrator 实际生效的存储 provider（本部署应为 Local）
tr '\0' '\n' < /proc/$(pgrep -x orchestrator | head -1)/environ \
  | grep -E 'STORAGE_PROVIDER|MINIO_|BUCKET_NAME'

# 1. 跑增量探针，保留快照
cd /path/to/e2b-infra/benchmark
python snapshot.py --mode diff --keep-snapshot --blob-mb 200

# 2. 量产物（三组 build ID 对应 #1/#2/#3）
ll -th /orchestrator/build | head -7      # 本地缓存里的 diff，量增量看这里最直观
du -sh /orchestrator/template/*/
du -sh /tmp/templates/*/ | tail          # 持久化落点（Local provider 默认路径）

# 3. 判据
#    #2 的 memfile ≈ 工作集（ARM）还是 ≈ 0（精确增量）？
#    #1/#2 的 rootfs 应该是 0 字节；#3 的 rootfs ≈ 写入量

# 4. 收尾（全量清理：所有快照 + 所有 paused 沙箱）
python snapshot.py --mode cleanup --dry-run
python snapshot.py --mode cleanup
```

---

## 8. 代码索引

x86 版（`AustinJiangg/infra` 分支 `2026.09-dev`）：

| 位置 | 内容 |
|---|---|
| `packages/orchestrator/internal/sandbox/sandbox.go:864-874` | Pause 流程的权威注释 |
| `packages/orchestrator/internal/sandbox/sandbox.go:875-975` | `Sandbox.Pause` 主流程 |
| `packages/orchestrator/internal/sandbox/sandbox.go:993-1031` | `pauseProcessMemory`：脏页 → diff + header |
| `packages/orchestrator/internal/sandbox/sandbox.go:1033-` | `pauseProcessRootfs` |
| `packages/orchestrator/internal/sandbox/snapshot.go:24-75` | 快照产物上传 |
| `packages/orchestrator/internal/sandbox/fc/client.go:121-139` | `createSnapshot`：只传 snapshot_path |
| `packages/orchestrator/internal/sandbox/fc/client.go:298-341` | `/memory/mappings`、`/memory`、`/memory/dirty` |
| `packages/orchestrator/internal/sandbox/fc/memory.go:24-57` | `ExportMemory`：脏页 → host 虚拟地址区间 |
| `packages/orchestrator/internal/sandbox/block/cache.go:367-391` | `NewCacheFromProcessMemory`（`process_vm_readv`） |
| `packages/orchestrator/internal/sandbox/uffd/uffd.go:219-221` | resume 沙箱的 `DiffMetadata` |
| `packages/orchestrator/internal/sandbox/uffd/noop.go:35-` | 非 resume 沙箱的 `DiffMetadata`（mincore） |
| `packages/orchestrator/internal/sandbox/uffd/userfaultfd/userfaultfd.go:230-252` | 只注册 MISSING，区分读/写缺页 |
| `packages/orchestrator/internal/sandbox/uffd/userfaultfd/userfaultfd.go:349-356` | **`UFFDIO_COPY_MODE_WP`，精确增量的关键** |
| `packages/orchestrator/internal/sandbox/rootfs/nbd.go:72-99` | rootfs diff = overlay COW cache |
| `packages/orchestrator/internal/server/sandboxes.go:430-508` | `Checkpoint`：快照后重新 resume |
| `packages/shared/pkg/storage/header/mapping.go:14-20` | `BuildMap` |
| `packages/shared/pkg/storage/header/metadata.go:27-96` | 映射合并 / `ToDiffHeader` |
| `packages/shared/pkg/storage/template.go:10-52` | 存储键名 |
| `packages/shared/pkg/storage/template_cache.go:50` | 本地缓存目录 |
| `packages/shared/pkg/fc/firecracker.yml:635-663` | `/memory`、`/memory/dirty` 的语义 |
| `packages/shared/pkg/fc/firecracker.yml:1312-1330` | `SnapshotCreateParams`（`mem_file_path` 可选） |

ARM 版（本仓库 `0001-adapted-for-arm-architecture.patch`）：

| 补丁段 | 内容 |
|---|---|
| `uffd/userfaultfd/userfaultfd.go` | 注释掉 `UFFDIO_COPY_MODE_WP` ← 增量语义变化的唯一来源 |
| `uffd/uffd.go` | `uffdMsgListenerTimeout` 10s → 120s |
| `fc/client.go` | `machine-config` 去掉 `TrackDirtyPages`，`smt=false` |
| `block/cache.go` | `WriteAtWithoutLock` 防御性检查 + recover |
