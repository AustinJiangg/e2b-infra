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

1. 这个配额**只管 orchestrator 进程和模板构建**，**不管沙箱**。
   "并发几十个沙箱毫无压力"完全不能说明这个值够用。
2. 撑爆它的通常不是进程堆，而是 page cache 和 `/mnt/snapshot-cache`(tmpfs) 的 shmem 页。
   所以 OOM 现场的 `anon-rss` 可能只有几十 MB，看着像"没吃内存却被杀"。
3. 现值 **32768 MiB**（原 8192，2026-09 上调）。要改，**必须改三个地方**（§3），
   只改运行态会被下次 `build.sh -i` 覆盖回去。
4. 别无脑往大调。这个上限的意义是"跑飞了别把整台机器拖垮"，共享机器上尤其如此。
   按实测峰值的 3~4 倍取值（§2）。

---

## 1. 它管什么、不管什么

这套部署里有两棵互不相干的 cgroup 树：

```
/sys/fs/cgroup/
├── nomad.slice/share.slice/<allocID>.start.scope   ← template-manager.hcl 的 resources 管这里
│      └── orchestrator 主进程（/usr/bin/template-manager）
│            · 模板构建：解压镜像层、拼 rootfs、写快照
│            · 这些动作产生的 page cache 与 tmpfs shmem 页，全记在这个 cgroup 账上
│
└── e2b/<sandbox>/                                  ← orchestrator 自建，resources 管不到
       └── 每个沙箱的 firecracker 进程
             · VM 内存走 hugepages，连常规 memcg 的账都不走
```

代码依据：`packages/orchestrator/.../sandbox/cgroup/manager.go` 里
`RootCgroupPath = /sys/fs/cgroup/e2b`，orchestrator 启动日志会打印
`initialized root cgroup {"path": "/sys/fs/cgroup/e2b"}`。

| 行为 | 记在谁账上 |
|---|---|
| 解压基础镜像的层 | page cache → **Nomad task cgroup** |
| 拼 rootfs、写 ext4 | page cache → **Nomad task cgroup** |
| 写快照到 `/mnt/snapshot-cache`（65 GB tmpfs） | shmem → **Nomad task cgroup** |
| 构建时的 provision 沙箱 | `/sys/fs/cgroup/e2b` |
| 运行中的沙箱 | `/sys/fs/cgroup/e2b` + hugepages |

**为什么 tmpfs 是压垮它的那根稻草**：page cache 可回收（memcg 有压力时先回写再丢弃），
而 tmpfs 的 shmem 页不能简单丢弃，只能换出到 swap。这台机器 swap 只有 4 GB 且已用满，
内核没有退路，只能挑 cgroup 里最大的进程杀。

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

取值建议：**峰值 × 3~4**。峰值 5–10 GiB 对应 32 GiB，已经很宽裕；要构建几 GB 的大镜像
或跑并发构建，再按同样比例上调。

不建议直接设成几百 G：
- 这是**共享服务器**，limit 的作用就是"某个东西跑飞了别把整机拖垮"，设得过大等于关掉这层保护；
- Nomad 调度侧也按这个数预留，语义会失真，将来多节点/多任务时会误导调度。

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
