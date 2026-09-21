# 交付清单

最后更新：2026-09-21（第三、四节同步到 deltabox-dev 融合后的状态；其余各节仍是 2026-08-28 开发收尾时的记录，
其中的分支名、commit、`bin/` 二进制与实测数字对应当时的代码，保留作历史。仓库级总索引见开发仓库根目录 `交付件清单.md`）

> **2026-09-21 起，RPM 仓库里 checkpoint / restore 三个组件的来源换了**：不再是 KASandbox / infra-arm / e2b-arm
> 三个开发仓库的 `jll` 分支，而是 openEuler 交付仓库 `KASandbox_0904` 的 `deltabox-dev` 分支
> （当前融合到 `4af2872c6`）的 `firecracker/`、`packages/`、`py-sdk/` 三个目录。第一节表格里的仓库与 commit
> 是 08-28 的状态，不再是 0001 / `firecracker.arm` / SDK 覆盖层的来源。XFS+reflink 套（`xfs-reflink` 分支）已归档，
> 此后只维护 `main`（ext4 差分树）。

两处产物，用途不同，不要混：

| | 位置 | 干什么用 |
|---|---|---|
| **测试工具箱** | `WSL:~/projects/e2b-repo/e2b-infra/rollback/scripts/dev/` | 拷到 950，**手工换二进制**跑验证。不走 RPM |
| **RPM 仓库** | `WSL:~/projects/e2b-repo/e2b-infra`（两个分支） | 正式部署：构建 rpm → 装到 `/opt` → `build.sh -i/-s` |

**2026-08-28 起 WSL 侧是完整的**：`bin/` 四个二进制（226MB）与全部报告都已从 920B
拉回并 `sha256sum -c` 校验通过，920B 上不再有 WSL 没有的东西。2026-09-15 起报告不再入库，
归档在工作区 `e2b-repo/rollback-reports/`（下文的 `reports/…` 都指那里）。

**去 950 要拷两份，缺一不可**：`e2b-infra`（部署）和 `rollback/scripts/dev/`（验证）。
后者已经并入 e2b-infra 仓库（2026-09-15 由 `rollback/test-950/` 迁来），但仍要单独拷到被测机上跑。

---

## 一、源码仓库的当前状态

| 仓库 | 分支 | commit | 说明 |
|---|---|---|---|
| KASandbox | `jll` | `3863c76` | ext4 套的 FC；含 seccomp `pread64` 修复 |
| KASandbox | `jll-xfs` | `2b06bb0` | XFS 套的 FC；同上 |
| infra-arm | `jll` | `ea52f88c3` | ext4 差分树；已 rebase 到 `2026.09`，不含 gsd；+ memMode + 宿主分阶段计时 + 封存不等盘 |
| infra-arm | `jll-xfs` | `6f8853e60` | XFS+reflink；同上 |
| e2b-infra | `main` | `c362670` | RPM：ext4 套 + SDK 覆盖层 + 交付侧验收脚本 |
| e2b-infra | `xfs-reflink` | `311b51e7` | RPM：XFS 套 + SDK 覆盖层 + 交付侧验收脚本 |
| **e2b-arm** | `jll` | `76703933` | **Python SDK**：去 gsd 命名、删死 header、加 `mem_mode` |

infra-arm 保留了 rebase 前的备份 tag：`pre-rebase-jll` / `pre-rebase-jll-xfs`。
已删除的废弃分支：`jll-fc-mem-snapshot`（`119032fac`）。

**2026-08-28 核对：四个仓库全部分支都已推到各自 origin，ahead 0 / behind 0。**

---

## 二、测试工具箱 `rollback/scripts/dev/`

### 脚本

| 文件 | 作用 | 是否实跑验证过 |
|---|---|---|
| `README.md` | 总说明：两套的区别、HDBSS 三级证据、完整流程、判据速查 | — |
| `950-摸底结论.md` | 950 摸底报告的解读与据此要改的东西 | — |
| `两个环境变量与已知坑.md` | `FC_TRACK_DIRTY_PAGES` / `ORCHESTRATOR_BASE_PATH` 详解；busybox 与 seccomp 两个坑 | — |
| `耗时基准结论.md` | **两套的 checkpoint/restore 耗时对照**：全量 vs 增量、冻结窗口、restore、连打劣化 | — |
| `数据卷与loop.md` | 为什么只能用 loop、三项调优各消掉什么、`nodiscard` 的坑、两台机器的不对称、`cowextsize` | — |
| `00-recon-950.sh` | 950 只读摸底：内核 HDBSS、cap 502 真开一把、磁盘、reflink、工具链、e2b 现状 | ✅ 920B 与 950 上都实跑过 |
| `01-check-host.sh` | 宿主自检（只读） | ✅ 920B |
| `cap_test.c` | KVM cap 502 探针，00/01/`hdbss_evidence.py` 会自动编译调用 | ✅ 两台机器 |
| `02-prepare-loop-volume.sh` | 造数据卷（`--fs xfs\|ext4`，loop 或 `--device` 真盘）。loop 做了三项调优：direct-io、`fallocate` 预分配、4K 逻辑扇区；`mkfs` 走 nodiscard 免得把预分配打穿。跑完自检并打勾 | ✅ 920B 上实跑过（建 300G loop ext4，direct-io/4K/预分配 100% 三项全绿，服务已跑在上面） |
| `03-switch.sh` | 两套之间切换；nomad job 名与 hcl 路径已改成自动探测 | ✅ 920B 上 `--yes` 实跑过多次（`backup/` 下 8 份时间戳备份为证）；**950 上未跑过** |
| `hcl_env.py` | 被 03 调用，幂等地改 nomad HCL 的 env 块 | ❌ 随 03 |
| `04-verify-runtime.sh` | 切换后冒烟：跑的是哪个 exe、进程真实 env、store 落在哪个 fs、FC 的 `dirty_tracking` | ⚠️ 没有留下可查的执行证据，按未验证对待 |
| `lib.py` | 测试脚本共用封装 | ✅ |
| `correctness.py` | 正确性 e2e（线性/前滚/分叉跨 LCA/删除语义/失败语义） | ✅ 两套都 ALL PASS |
| `timing.py` | 耗时与存储：逐代 create + 链深 5/20/50 | ✅ 两套 |
| `loop.py` | 稳定性：N 次回滚成功率与分位数 | ✅ 两套 |
| `hdbss_evidence.py` | HDBSS 三级证据 | ✅ 920B（负样本） |
| `bench-ckpt.py` | **耗时基准**：全量 vs 增量各档（0/16/32/64/128/256MB）的分布、虚机冻结窗口、**宿主分阶段**、restore 深浅集、60 秒持续速率 | ✅ ext4 套（见第六节） |
| `freeze_probe.py` | 被 `bench-ckpt.py` 推进 guest 里跑的 ~2.5kHz 时钟采样器，用时间序列里的"洞"量虚机冻结窗口 | ✅ 随 bench |
| `probe-ramp.py` | 判别连打劣化是"链变深"还是"写得多"：把脏页量拉开做同样多次 checkpoint，看哪种归一化收拢 | ✅ ext4（判定：跟着代数走） |
| `run-all.sh` | 一条命令跑完一套（correctness → timing → loop → bench），报告落 `reports/<方案>-<时间戳>/`；开头会等 API 接受调度 | ✅ 两套 |

> 参数顺序要注意：`correctness.py <方案> <df挂载点>`、`timing.py <方案> <df挂载点>`、
> `loop.py <方案> <次数>`、`bench-ckpt.py <方案> [--选项]`。`run-all.sh` 传的是对的；
> 自己手敲容易把挂载点当成方案名，那样 XFS 的产物会被拿 ext4 的规则去判，报假 FAIL。

### 数据卷：两台机器不对称

| | 根盘 | ext4 套 | XFS 套 |
|---|---|---|---|
| 920B | XFS | 调优过的 loop ext4（`/mnt/ext4dev`） | 直接用根盘 |
| **950** | **ext4** | **直接用根盘 —— 这套的数字是实数** | 调优过的 loop XFS |

950 上 `vg_free=0`、无空闲裸盘，XFS 卷只能是 loop，所以 **XFS 套的绝对耗时偏悲观**。
我们真正要交付的 ext4 套在 950 上跑在真盘上，不受影响。

### `bin/` 四个二进制

| 文件 | sha256[:16] | 来源 |
|---|---|---|
| `fc-ext4` | `9bfa37ab2a6b67fe` | KASandbox `jll@3863c76` |
| `fc-xfs` | `bab5f15bb3682dc3` | KASandbox `jll-xfs@2b06bb0` |
| `orchestrator-ext4` | 见 `bin/SHA256SUMS` | infra-arm `jll@d417aa973` + 真 busybox |
| `orchestrator-xfs` | 见 `bin/SHA256SUMS` | infra-arm `jll-xfs@f645faf65` + 真 busybox |

`bin/SHA256SUMS` 是这四条，`01-check-host.sh` 会 `sha256sum -c` 校验。

**配对规则（错了必定失败）**：`fc-ext4` ↔ `orchestrator-ext4`，`fc-xfs` ↔ `orchestrator-xfs`。
ext4 套的 orchestrator 依赖 `PUT /snapshot/save-dirty-bitmap`，只有 `fc-ext4` 有；
用 `strings <fc> | grep -c SaveDirtyBitmap` 区分（ext4 是 2，xfs 是 0）。

> orchestrator 不能直接 `go build` 出来就用：`2026.09` 分支里的
> `busybox_1.35_arm64` 是个 HTML 占位文件，编出来的 orchestrator 建模板会
> panic guest 内核。必须先照 spec 的做法把 RPM 仓库里的真 busybox 拷进去。
> 见 `两个环境变量与已知坑.md`，构建脚本 `tmp/build-rebased.sh` 已经这么做了。

---

## 三、RPM 仓库 `e2b-infra`

| 分支 | commit | 方案 |
|---|---|---|
| `main` | `c362670` | ext4 差分树（对文件系统无要求） |
| `xfs-reflink` | `311b51e7` | XFS+reflink（数据路径必须落在开了 reflink 的 XFS 上） |

**相对介入前（`0d8b2c4`）改动范围**，`e2b-infra.spec` **逐字节未动**：

| | 说明 |
|---|---|
| `0001-adapted-for-arm-architecture.patch` | ARM 适配 + checkpoint / restore；2026-09-21 起来源是 `deltabox-dev` 的 `packages/`（融合到 `4af2872c6`），由开发工作区的 `tmp/regen-e2b-infra-patch-deltabox.sh` 生成。738827 字节 / 20132 行 / 169 个文件，sha256 `90bd549c315dbbe0de4b1ea3bb27c94cb8164bb826a00e4c6b7312b9e7bd1c96` |
| `firecracker.arm` | 构建自 `deltabox-dev` 的 `firecracker/`（`86e4d9610`；到 `4af2872c6` 该目录无变化）。2677888 字节，sha256 `18f3faa7f47c173a5f47bfc7f578f5073cfbde2ac9bdb50bf88c434341d6d4c9`。含 seccomp `pread64` 修复、原地 rollback、`save-dirty-bitmap` |
| `e2b-deploy/build.sh` | `install_e2b` 里多调一次 SDK 覆盖层安装；两个脚本共用一个解析出来的解释器 |
| `e2b-deploy/dep/e2b-sdk-checkpoint/` | **新增**：SDK 覆盖层（installer + **21 个** payload 文件 + 使用说明）。08-28 时是 16 个；2026-09-21 起 21 个 = 12 个新增 + 9 个覆盖上游（新纳入 `e2b/sandbox/checkpoint/errors.py`、`e2b/exceptions.py`、`e2b/envd/{api,rpc}.py`、`e2b_connect/client.py`）。17 个取自 `py-sdk@4af2872c6`，4 个（`e2b/__init__.py`、`e2b/connection_config.py`、`e2b/sandbox_{sync,async}/main.py`）取自 `e2b-arm@1598c96e`，两边差异只来自 SDK 基线版本 |
| `e2b-deploy.tar.gz` | 随 `e2b-deploy/` 重打（保住了被 gitignore 的 70MB `ubuntu-22.04-custom.tar.gz`） |

spec 不用动的原因：`e2b-deploy.tar.gz` 本来就是 `Source9`，整个 `e2b-deploy/`
会被 `cp -rp` 到 `/opt/e2b-infra/`，覆盖层跟着走。

### 0001 patch 的来历与验证

> 四个仓库分别以什么形态落进 e2b-infra、为什么、以及单测要不要进 patch 的待定项，
> 见开发仓库根目录的 `仓库融合说明.md`（那份是开发态文档，不随交付走）。

**2026-09-21 起的做法**（细节见 `deploy-docs/08-源码开发与出包流程.md` §11）：起点是「upstream 2026.09 +
上一版 0001（`81f9612`）」，把 `deltabox-dev` 上 `0f9b58f92..4af2872c6` 触及 `packages/` 的 **44 个提交**
逐个只取 `packages/` 的 hunk 应用上去——41 个干净、1 个 3-way 自动合并、2 个因 openEuler 基线上下文不同
需要手工（`c42d23e73` 的 `block/chunk.go` 与 `uffd/userfaultfd/userfaultfd.go`，`579f75d68` 的 `network/network.go`），
外加一处编译期适配（`network/conntrack_handles.go`：`NamespacePath()` → `netns.GetFromName(s.NamespaceID())`）；
同步 vendor 15 个文件；再 `git diff upstream <融合树>` 出补丁。重跑：开发工作区的
`tmp/regen-e2b-infra-patch-deltabox.sh`（`VARIANT=M` 时除 deltabox-dev 外不依赖其它开发仓库）。

08-28 之前的做法（历史）：infra-arm 的 checkpoint 提交 cherry-pick 到「upstream 2026.09 + ARM 适配」那条线上，
再 `git diff upstream <branch>` 生成，脚本是 `tmp/regen-e2b-infra-patch.sh`。

**单测不在 patch 里（2026-08-25 起）。** 我们新增的 `_test.go`（2026-09-21 这一版共 **40 个**；
08-25 时是 6~7 个、约 1500 行）只留在开发仓库——现在是 **`deltabox-dev` 分支**，此前是 infra-arm 的
`jll` / `jll-xfs` 分支——不进 e2b-infra。原因：rpmbuild 的 `%build` 只跑 `go build`，从不 `go test`，
这些文件进了 RPM 源树一次也不会被编译。已有测试文件里跟着签名改的一两行**保留**，
否则树不自洽、`go test ./...` 会编译失败。**评审要看单测，去 `deltabox-dev`。**
另外，`internal/checkpoint` 与 `shared/pkg/proxy` 两个包的单测在 `-mod=vendor` 下编不了
（引用了 vendor 里没有的 `go.uber.org/zap/zaptest/observer`），只能在 deltabox-dev 上跑，见 08 篇 §11.4。
详见开发仓库根目录的 `仓库融合说明.md` 第六节。

已验证（2026-09-21 这一版）：
- 纯净上游树上 `patch -p1 --dry-run` 干净，`GIT binary patch` 为 0；补丁打出的树补回被剔除的 40 个单测后与融合树零差异
- 五个包 `GOWORK=off GOFLAGS=-mod=vendor go build ./...` 在 aarch64 上全过
- x86（WSL）与 aarch64（920B）各自从头生成，补丁逐字节相同
- 与 deltabox-dev 逐文件比对：我们动过的 100 个 `packages/` 文件 89 个逐字节相同，其余 11 个的差异均为基线差异、逐行有解释
- 补丁里 openEuler 基线独有符号（`KillOrphanedProcesses` / `Mooncake` / `ExternalNetNS` / `force_disconnect` / `NamespacePath`）出现 0 次

已验证（08-28 那一版，历史）：
- 新 patch 打到**原始上游树**上，**再补回被剔除的那几个单测**后，
  与移植树 `diff -r` **零差异**（两套都是）——两仓库的差异是可判定的
- `packages/{api,client-proxy,envd,db,orchestrator}` 全部以 `GOWORK=off GOFLAGS=-mod=vendor` 编过
- rebase 摘掉 gsd 之后重新生成，结果与之前那版**字节完全相同** —— 说明代码增量没变
- 生成脚本会自动把改过的 `packages/shared` 文件同步进各包的 `vendor/` 树
  （infra-arm 没有 vendor/，漏了这步 orchestrator 会编不过）

未验证：
- **完整 rpmbuild 只用重建的源码包跑过**（2026-09-22，920B）：`e2b-infra-2026.09.tar.gz` 在仓库里是 git-lfs 指针，LFS 原件（sha256 `59f05d43…5ea9`）在开发机上拿不到，验证用的是从原件解开的工作树重新打的等价包（去掉 `vendor/` 后与上游 2026.09 标签归档只差 `go.work.sum` 与被 Source1 覆盖的 busybox）。`%prep` 以 `--fuzz=0` 打 0001 无失败无偏移，`%build`、成包通过；拆包核对 `bin/firecracker` sha256 `18f3faa7…d4c9`、SDK 覆盖层 21 个文件与仓库逐字节相同。用 LFS 原件的 rpmbuild 由部署机（950）完成
- 950 上的实际部署与运行

### 为什么 firecracker 也在仓库里换

`e2b-deploy/dep/init-client.sh` 会把 `/opt/e2b-infra/bin/firecracker`（即仓库里的
`firecracker.arm`）拷进 `/fc-versions/v<ver>/firecracker` —— 仓库里这个才是真正跑起来的。
手动往 `/fc-versions/` 拷会在下次 `build.sh -i/-s` 时被覆盖。

---

## 四、环境变量：按平台怎么配

**目标平台 950 上什么都不用配。** orchestrator 启动时探测 KVM 的硬件脏状态跟踪能力
（`KVM_CHECK_EXTENSION(502)` > 0，即 HDBSS），探到就自动打开脏页跟踪，增量 checkpoint 走硬件标脏。
RPM 标准部署因此不传 `FC_TRACK_DIRTY_PAGES`：`e2b-deploy/dep/template-manager.hcl` 的 `env {}` 块里没有它，
`e2b-deploy/dep/deploy.sh:131` 起的 `envsubst` 白名单里也没有——这是按目标平台定的默认。

`FC_TRACK_DIRTY_PAGES` 用来覆盖探测结果，读法是 `strconv.ParseBool`
（`deltabox-dev@4af2872c6`，`packages/orchestrator/internal/sandbox/fc/dirtytracking.go:59-103`；
该语义由 `86e4d9610` 引入）：

- 没设 → 跟硬件走（探到能力号 502 则开，否则关）；
- `1/t/T/TRUE/true/True` → 强制开；`0/f/F/FALSE/false/False` → 强制关；
- 设了但解析不了（空串、`yes`、`on`、拼错）→ 忽略该值、同"没设"一样跟硬件走，启动时打一条 WARN
  写明这个值被忽略（`packages/orchestrator/main.go:765-770`）。`86e4d9610` 之前的代码是
  "`"true"` 开，其它一律关"，空串会把 950 上的增量关掉。

三种情形：

| 情形 | `FC_TRACK_DIRTY_PAGES` | 脏页跟踪 | checkpoint |
|---|---|---|---|
| 950（有 HDBSS，目标平台） | 不设 | 自动开，硬件标脏 | 增量 |
| 没有 HDBSS 的机器，不设变量 | 不设 | 关（启动日志有 `dirty page tracking is off` 的 WARN） | 每次全量，功能正确 |
| 我们的 920B 开发环境（无 HDBSS） | 显式 `true` | 开，退化为 KVM 写保护 | 增量 |

950 没有外网，开发只能在 920B 上做；开发栈显式设 `true` 之后，920B 与 950 的差别基本只剩
"增量靠什么实现"，其余路径相同。在其它无 HDBSS 的机器上做开发或预验证，同样是在 nomad job 的
`env {}` 块里加一行字面量 `FC_TRACK_DIRTY_PAGES = "true"`（命令见 `single-node-offline-deploy.md` §4.1）。
`acceptance/checkpoint_verify.py` 里「gB / gC 是增量」那条断言，在无 HDBSS 且未设该变量的机器上不成立，
其余断言不受影响。

| 变量 | main（ext4） | xfs-reflink |
|---|---|---|
| `FC_TRACK_DIRTY_PAGES` | 950 上不用配 —— 不设时探 KVM cap 502，探得到就自动开 | 同左 |
| `ORCHESTRATOR_BASE_PATH` | 不用配 —— 默认 `/orchestrator`，950 根盘就是 ext4 | **已写进仓库的 `template-manager.hcl`**：`"/mnt/xfsdev/orchestrator"`（该分支已归档） |

orchestrator 启动时会打一行 `checkpoint capabilities`，写明脏页跟踪是开是关及原因；关着时另有一条 **WARN**。
XFS 套的 store 不支持 reflink 报 **ERROR** 并指明该怎么改。两种情况都不让部署失败（功能是对的）。详解见
`两个环境变量与已知坑.md`。

HDBSS 的**用哪种方式标脏**仍然不需要任何开关，FC 运行时自己选；
用 `hdbss_evidence.py` 确认是否真用上。

---

## 五、2026-08-24 在 920B 上的验证结果

两套都用 `run-all.sh` 跑的完整一轮（`correctness` → `timing` → `loop`），
用的是 rebase 摘掉 gsd、补了真 busybox、FC 修了 seccomp 之后的二进制。

| | ext4 套 | XFS 套 |
|---|---|---|
| correctness | **ALL PASS** | **ALL PASS** |
| timing | **ALL CORRECT** | **ALL CORRECT** |
| loop（200 次） | **STABLE**，failures 0，p50 0.042s | **STABLE**，failures 0，p50 0.020s |
| store 落点 | `/mnt/ext4dev`（loop ext4） | `e2b-repo/xfsdev`（根盘 XFS） |
| 报告 | `reports/ext4-20260824-161727/` | `reports/xfs-20260824-161025/` |

链深对照（920B，**KVM 写保护，不是 HDBSS**）：

| 深度 | ext4 create p50 | ext4 df ΔMB | XFS create p50 | XFS df ΔMB |
|---|---|---|---|---|
| 5 | 0.090s | 2104 | 0.118s | 2237 |
| 20 | 0.075s | 2322 | 0.145s | 2999 |
| 50 | 0.094s | 2845 | 0.179s | 2136 |

这些数字**不含硬件标脏的收益**，950 上要重测。

---

## 六、2026-08-24 耗时基准（920B，KVM 写保护）

`bench-ckpt.py` 两套各跑一轮完整矩阵。完整解读见 `耗时基准结论.md`，
原始报告在 `reports/bench-{ext4,xfs}-20260824-*/`。摘要：

| 脏页增量 | ext4 创建 p50 | XFS 创建 p50 | ext4 新增物理 | XFS 新增物理 |
|---|---|---|---|---|
| **全量（2GB）** | **1.511 s** | **1.524 s** | 2048 MB | 2048 MB |
| 0 MB | 0.030 s | 0.110 s | 4.6 MB | 36.0 MB |
| 16 MB | 0.042 s | 0.132 s | 23.2 MB | 57.2 MB |
| 32 MB | 0.049 s | 0.143 s | 39.8 MB | 74.0 MB |
| 64 MB | 0.070 s | 0.162 s | 73.0 MB | 107.1 MB |
| 128 MB | 0.111 s | 0.187 s | 139.3 MB | 173.7 MB |
| 256 MB | 0.196 s | 0.277 s | 271.9 MB | 307.1 MB |

restore 两套都在几十毫秒（XFS 更快），内容校验零次不符。

**三条要带到 950 去的结论：**

1. 增量比全量快 8–51 倍，小脏页量上差距最大 —— 这是给客户的主线。
2. **连打 60 秒会平滑劣化**：ext4 ×2.8、XFS ×1.7。单次数字不能外推到长链高频场景，
   机制未查清（链深与累计写入量在现有测法里分不开）。
3. XFS 套每次固定多花约 33 MB 物理空间，已排除测量噪声，机制是假说（`cowextsize`），未验证。

数字**不含 HDBSS 的收益**，950 上要整套重测。

---

## 七、Python SDK（e2b-arm `jll` = `76703933`）

线上协议**一个字节没动**：同样的 49984 端口、同样的 `checkpoint` package、
同样四个 RPC 方法名。改了三件事：

1. **去掉 gsd 命名**。`e2b/gsd/` → `e2b/checkpointd/`，helper 跟着改名，
   文档改写成实际发生的事（宿主侧应答，沙箱里没有守护进程）。
   `is_running()` 保留兼容，新增同义的 `is_available()`。
2. **删掉死的 `Authorization: Basic cm9vdDo=`**。我们的服务用的是
   `e2b-traffic-access-token`，这个 header 没人看。
3. **新增 `mem_mode`**（proto + SDK + orchestrator 三处）。
   `"incremental"` / `"full"`，让调用方能自己发现"悄悄退化成全量拷贝"。
   在此之前只能去宿主磁盘上读 `manifest.json` 才知道。

另外删掉了 `e2b/gsd/e2b.env` —— 它把一个真实 API key 和 access token
打进了包目录里。

pb 是用 `scripts/regen-checkpoint-pb.py` 就地改序列化描述符生成的，不是裸 protoc：
buf 的 managed 模式会往描述符里塞 java/objc/php 等选项，裸 protoc 重编会全丢掉。
已核对描述符差异恰好是两个新字段 + go_package，JSON 在带/不带新字段两种情况下都能正常 round-trip。

### 怎么装

部署时 `install_e2b` 自动调 `dep/e2b-sdk-checkpoint/install.py`，
**排在 `patch_e2b.py` 之前**（顺序反了会被 https→http 的全局替换盖回去）。
手工：`install.py` / `--check` / `--uninstall` / `--force`。

用法文档：`e2b-infra/e2b-deploy/dep/e2b-sdk-checkpoint/使用说明.md`
（部署后在 `/opt/e2b-infra/dep/e2b-sdk-checkpoint/`）。

### 920B 上的现状

- **系统 python3.11**：已装（`--force`，因为那里的 e2b 元数据是 2.21.0 而代码是 2.20.0）
- **conda 环境 `jll-e2b`（python 3.12 / e2b 2.20.0）**：干净安装，**不需要 `--force`** ——
  这就是 950 上的正常路径。装完 `--check` 通过，端到端跑通
  （`mem_mode` full→incremental、restore 内存+磁盘都对、list/delete 正常），
  用它跑 `correctness.py` 也是 **ALL PASS**
- phz 原来那份 SDK 备份在
  `tmp/before-state/site-packages-e2b-before-sdk-overlay.tar.gz`
- 老的 `e2b/gsd/` 被改名成 `e2b/gsd.replaced-by-checkpointd/`（没删），
  `install.py --uninstall` 会挪回去
- 为了生成 pb，往 920B 的系统 python 里 `pip install` 了 `grpcio-tools`（附带 `grpcio`）。
  `protobuf` 没有被动过（装之前之后都是 7.35.1）


---

## 七、2026-08-25 复测（920B，rebase 后的最终二进制）

08-24 之后又改了两处（封存层不等盘、宿主分阶段计时），两套各重跑一整轮 `run-all.sh`，
**这一轮才是当前二进制对应的数据**，报告在 `reports/ext4-20260825-154832/` 与
`reports/xfs-20260825-162413/`（各含 `04-bench.log` 与 `bench/报告.md`）。

| | ext4 套 | XFS 套 |
|---|---|---|
| correctness | ALL CORRECT | ALL CORRECT + ALL PASS |
| loop（200 次） | STABLE，failures 0，p50 **0.035s** | STABLE，failures 0，p50 **0.019s** |
| 全量 2GB 快照 p50 | **1.549 s** | 1.523 s |
| 增量 脏页 0 MB | **0.022 s（快 72×）**，新增物理 4.16 MB | 0.108 s（快 14×），新增物理 35.48 MB |
| 增量 脏页 64 MB | **0.062 s（快 25×）**，新增物理 73.04 MB | 0.171 s（快 9×），新增物理 107.23 MB |
| 增量 脏页 256 MB | **0.183 s（快 8×）**，新增物理 271.93 MB | 0.322 s（快 5×），新增物理 306.87 MB |

XFS 套这轮跑在 loop XFS（`/mnt/xfsdev`）上，绝对耗时偏悲观；ext4 套跑在调优过的 loop ext4 上。
另有 `reports/cow4k-xfs/` 与 `reports/xfs-cow4k/` 两份 `cowextsize` 对照。

## 八、2026-08-27 新增：交付侧验收脚本

`e2b-infra/rollback/scripts/acceptance/` 下两个**单文件、零共享依赖**的脚本，给交付方在 950 上做验收用，
与本目录这套开发态工具箱互补（本目录的脚本共享 `lib.py`，跑得更细，但不适合只拷两个文件过去）：

| 脚本 | 只做一件事 | 已验证 |
|---|---|---|
| `checkpoint_verify.py` | 正确性：三代 × 11 个观测点（含**删除**与**权限位**，只重放写入的假实现骗不过），先自证检查会失败，再用 checkpoint 前起的心跳进程 pid + 启动时间证明是内存回来了而非虚机重启 | 920B ext4 套 kvm-wp：**52/52** |
| `checkpoint_bench.py` | 性能：一条链上全量 + 每档一次增量，再逐级走回；每档拆内存/文件（默认 3:1），各抓"刚写完"与"宿主平静后"两次；宿主内部耗时读服务端 `timings.json` | 920B ext4 套 kvm-wp：**sweep 60/60** |

**已知缺口：这两个脚本目前只打屏、不落盘**，52/52 与 60/60 只存在于 git commit message 里。
下次上机 `tee` 一份，或给它们加 `--out`。
