# 12 orchestrator 资源配额调优（memory / cpu）

> 起因：构建一个 200 MB 的基础镜像时 orchestrator 被 OOM kill，而现象出在 api
> ——部署卡在 api 的 deployment 上 10 分钟超时失败，根因离现象很远。
>
> 本篇讲 `template-manager.hcl` 里 `resources { memory, cpu }` 到底管什么、
> 怎么量出该给多少、改哪几个文件、怎么验证生效。
>
> 配套：[`11-沙箱绑核与NUMA亲和.md`](11-沙箱绑核与NUMA亲和.md)（同一套 cgroup 结构，
> 讲 CPU 侧）、[`06-日常运维手册.md`](06-日常运维手册.md) §2 Nomad 操作。

---

## 0. 结论速查

1. firecracker 进程**就在这个 cgroup 里**（arm64 补丁把 CLONE_INTO_CGROUP 注释掉了，
   `/sys/fs/cgroup/e2b` 只剩空目录，§1）；但**客户机内存走 hugetlb，不记 memory 控制器**，
   所以这个值既不是"只管 orchestrator"，也不是"沙箱数 × 沙箱内存"的预算。
   "并发几十个沙箱毫无压力"仍然不能说明这个值够用——沙箱内存根本不在账上。
2. 撑爆它的通常不是进程堆，而是构建/快照产生的 page cache（脏页回写完才可回收）。
   所以 OOM 现场的 `anon-rss` 可能只有几十 MB，看着像"没吃内存却被杀"。
3. 现值 **262144 MiB（256 GiB）**（8192 → 32768 → 262144，2026-09）。要改，**必须改三个地方**（§3），
   只改运行态会被下次 `build.sh -i` 覆盖回去。
4. 这个上限的意义是"跑飞了别把整台机器拖垮"。1.1 TB 的机器上 256 GiB 仍是护栏；
   换到小内存机器要按实测峰值重新取（§2）。

---

## 1. 它管什么、不管什么

看起来有两棵树，实际只有一棵在用：

```
/sys/fs/cgroup/
├── nomad.slice/share.slice/<allocID>.start.scope   ← template-manager.hcl 的 resources 管这里
│      ├── orchestrator 主进程（/usr/bin/template-manager）
│      │     · 模板构建：解压镜像层、拼 rootfs、写 memfile / 差分文件
│      │     · 这些动作产生的 page cache 记在这个 cgroup 账上
│      └── 每个沙箱的 firecracker 进程（含构建期的 provision 沙箱）
│            · 子进程默认继承父进程的 cgroup，orchestrator 没有把它挪走
│            · guest 内存是 MAP_HUGETLB 映射，只记 hugetlb 控制器，**不进 memory.max 的账**
│
└── e2b/sbx-<id>/                                   ← orchestrator 自建，但里面没有进程
```

代码依据：

- `packages/orchestrator/internal/sandbox/cgroup/manager.go`：`RootCgroupPath = /sys/fs/cgroup/e2b`，
  启动时 mkdir 并打印 `initialized root cgroup`，每个沙箱再 mkdir 一个 `sbx-<id>`——目录确实有。
- `packages/orchestrator/internal/sandbox/fc/process.go` `configure()`：上游用
  `SysProcAttr.UseCgroupFD/CgroupFD`（CLONE_INTO_CGROUP）把 FC 放进 `sbx-<id>`，
  **ARM 移植补丁 `fbee6fcd1` 把这几行整段注释掉了**（原因、以及它在 cgroup v1 / v2 机器上
  分别意味着什么，见 [11 篇 §1.1](11-沙箱绑核与NUMA亲和.md)）。于是 FC 留在父进程的 cgroup，
  即 Nomad task scope。
  验证：`cat /proc/$(pgrep -n firecracker)/cgroup` 应显示 `nomad.slice/...start.scope`，
  `cat /sys/fs/cgroup/e2b/sbx-*/cgroup.procs` 应为空。
- `packages/api/internal/sandbox/sandbox_features.go` `HasHugePages()`：FC ≥ 1.7 恒为 true，
  部署用 1.13.1，沙箱和模板构建都以 `huge_pages=2M` 起 VM；
  `firecracker/src/vmm/src/vmm_config/machine_config.rs` 里对应 `MAP_HUGETLB | MAP_HUGE_2MB`。
  hugetlb 页由 hugetlb 控制器记账，memory 控制器不记（cgroup2 以 `memory_hugetlb_accounting`
  挂载时例外，6.6+ 才有该选项、默认关；`grep cgroup2 /proc/mounts` 可查）。

| 行为 | 记在谁账上 |
|---|---|
| 解压基础镜像的层 | page cache → **Nomad task cgroup** |
| 拼 rootfs、写 ext4 | page cache → **Nomad task cgroup** |
| 写 memfile、checkpoint 差分文件 | page cache → **Nomad task cgroup** |
| orchestrator 自己的堆（差分计算、块缓存） | anon → **Nomad task cgroup** |
| firecracker 进程 guest 以外的内存（几十 MB） | anon → **Nomad task cgroup** |
| 沙箱 / provision 沙箱的 guest 内存 | hugetlb 控制器，**不进 memory.max** |
| `/mnt/snapshot-cache`（65 GB tmpfs） | 代码里只 `MkdirAll`，没有写入路径，不构成压力 |

**为什么峰值远高于进程 RSS**：page cache 名义上可回收，但脏页要先回写；构建一口气解压
几百 MB 的层、写整份 rootfs 和 memfile，回写跟不上时 memcg 就先于系统 OOM。
这台机器 swap 只有 4 GB 且已用满，内核没有退路，只能挑 cgroup 里最大的进程杀。

## 2. 量出该给多少

**别拍脑袋，读实测峰值**（cgroup v2 / Linux 5.19+ 提供 `memory.peak`）：

```bash
# 跑完一次典型构建后读
for f in /sys/fs/cgroup/nomad.slice/share.slice/*.start.scope/memory.peak; do
  echo "$f: $(numfmt --to=iec < $f)"
done

# 构建过程中实时盯
watch -n2 'for f in /sys/fs/cgroup/nomad.slice/share.slice/*.start.scope/memory.current; do
  numfmt --to=iec < $f; done; df -h /mnt/snapshot-cache | tail -1'
```

取值建议：**峰值 × 3~4**，再留出并发构建的余量。现值 256 GiB 是在 1.1 TB 机器上取的：
hugepages 只预分配 20%（`start-client.sh`，其余 80% 走 overcommit），普通内存充裕，
256 GiB 仍能起到"跑飞了别拖垮整机"的作用。换到小内存机器要按本机峰值重新取。

注意两点：
- Nomad 调度侧也按这个数预留，将来多节点/多任务时这个值会影响调度；
- 如果哪天关掉 hugepages（FC < 1.7，或改了 `HasHugePages()`），或 cgroup2 挂上
  `memory_hugetlb_accounting`，沙箱 guest 内存会一下子全进这个账，配额语义随之改变。

## 3. ★ 改哪里：三个文件，改错一个就白改

```
仓库 e2b-deploy/dep/template-manager.hcl
  └─打包进 e2b-deploy.tar.gz (Source9)
     └─spec:  cp -rp %{_builddir}/e2b-deploy/* %{buildroot}/opt/e2b-infra/
        └─► /opt/e2b-infra/dep/template-manager.hcl        ① build.sh -i 的源头
             └─build.sh -i:  cp -fv "$DEP_DIR/template-manager.hcl" "$E2B_DIR/nomad/..."
                └─► /opt/e2b-infra/nomad/template-manager.hcl   ② -r/-f 渲染的输入
                     └─deploy.sh envsubst
                        └─► /opt/e2b-infra/rendered/template-manager.hcl  ③ nomad 实际读的
```

**只改 ② 不改 ①** → 下次 `build.sh -i` 会用 ① 覆盖 ②，改动丢失。
**只改 ①② 不重新渲染** → ③ 还是旧值，nomad 读的仍是旧配额。

### 只在服务器上临时改（不出包）

```bash
NEW=65536      # 目标值，MiB

sed -i "/^ *resources {/,/^ *}/ s/^\( *memory *= *\)[0-9]\+/\1$NEW/" \
    /opt/e2b-infra/dep/template-manager.hcl \
    /opt/e2b-infra/nomad/template-manager.hcl

# 重新渲染 + 提交 job（resources 变了 → spec 有 diff → Nomad 自动重建 alloc）
bash /opt/e2b-infra/build.sh -r template-manager
```

⚠️ 重建 alloc 会**杀掉运行中的沙箱**，挑好时机。之后暖池会重新预创建槽位，
`ip netns list` 的数量回到 ~300 是正常的（见 11 篇 §0）。

### 要让它跨 RPM 升级存活

改仓库里的 `e2b-deploy/dep/template-manager.hcl`，重新打 `e2b-deploy.tar.gz` 并出包
（见 [`03-RPM包构建深度解析.md`](03-RPM包构建深度解析.md) §4）。
`/opt/e2b-infra/dep/` 是 RPM 装的，`rpm -U` 会覆盖它。

## 4. 验证生效

```bash
cd /opt/e2b-infra && source .env && export NOMAD_TOKEN=$NOMAD_ACL_TOKEN

# 三份文件应一致
grep -n -A3 'resources {' /opt/e2b-infra/dep/template-manager.hcl \
                          /opt/e2b-infra/nomad/template-manager.hcl \
                          /opt/e2b-infra/rendered/template-manager.hcl

# Nomad 侧实际生效值
ALLOC=$(nomad job status template-manager-system | awk '$1 ~ /^[0-9a-f]{8}$/{print $1; exit}')
nomad alloc status "$ALLOC" | grep -A3 'Task Resources'
#   Memory 一列应显示 "xx MiB/<新值> GiB"

# cgroup 里的硬限（最终真相）
cat /sys/fs/cgroup/nomad.slice/share.slice/*.start.scope/memory.max | numfmt --to=iec
```

## 5. 配额不足时的现场特征

按这个顺序对照，能快速确认是不是配额问题：

```bash
nomad alloc status <alloc>          # Recent Events
#   Terminated  Exit Code: 137, Signal: 9      ← 137 = 128+9，被 SIGKILL

dmesg -T | grep -iE 'oom|killed process' | tail
#   oom-kill:constraint=CONSTRAINT_MEMCG,      ← MEMCG 而非系统内存不足
#     oom_memcg=/nomad.slice/share.slice/<allocID>.start.scope
#   Killed process (template-manage) anon-rss:79636kB file-rss:45712kB
#                                    ↑ 只有 125 MB，别被这个数字误导
```

**连锁反应链**（知道这条链，才不会在 api 上白查半天）：

```
配额不足 → orchestrator 被 memcg OOM kill
         → Nomad 重启它，再次 OOM，重试次数耗尽
         → template-manager-system 的 alloc 变 failed
         → api 的 orch.NodeCount() 恒为 0
           （packages/api/internal/handlers/store.go：只有节点数 != 0 才置 healthy）
         → api /health 恒 503
         → deploy.sh 卡在 api 的 deployment 上，10 分钟 progress deadline 后失败
         → 报错信息全在 api 上，与真正的根因（另一个 job 的内存配额）毫无字面关联
```

## 6. cpu = 2048 是什么

不是绑核，也不是"2 个核"。Nomad 的 `resources.cpu` 单位是 MHz，用于调度侧预留和
换算 cgroup 的 `cpu.weight`（相对权重）。想把沙箱限制到指定物理核，那是另一回事，
见 [`11-沙箱绑核与NUMA亲和.md`](11-沙箱绑核与NUMA亲和.md)。

## 7. 变更记录

| 日期 | 变更 | 依据 |
|---|---|---|
| 2026-09 | `memory` 8192 → 32768 | 200 MB 基础镜像 + `skip_cache=True` 全量重建在 8 GiB 下必然 memcg OOM；上调后同一构建 7 秒完成 |
| 2026-09-15 | `memory` 32768 → 262144 | 查实 FC 进程就在 task cgroup 里（arm64 补丁注释掉了 CLONE_INTO_CGROUP），guest 内存因 hugetlb 不记账；§0/§1 按代码事实改写，上限按 1.1 TB 机器取为护栏值 |
