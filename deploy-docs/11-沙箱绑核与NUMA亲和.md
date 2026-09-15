# 11 沙箱绑核与 NUMA 亲和

> 起因：多核大内存机器上想把沙箱限制到指定 CPU——要么避免沙箱把整机吃满影响同机的其他人，
> 要么压测时要一份不受跨 NUMA 抖动干扰的稳定数字。
>
> 配套：[`01-整体架构与组件总览.md`](01-整体架构与组件总览.md)（沙箱层构成）、
> [`06-日常运维手册.md`](06-日常运维手册.md) §8 宿主机专项检查。

---

## 0. 结论速查

0. **绑核是临时手段，不是常态配置。** 默认不绑——让沙箱用满整机所有核，
   才是这套部署的正常状态。只有压测要稳定数字、或临时需要和同机其他负载隔离时才绑，
   **用完按 §3.6 回退**。§3.5 的开机固化是可选项，只在长期需要绑核时才做。
1. **上游没有内建的沙箱绑核功能**。全代码库没有任何 `sched_setaffinity` / `taskset` 逻辑。
   SDK 的 `cpu_count` 是 **VM 的 vCPU 数量**，不是绑到哪几个物理核。
2. **firecracker 进程和 orchestrator 在同一个 cgroup 里**——Nomad 的 task cgroup。
   `/sys/fs/cgroup/e2b/sbx-<id>` 那些目录是空的（§1）。所以从宿主机侧绑，绑的对象
   只能是"orchestrator + 全部沙箱"这一整组，分不开；想按沙箱单独设要改代码（§7）。
3. **推荐方案 A**：给 Nomad task scope 写 `cpuset.cpus` + `cpuset.mems`。
   一条命令、立即生效、覆盖所有现存和新建沙箱、不改代码不重启。代价是模板构建也被限制。
4. **绑之前必须先算大页**：沙箱内存走 hugepages，而大页按 NUMA 节点分配。
   绑到大页不够的节点，沙箱会直接起不来。换算公式见 §3.2。
5. `cpuset` 是"允许集"不是"独占"——它不阻止别人的进程也跑在这些核上。
   真独占要 `isolcpus` 或 cpuset partition，那是要重启/影响全局的操作。

---

## 1. 前提：沙箱在哪个 cgroup 里

理解绑核（以及内存配额）的一切，都从这张图开始。注意它和上游的行为**不一样**：

```
/sys/fs/cgroup/
├── nomad.slice/share.slice/<allocID>.start.scope    ← Nomad 管的 task cgroup
│      ├── orchestrator 主进程（/usr/bin/template-manager）
│      │     · 受 template-manager.hcl 里 resources{cpu, memory} 约束
│      │     · 模板构建的解压/写 rootfs/写 memfile 都记在这里
│      └── 每个沙箱的 firecracker 进程（含构建期的 provision 沙箱）
│            · 子进程默认继承父进程的 cgroup，orchestrator 没有把它挪走
│            · guest 内存走 hugetlb，不进 memory.max 的账（见 12 篇 §1）
│            · **cpuset 对它有效**——这就是绑核的落点
│
└── e2b/sbx-<id>/                                    ← orchestrator 自建，但里面没有进程
```

代码依据（`packages/orchestrator/internal/sandbox/`）：

- `cgroup/manager.go`：`RootCgroupPath = /sys/fs/cgroup/e2b`，启动时 mkdir 并启用
  `+cpu +memory`（没有 `cpuset`），每个沙箱再 mkdir 一个 `sbx-<id>`——**目录确实会建**，
  启动日志也会打印 `initialized root cgroup {"path": "/sys/fs/cgroup/e2b"}`。
- `fc/process.go` `configure()`：上游在这里用 `SysProcAttr.UseCgroupFD/CgroupFD`
  （`CLONE_INTO_CGROUP`）把 firecracker 放进 `sbx-<id>`。**ARM 移植补丁（`fbee6fcd1`）
  把这几行整段注释掉了**：

  ```go
  //if cgroupFD != cgroup.NoCgroupFD {
  //	p.cmd.SysProcAttr.UseCgroupFD = true
  //	p.cmd.SysProcAttr.CgroupFD = cgroupFD
  //}
  ```

  于是 firecracker 留在父进程所在的 cgroup，也就是 Nomad task scope。

在机器上一眼验证：

```bash
cat /proc/$(pgrep -n firecracker)/cgroup        # 0::/nomad.slice/share.slice/<alloc>.start.scope
cat /sys/fs/cgroup/e2b/sbx-*/cgroup.procs        # 全空
```

### 1.1 cgroup v1 还是 v2？ARM 移植改了什么

先判断机器用的是哪个版本：

```bash
test -f /sys/fs/cgroup/cgroup.controllers && echo "cgroup v2" || echo "cgroup v1"
# v2：/sys/fs/cgroup 是 cgroup2 挂载，一棵树，控制器以文件形式出现在每个目录里
# v1：/sys/fs/cgroup 是只读 tmpfs，下面 cpu/ memory/ cpuset/ 各是独立挂载
```

openEuler 24.03 默认装出来是 v1，是否切成 v2 取决于装机时的内核参数
（`systemd.unified_cgroup_hierarchy=1`）。**同一套部署包在两种机器上都能跑**，
这是 ARM 移植补丁（`fbee6fcd1`）在 `packages/orchestrator/internal/sandbox/` 里改了两处的结果：

| 改动 | 上游行为 | 补丁后 |
|---|---|---|
| ① `cgroup/manager.go` `NewManager()` / `Initialize()` | 找不到 `cgroup.controllers`（即 v1）就返回错误，`main.go` 里 `Fatal`，**orchestrator 起不来** | v1 时打一行 `cgroup v1 detected, skipping initialization`，跳过建 `/sys/fs/cgroup/e2b`，继续跑 |
| ② `fc/process.go` `configure()` | `CLONE_INTO_CGROUP` 把 firecracker 放进 `sbx-<id>` | 整段注释掉，**不分 v1 v2**，firecracker 留在父进程的 cgroup |

于是两种机器上的实际状态是：

| | cgroup v1 | cgroup v2 |
|---|---|---|
| orchestrator 能否启动 | 能（上游不能） | 能 |
| `/sys/fs/cgroup/e2b` | 不建 | 建了，`sbx-<id>` 也建，但都是空目录 |
| firecracker 所在 cgroup | 和 orchestrator 一起，在 Nomad 的 task cgroup 里 | 同左 |
| 沙箱级 cgroup 统计（`hoststats` 的 `CgroupMemoryUsage` 等） | 无 | 恒为 0 |
| 按沙箱单独限核 / 限内存 | 不行 | 不行（要改代码，§7） |
| 整组（orchestrator + 全部沙箱）限核 / 限内存 | 可以，但路径是 v1 的（本篇不覆盖） | 可以，本篇 §3–§5 |

注意改动 ① 已经足够让 v1 跑起来（v1 上 `Create()` 建目录会失败并退化成不带 cgroup FD 启动），
改动 ② 是额外多砍的一刀，把 v2 机器上本来能用的沙箱级 cgroup 也一并砍掉了。
补丁提交信息没有写原因。要找回来见 §7。

**这张图能解释三件容易困惑的事：**

| 现象 | 解释 |
|---|---|
| 并发几十个沙箱毫无压力，但构建一个模板就 OOM | 沙箱 guest 内存走 hugetlb，不进 `memory.max` 的账；构建产生的 page cache 才记在这里（12 篇） |
| `/sys/fs/cgroup/e2b/sbx-*/memory.current` 全是 0 | 目录里没有进程；orchestrator 上报的 per-sandbox cgroup 统计在这套部署里恒为 0 |
| 给 `/sys/fs/cgroup/e2b` 写 cpuset 没有任何效果 | 同上，那棵树是空的；要写就写 Nomad task scope |

---

## 2. 三个方案对比

三个方案的作用对象**都是"orchestrator + 全部沙箱"**，因为它们在同一个 cgroup 里、
又是父子进程。区别只在生效方式和能不能选 NUMA 节点：

| | 方案 A：task scope 写 cpuset | 方案 B：numactl 包 orchestrator | 方案 C：Nomad `cores` |
|---|---|---|---|
| 作用对象 | orchestrator + 沙箱 | orchestrator + 沙箱（子进程继承） | orchestrator + 沙箱 |
| 能选核、选 NUMA 节点 | ✅ 自己填 | ✅ 自己填 | ❌ 核由 Nomad 挑，没有 mems |
| 生效方式 | 写文件，立即生效，不重启 | 改 hcl + `build.sh -r` 重启 job | 改 hcl + 重启，且与 `cpu` 互斥 |
| 持久化 | alloc 重建就丢；要 systemd unit（§3.5） | 跟 job spec 走，可进仓库 | 跟 job spec 走 |
| 拖慢模板构建 | 是 | 是 | 是 |

**默认用方案 A**：临时压测、临时隔离最省事。要长期固定就用方案 B 进仓库。
方案 C 能用但控制不了落在哪个节点，见 §5。

---

## 3. 方案 A：给 Nomad task scope 设 cpuset

cpuset 对 cgroup 里的所有进程生效，改了立刻迁移。firecracker 和 orchestrator 都在
`/sys/fs/cgroup/nomad.slice/share.slice/<allocID>.start.scope` 里，给这个 scope 写一次，
已经在跑的沙箱和以后新建的全部受约束。

> 只适用于 cgroup v2 的机器。cgroup v1 的路径和文件都不同，本篇不覆盖；
> 怎么判断、v1 上会怎样，见 §1.1。

### 3.1 前置检查

```bash
# ① 找到 scope（alloc 每次重建 ID 会变，别写死）
SCOPE=$(ls -d /sys/fs/cgroup/nomad.slice/share.slice/*.start.scope | head -1)
echo "$SCOPE"
grep -c firecracker <(for p in $(cat $SCOPE/cgroup.procs); do cat /proc/$p/comm; done)   # 沙箱数

# ② cpuset 控制器是否一路启用到 scope 上（Nomad 在 v2 上自己会开；没开就补）
for d in /sys/fs/cgroup /sys/fs/cgroup/nomad.slice /sys/fs/cgroup/nomad.slice/share.slice; do
  grep -qw cpuset $d/cgroup.subtree_control || echo "+cpuset" > $d/cgroup.subtree_control
done
ls $SCOPE/cpuset.cpus                              # 有这个文件才能往下走

# ③ 当前是否受限
cat $SCOPE/cpuset.cpus.effective                   # 等于全部 CPU = 不受限
cat $SCOPE/cpuset.mems.effective

# ④ CPU / NUMA 拓扑
lscpu | grep -E '^CPU\(s\)|NUMA'
numactl -H | grep -E 'node [0-9]+ (cpus|size|free)|node distances' -A5
```

`numactl -H` 末尾的 **node distances 矩阵**决定该把哪几个节点凑成一组：
数值越小越近，同组内绑才有意义。

### 3.2 ★ 算大页：这一步决定能绑几个节点

沙箱 VM 内存走 hugepages，**大页是按 NUMA 节点分配的**。绑到大页不足的节点，
沙箱直接起不来。先看清楚每个节点有多少：

```bash
for n in $(ls -d /sys/devices/system/node/node[0-9]* | sed 's/.*node//'); do
  cpus=$(cat /sys/devices/system/node/node$n/cpulist)
  line="node$n  cpus=$cpus"
  for hp in /sys/devices/system/node/node$n/hugepages/hugepages-*; do
    sz=$(basename "$hp" | sed 's/hugepages-//')
    t=$(cat "$hp/nr_hugepages"); f=$(cat "$hp/free_hugepages")
    [ "$t" -gt 0 ] && line="$line | ${sz}: total=$t free=$f"
  done
  echo "$line"
done
```

**换算公式**（2 MB 大页）：

```
单个沙箱需要的大页数 = 沙箱内存(MiB) / 2
可支撑并发数        = 该组节点的 free_hugepages 之和 / 单沙箱大页数
```

一台实测机器的例子（384 核 / 4 节点，每节点 13942 个 2 MB 大页 ≈ 27 GiB）：

| 绑定范围 | CPU | 可用大页 | 1 GiB 沙箱的并发上限 |
|---|---|---|---|
| 单个节点 | 96 核 | ≈ 13900 | **约 27 个** |
| 两个近邻节点 | 192 核 | ≈ 27700 | **约 54 个** |
| 不绑（全部 4 节点） | 384 核 | ≈ 55700 | 约 108 个 |

这台机器上原本能跑 30 并发，**绑单个节点会直接跑不起来**（30 × 512 = 15360 > 13900）——
这就是为什么必须先算再绑。

> `nr_hugepages`（静态池）不是硬上限：`start-client.sh` 还设了 `nr_overcommit_hugepages`，
> 运行时可以动态追加。但动态凑大页需要**连续物理内存**，机器跑久了内存碎片化就会失败。
> 做容量规划按静态池算，别指望 overcommit 兜底。

### 3.3 设置

```bash
# 按 §3.2 的结论填：CPUS 是核心列表，MEMS 是对应的 NUMA 节点号
CPUS="0-191"
MEMS="0-1"

SCOPE=$(ls -d /sys/fs/cgroup/nomad.slice/share.slice/*.start.scope | head -1)

# 备份，方便回退
{ echo "cpus=$(cat $SCOPE/cpuset.cpus)"
  echo "mems=$(cat $SCOPE/cpuset.mems)"; } > /root/e2b-cpuset.bak

# CPU 和内存节点必须成对设置
echo "$CPUS" > $SCOPE/cpuset.cpus
echo "$MEMS" > $SCOPE/cpuset.mems
```

> ⚠️ **在没有沙箱在跑的时候做。** `cpuset.cpus` 改了会立刻把进程迁到允许的核上（安全）；
> 但 `cpuset.mems` 改了**不迁移已分配的内存**，只影响新分配——正在跑的沙箱会变成
> "CPU 在新节点、大页还在老节点"的跨 NUMA 状态，比不绑还慢。
>
> 顺带：orchestrator 自己也被一起迁走了，正在进行的模板构建会变慢。

**到这一步绑核就已经生效了**，不需要再做任何事。临时用途（压测、临时隔离）做完
按 §3.6 回退即可，别急着上 §3.5 的开机固化。

### 3.4 验证

```bash
SCOPE=$(ls -d /sys/fs/cgroup/nomad.slice/share.slice/*.start.scope | head -1)
cat $SCOPE/cpuset.cpus.effective      # 应等于 $CPUS
cat $SCOPE/cpuset.mems.effective      # 应等于 $MEMS

# 起几个沙箱后，看 firecracker 实际落在哪（这一步才是真相，别只看 cgroup 文件）
for p in $(pgrep -f '/fc-versions/.*/firecracker'); do
  echo "pid $p  cgroup=$(cut -d: -f3 /proc/$p/cgroup)  affinity=$(taskset -pc $p 2>/dev/null | sed 's/.*: //')  当前CPU=$(ps -o psr= -p $p | tr -d ' ')"
done

# 大页只应从绑定的节点消耗，其余节点 free 纹丝不动
for n in $(ls -d /sys/devices/system/node/node[0-9]* | sed 's/.*node//'); do
  echo "node$n free_hugepages=$(cat /sys/devices/system/node/node$n/hugepages/hugepages-2048kB/free_hugepages)"
done
```

### 3.5 持久化（可选，多数情况不需要）

**先判断你要不要这一节。** 绑核在这套部署里是临时手段：默认不绑、全核可用是常态，
只有压测取数或临时隔离时才绑，做完就该回退。这类用途**不需要持久化**——
task scope 由 Nomad 在 alloc 启动时创建，机器重启、`build.sh -d`、`build.sh -r` 之后
都是新的 scope（ID 也变），cpuset 设置随之丢失，而这恰好就是你想要的结果：
**自动回到不绑核的默认状态**。

只有当"这台机器长期只给 e2b 用固定几个 NUMA 节点"成为一项既定约束时，才需要固化。
那种情况下**优先用方案 B**（§4，跟 job spec 走，天然持久）。坚持用方案 A 的话，
用一个带等待的 oneshot 服务：

```bash
cat > /etc/systemd/system/e2b-cpuset.service <<'EOF'
[Unit]
Description=Pin e2b orchestrator+sandboxes to NUMA nodes (cpuset on the Nomad task scope)
After=nomad.service
Wants=nomad.service

[Service]
Type=oneshot
RemainAfterExit=yes
Environment=E2B_CPUS=0-191
Environment=E2B_MEMS=0-1
# task scope 由 Nomad 拉起 alloc 时创建，最多等 5 分钟
ExecStart=/bin/bash -c 'for i in $(seq 1 150); do S=$(ls -d /sys/fs/cgroup/nomad.slice/share.slice/*.start.scope 2>/dev/null | head -1); [ -n "$S" ] && [ -f "$S/cpuset.cpus" ] && break; sleep 2; done; \
  [ -n "$S" ] && [ -f "$S/cpuset.cpus" ] || { echo "task scope 未出现，跳过"; exit 0; }; \
  echo "${E2B_CPUS}" > $S/cpuset.cpus; \
  echo "${E2B_MEMS}" > $S/cpuset.mems; \
  echo "pinned $S: cpus=$(cat $S/cpuset.cpus.effective) mems=$(cat $S/cpuset.mems.effective)"'

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now e2b-cpuset.service
systemctl status e2b-cpuset.service --no-pager | tail -5
```

`exit 0` 而不是报错退出是刻意的：绑核失败不该让开机流程挂掉。
改绑定范围只需改 `Environment=` 两行再 `daemon-reload && systemctl restart e2b-cpuset`。

**代价：每次 `build.sh -d` / `build.sh -r template-manager` 之后要重跑一次**（scope 被重建了）：

```bash
systemctl restart e2b-cpuset.service
```

这条维护负担是持久化的固有成本——忘了重跑就会得到一个"以为绑着、实际没绑"的状态，
比不绑更难排查。不确定要不要长期绑核时，宁可不装这个服务，每次手动执行 §3.3 那两行。

### 3.6 回退

```bash
SCOPE=$(ls -d /sys/fs/cgroup/nomad.slice/share.slice/*.start.scope | head -1)
echo "" > $SCOPE/cpuset.cpus    # 空值 = 继承父节点 = 不限制
echo "" > $SCOPE/cpuset.mems
systemctl disable --now e2b-cpuset.service 2>/dev/null
cat $SCOPE/cpuset.cpus.effective   # 应回到全部 CPU
```

---

## 4. 方案 B：numactl 包住 orchestrator

firecracker 是 orchestrator fork 出来的，**CPU affinity 和 NUMA 内存策略都会被子进程继承**，
所以给主进程绑，沙箱自动跟着。用 `numactl` 而不是 `taskset`：后者只管 CPU，
不管内存节点，会踩 §6 ② 的坑。改 `template-manager.hcl` 的 args：

```hcl
config {
  command = "/bin/bash"
  args    = ["-c", " chmod +x /usr/bin/template-manager && numactl --cpunodebind=0-1 --membind=0-1 /usr/bin/template-manager --port ${TEMPLATE_MANAGER_PORT}"]
}
```

```bash
bash /opt/e2b-infra/build.sh -r template-manager
```

`--membind` 对大页同样有效（hugetlb 分配遵守进程的 mempolicy），所以 §3.2 的大页核算
照样要做。效果与方案 A 相同（都是 orchestrator + 全部沙箱一起绑），区别是它跟着 job spec 走，
改 `e2b-deploy/dep/template-manager.hcl` 就能进仓库持久化，alloc 重建也不会丢。
长期绑核选它，临时绑核选方案 A。

---

## 5. 方案 C：Nomad 的 `cores` —— 能绑，但选不了节点

Nomad 支持 `resources { cores = N }` 给任务分配独占 cpuset。按 §1，沙箱就在 Nomad 的
task cgroup 里，所以它**对沙箱有效**（早先版本的本篇写成"无效"，是基于 firecracker 在
另一棵树的错误前提）。但不推荐：

- 用哪几个核由 Nomad 挑，不能指定，也不写 `cpuset.mems`——绑 CPU 不绑内存节点，正是 §6 ② 的坑；
- `cores` 与 `cpu` 互斥，改了还会影响调度语义；scope 会从 `share.slice` 挪到 `reserve.slice`，
  §3 的路径全变。

要绑就用 A 或 B。

---

## 6. 三个必须知道的前提

**① cpuset 是"允许集"，不是"独占"。**
写 `cpuset.cpus = 0-191` 只是说沙箱**只能**跑在这些核上，
并**不阻止**同机其他人的进程也跑上去。要真独占得上内核参数 `isolcpus=`
或 cgroup v2 的 partition 模式（`echo root > cpuset.cpus.partition`），
两者都要重启或影响全局，共享机器上动之前先和同事商量。

**② 绑 CPU 必须同时绑内存节点。**
只设 `cpuset.cpus` 不设 `cpuset.mems`，沙箱可能 CPU 在 node0、大页在 node2，
每次访存都跨 NUMA——绑核反而把性能绑没了。两者永远成对设置。

**③ 绑核会压低并发上限。**
按 §3.2 的换算，绑定范围越小并发上限越低。先想清楚目标：

| 目标 | 建议 |
|---|---|
| 别让沙箱吃满整机、影响同事 | 方案 A（临时）或 B（长期），圈一块够用的地 |
| 压测要稳定可复现的数字 | 方案 A + NUMA 对齐，必要时再谈 `isolcpus` |
| 只限制沙箱、不限制构建 | 现在做不到，见 §7 |
| 追求最大吞吐 | **别绑**，绑核在这个目标下是减配 |
| 日常使用、没有特殊诉求 | **别绑**，这是默认状态；已经绑了就按 §3.6 回退 |

---

## 7. 想按沙箱单独绑、或只绑沙箱不绑构建？需要改代码

上面三个方案都是"orchestrator + 全部沙箱"一锅端，根源是 firecracker 没被放进自己的
cgroup。要做到"沙箱 A 用 0-7、沙箱 B 用 8-15"或"只限沙箱不限构建"，得把上游的
`CLONE_INTO_CGROUP` 找回来。改动在 `packages/orchestrator/internal/sandbox/`，三处：

1. `fc/process.go` `configure()`：把注释掉的那段恢复。**必须加 cgroup v2 判断**——
   ARM 移植把它注释掉，最可能就是为了在 cgroup v1 的机器上跑（§1.1）；
   v1 上 `/sys/fs/cgroup` 是只读 tmpfs，`Create()` 本来就会失败并返回 `NoCgroupFD`，
   所以恢复后用 `cgroupFD != NoCgroupFD` 这个既有条件就够了，但要在 v1 机器上实测一遍。
2. `cgroup/manager.go` `Initialize()`：`subtree_control` 加 `+cpuset`。
3. `cgroup/manager.go` `Create()` 之后：按调度策略给 `sbx-<id>` 写 `cpuset.cpus/mems`；
   或者不改这里，只靠 1 和 2，此时本篇 **原来的做法**（给 `/sys/fs/cgroup/e2b` 写一次、
   靠层级继承覆盖全部沙箱）就成立了，而且构建不受影响。

顺带的收益：上游 per-sandbox 的 cgroup 统计（`hoststats` 里的 `CgroupMemoryUsage` 等）
在这套部署里恒为 0，恢复后就有数了。

这是功能开发，不是配置调整，需要重新出包。改完要在 cgroup v1 和 v2 的机器上各起一次沙箱确认。

---

## 8. 参考

| 位置 | 内容 |
|---|---|
| `packages/orchestrator/internal/sandbox/cgroup/manager.go` | `RootCgroupPath = /sys/fs/cgroup/e2b`、`Initialize()` 只启用 `+cpu +memory`；目录建了但没进程 |
| `packages/orchestrator/internal/sandbox/fc/process.go` | `configure()` 里 `CLONE_INTO_CGROUP` 被 ARM 移植补丁注释掉，firecracker 留在 Nomad task scope |
| `e2b-deploy/dep/template-manager.hcl` | `resources { cpu, memory }`——管 orchestrator + 沙箱进程所在的 cgroup；沙箱 guest 内存因 hugetlb 不记账（12 篇） |
| `e2b-deploy/dep/start-client.sh` | 大页静态池与 overcommit 池的分配逻辑（`nr_hugepages` / `nr_overcommit_hugepages`） |
| [`06-日常运维手册.md`](06-日常运维手册.md) §8.1 | 沙箱基础设施巡检（nbd、大页、netns） |
| [`01-整体架构与组件总览.md`](01-整体架构与组件总览.md) §3.4 | 沙箱层各组件构成 |
