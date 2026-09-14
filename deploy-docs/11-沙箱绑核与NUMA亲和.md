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
1. **上游没有内建的沙箱绑核功能**。orchestrator 建 cgroup 树时只启用 `+cpu +memory`，
   没有 `cpuset`；全代码库没有任何 `sched_setaffinity` / `taskset` 逻辑。
   SDK 的 `cpu_count` 是 **VM 的 vCPU 数量**，不是绑到哪几个物理核。
2. 但可以从宿主机侧绑，**推荐方案 A**：给 `/sys/fs/cgroup/e2b` 写 `cpuset.cpus` + `cpuset.mems`。
   一条命令、立即生效、覆盖所有现存和新建沙箱、不用改代码也不用重启 orchestrator。
3. **绑之前必须先算大页**：沙箱内存走 hugepages，而大页按 NUMA 节点分配。
   绑到大页不够的节点，沙箱会直接起不来。换算公式见 §3.2。
4. `cpuset` 是"允许集"不是"独占"——它不阻止别人的进程也跑在这些核上。
   真独占要 `isolcpus` 或 cpuset partition，那是要重启/影响全局的操作。

---

## 1. 前提：这套部署里有两棵 cgroup 树

理解绑核（以及内存配额）的一切，都从这张图开始：

```
/sys/fs/cgroup/
├── nomad.slice/share.slice/<allocID>.start.scope    ← Nomad 管的 task cgroup
│      └── orchestrator 主进程（/usr/bin/template-manager）
│         · 受 template-manager.hcl 里 resources{cpu, memory} 约束
│         · 模板构建的解压/写 rootfs/写快照都记在这里
│
└── e2b/                                             ← orchestrator 自己建的沙箱 cgroup 树
    ├── <sandbox-1>/    ← 一个 firecracker 进程
    ├── <sandbox-2>/
    └── ...
          · 与 Nomad 的 cgroup 完全无关，不受 hcl 的 resources 约束
          · 沙箱 VM 内存走 hugepages，连常规 memcg 的账都不走
```

代码依据（`packages/orchestrator/pkg/sandbox/cgroup/manager.go`，2026.09 里是
`internal/sandbox/cgroup/`）：

```go
// RootCgroupPath is the base path for all E2B sandbox cgroups
RootCgroupPath = cgroupV2MountPoint + "/e2b"     // = /sys/fs/cgroup/e2b

func (m *managerImpl) Initialize(ctx context.Context) error {
	os.MkdirAll(RootCgroupPath, 0o755)
	controllersPath := filepath.Join(RootCgroupPath, "cgroup.subtree_control")
	os.WriteFile(controllersPath, []byte("+cpu +memory"), 0o644)   // ← 没有 cpuset
	...
}
```

orchestrator 启动日志里能直接看到这两行：

```
INFO initialized root cgroup   {"path": "/sys/fs/cgroup/e2b"}
INFO cgroup accounting enabled {"root": "/sys/fs/cgroup/e2b"}
```

**这张图能解释三件容易困惑的事：**

| 现象 | 解释 |
|---|---|
| 并发几十个沙箱毫无压力，但构建一个模板就 OOM | 沙箱在 `e2b/` 树下，不占 hcl 里的 `memory` 配额；构建是 orchestrator 自己干的活，全额记在 Nomad task cgroup 上 |
| OOM 日志里 `anon-rss` 只有几十 MB 却被 kill | 吃掉配额的不是进程堆，是 page cache 和 `/mnt/snapshot-cache`（tmpfs）的 shmem 页 |
| Nomad 的 `resources { cores = N }` 绑不住沙箱 | 它设的是 Nomad task cgroup 的 cpuset，沙箱根本不在那棵树里 |

---

## 2. 三个方案对比

| | 方案 A：cgroup cpuset | 方案 B：taskset 包 orchestrator | 方案 C：Nomad `cores` |
|---|---|---|---|
| 作用对象 | **只有沙箱** | orchestrator + 沙箱（子进程继承 affinity） | 只有 orchestrator |
| 对沙箱是否有效 | ✅ | ✅（继承） | ❌ **无效** |
| 是否拖慢模板构建 | 否 | **是**（构建也被限制） | — |
| 生效方式 | 写文件，立即生效 | 改 hcl + `build.sh -r` 重启 job | 改 hcl，且与 `cpu` 互斥 |
| 持久化 | 需要 systemd unit（见 §3.5） | 跟 job spec 走，可进仓库 | 跟 job spec 走 |

**默认用方案 A**：它精确命中"只想限制沙箱"这个需求，不牺牲构建速度，也不需要重启任何服务。

---

## 3. 方案 A：给 e2b cgroup 树设 cpuset

cpuset 在 cgroup v2 里是**层级继承**的，所以只要给 `/sys/fs/cgroup/e2b` 这个根节点设一次，
整棵子树——包括已经在跑的沙箱和以后新建的——全部受约束。

### 3.1 前置检查

```bash
# ① 根 cgroup 是否启用了 cpuset 控制器（多数发行版默认已启用）
grep -o cpuset /sys/fs/cgroup/cgroup.subtree_control \
  || echo "+cpuset" > /sys/fs/cgroup/cgroup.subtree_control

# ② e2b 树是否存在（orchestrator 启动后才有）+ 当前是否受限
ls /sys/fs/cgroup/e2b/cpuset.cpus
cat /sys/fs/cgroup/e2b/cpuset.cpus.effective     # 空文件 = 继承父 = 不受限
cat /sys/fs/cgroup/e2b/cpuset.mems.effective

# ③ CPU / NUMA 拓扑
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

# 备份，方便回退
{ echo "cpus=$(cat /sys/fs/cgroup/e2b/cpuset.cpus)"
  echo "mems=$(cat /sys/fs/cgroup/e2b/cpuset.mems)"; } > /root/e2b-cpuset.bak

# CPU 和内存节点必须成对设置
echo "$CPUS" > /sys/fs/cgroup/e2b/cpuset.cpus
echo "$MEMS" > /sys/fs/cgroup/e2b/cpuset.mems
```

> ⚠️ **在没有沙箱在跑的时候做。** `cpuset.cpus` 改了会立刻把进程迁到允许的核上（安全）；
> 但 `cpuset.mems` 改了**不迁移已分配的内存**，只影响新分配——正在跑的沙箱会变成
> "CPU 在新节点、大页还在老节点"的跨 NUMA 状态，比不绑还慢。

**到这一步绑核就已经生效了**，不需要再做任何事。临时用途（压测、临时隔离）做完
按 §3.6 回退即可，别急着上 §3.5 的开机固化。

### 3.4 验证

```bash
cat /sys/fs/cgroup/e2b/cpuset.cpus.effective      # 应等于 $CPUS
cat /sys/fs/cgroup/e2b/cpuset.mems.effective      # 应等于 $MEMS

# 起几个沙箱后，看 firecracker 实际落在哪
for p in $(pgrep -f '/fc-versions/.*/firecracker'); do
  echo "pid $p  affinity=$(taskset -pc $p 2>/dev/null | sed 's/.*: //')  当前CPU=$(ps -o psr= -p $p | tr -d ' ')"
done

# 子 cgroup 是否继承
for d in /sys/fs/cgroup/e2b/*/; do
  [ -f "$d/cpuset.cpus.effective" ] && echo "$(basename $d): $(cat $d/cpuset.cpus.effective)"
done

# 大页只应从绑定的节点消耗，其余节点 free 纹丝不动
for n in $(ls -d /sys/devices/system/node/node[0-9]* | sed 's/.*node//'); do
  echo "node$n free_hugepages=$(cat /sys/devices/system/node/node$n/hugepages/hugepages-2048kB/free_hugepages)"
done
```

### 3.5 持久化（可选，多数情况不需要）

**先判断你要不要这一节。** 绑核在这套部署里是临时手段：默认不绑、全核可用是常态，
只有压测取数或临时隔离时才绑，做完就该回退。这类用途**不需要持久化**——
`/sys/fs/cgroup/e2b` 由 orchestrator 启动时创建，机器重启或 `build.sh -d` 之后会重建，
cpuset 设置随之丢失，而这恰好就是你想要的结果：**自动回到不绑核的默认状态**。

只有当"这台机器长期只给 e2b 用固定几个 NUMA 节点"成为一项既定约束时，才需要固化。
那种情况下用一个带等待的 oneshot 服务：

```bash
cat > /etc/systemd/system/e2b-cpuset.service <<'EOF'
[Unit]
Description=Pin e2b sandboxes to NUMA nodes (cpuset on /sys/fs/cgroup/e2b)
After=nomad.service
Wants=nomad.service

[Service]
Type=oneshot
RemainAfterExit=yes
Environment=E2B_CPUS=0-191
Environment=E2B_MEMS=0-1
# /sys/fs/cgroup/e2b 由 orchestrator 启动时创建，最多等 5 分钟
ExecStart=/bin/bash -c 'for i in $(seq 1 150); do [ -f /sys/fs/cgroup/e2b/cpuset.cpus ] && break; sleep 2; done; \
  [ -f /sys/fs/cgroup/e2b/cpuset.cpus ] || { echo "e2b cgroup 未出现，跳过"; exit 0; }; \
  echo "${E2B_CPUS}" > /sys/fs/cgroup/e2b/cpuset.cpus; \
  echo "${E2B_MEMS}" > /sys/fs/cgroup/e2b/cpuset.mems; \
  echo "sandboxes pinned: cpus=$(cat /sys/fs/cgroup/e2b/cpuset.cpus.effective) mems=$(cat /sys/fs/cgroup/e2b/cpuset.mems.effective)"'

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now e2b-cpuset.service
systemctl status e2b-cpuset.service --no-pager | tail -5
```

`exit 0` 而不是报错退出是刻意的：绑核失败不该让开机流程挂掉。
改绑定范围只需改 `Environment=` 两行再 `daemon-reload && systemctl restart e2b-cpuset`。

**代价：每次 `build.sh -d` 之后要重跑一次**（cgroup 被重建了）：

```bash
systemctl restart e2b-cpuset.service
```

这条维护负担是持久化的固有成本——忘了重跑就会得到一个"以为绑着、实际没绑"的状态，
比不绑更难排查。不确定要不要长期绑核时，宁可不装这个服务，每次手动执行 §3.3 那两行。

### 3.6 回退

```bash
echo "" > /sys/fs/cgroup/e2b/cpuset.cpus    # 空值 = 继承父节点 = 不限制
echo "" > /sys/fs/cgroup/e2b/cpuset.mems
systemctl disable --now e2b-cpuset.service
cat /sys/fs/cgroup/e2b/cpuset.cpus.effective   # 应回到全部 CPU
```

---

## 4. 方案 B：taskset 包住 orchestrator

firecracker 是 orchestrator fork 出来的，**CPU affinity mask 会被子进程继承**，
所以给主进程绑核，沙箱自动跟着。改 `nomad/template-manager.hcl` 的 args：

```hcl
config {
  command = "/bin/bash"
  args    = ["-c", " chmod +x /usr/bin/template-manager && taskset -c 0-191 /usr/bin/template-manager --port 5008"]
}
```

```bash
bash /opt/e2b-infra/build.sh -r template-manager
```

**代价**：orchestrator 自己——包括模板构建那些重活——也一起被限制。
只想限制沙箱、不想拖慢构建就别用它。
好处是跟着 job spec 走，改 `e2b-deploy/dep/template-manager.hcl` 就能进仓库持久化。

---

## 5. 方案 C：Nomad 的 `cores` —— 对沙箱无效

Nomad 支持 `resources { cores = 8 }` 分配独占 cpuset，看起来正对口，但按 §1 那张图，
它设的是 `/nomad.slice/share.slice/<allocID>.start.scope` 的 cpuset，
**沙箱在 `/sys/fs/cgroup/e2b` 那棵树里，约束不到**。
而且 `cores` 与 `cpu` 互斥，改了还会影响调度语义。别用。

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
| 别让沙箱吃满整机、影响同事 | 方案 A，圈一块够用的地 |
| 压测要稳定可复现的数字 | 方案 A + NUMA 对齐，必要时再谈 `isolcpus` |
| 构建和沙箱都要限制 | 方案 B |
| 追求最大吞吐 | **别绑**，绑核在这个目标下是减配 |
| 日常使用、没有特殊诉求 | **别绑**，这是默认状态；已经绑了就按 §3.6 回退 |

---

## 7. 想做"每个沙箱绑不同核"？需要改代码

比如"沙箱 A 用 0-7、沙箱 B 用 8-15"这种粒度，上游没有接口，SDK 也没有对应参数。
要做得改 orchestrator：在 `cgroup.Initialize()` 的 `subtree_control` 里加 `+cpuset`，
再在 `cgroup.Create()` 之后按调度策略给每个沙箱的 cgroup 写 `cpuset.cpus`。
改动不算大，但属于功能开发，不是配置调整。

---

## 8. 参考

| 位置 | 内容 |
|---|---|
| `packages/orchestrator/.../sandbox/cgroup/manager.go` | `RootCgroupPath = /sys/fs/cgroup/e2b`、`Initialize()` 只启用 `+cpu +memory` |
| `e2b-deploy/dep/template-manager.hcl` | `resources { cpu, memory }`——管的是 orchestrator 主进程（含模板构建），**不管沙箱** |
| `e2b-deploy/dep/start-client.sh` | 大页静态池与 overcommit 池的分配逻辑（`nr_hugepages` / `nr_overcommit_hugepages`） |
| [`06-日常运维手册.md`](06-日常运维手册.md) §8.1 | 沙箱基础设施巡检（nbd、大页、netns） |
| [`01-整体架构与组件总览.md`](01-整体架构与组件总览.md) §3.4 | 沙箱层各组件构成 |
