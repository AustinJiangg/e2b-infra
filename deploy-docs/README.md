# deploy-docs：单机 ARM 部署原理详解

本目录是 **e2b-infra 单机（单台 aarch64 服务器）离线部署的"原理讲解"文档集**，
面向刚接触这套部署的新人：目标是读完之后，能说清楚仓库里每个脚本、每个文件是干什么的，
`build.sh -i` / `build.sh -s` 每一步在系统里改了什么，出了问题知道去哪里看日志、用什么命令排查。

## 与其它文档的分工

仓库里已有几份文档，各自定位不同，**不要混着看**：

| 文档 | 定位 | 什么时候看 |
|---|---|---|
| `single-node-offline-deploy.md`（仓库根目录） | **操作手册（runbook）**：装机步骤、改动后怎么生效（5 大场景）、故障排查、参数扫描 | 动手操作时照着做 |
| **本目录 `deploy-docs/`** | **原理讲解**：每一步"为什么、做了什么、改了系统哪里" | 新人入门、想深入理解时 |
| `benchmark/README.md` 及同目录文档 | 压测方案：沙箱启动耗时的采集、分析、可视化 | 做性能测试/瓶颈定位时 |
| `docs/zh/` | 上游风格的通用安装/使用/维护文档（多节点、k8s 形态也在内） | 了解多节点/k8s 部署形态时 |
| 根目录 `README.md` | 项目介绍 + 多节点 Nomad/k8s 安装教程 + 依赖自建教程 | 概览 |

一句话：**怎么做**看 `single-node-offline-deploy.md`，**为什么这么做/背后发生了什么**看本目录。

> 一个例外：本目录的 `08-源码开发与出包流程.md` 是**操作流程**而非原理讲解。
> 它讲的是"改 Go 源码"这条链路（源码工作树 → 重新生成 patch → 出 RPM），
> 而 `single-node-offline-deploy.md` 讲的是"拿到 RPM 之后"的装机与生效。两者首尾相接。

## 文档清单

| 文档 | 内容 |
|---|---|
| [`01-整体架构与组件总览.md`](01-整体架构与组件总览.md) | 单机部署长什么样：组件清单、请求链路、端口表、宿主机目录地图 |
| [`02-仓库文件地图.md`](02-仓库文件地图.md) | 仓库里每个文件/目录的作用，装机后 `/opt/e2b-infra` 的布局从哪来 |
| [`03-RPM包构建深度解析.md`](03-RPM包构建深度解析.md) | e2b-infra.spec 逐段精读：源码包、补丁、离线 Go 构建、%install 映射、%files 语义、如何重建 |
| [`04-build.sh-i-安装组件详解.md`](04-build.sh-i-安装组件详解.md) | `build.sh -i` 逐函数拆解：postgres/minio/harbor/nginx/e2b 各装了什么、改了系统哪里 |
| [`05-build.sh-s-启动部署详解.md`](05-build.sh-s-启动部署详解.md) | `build.sh -s` 逐步拆解：consul/nomad 拉起与 ACL bootstrap、宿主机调优、deploy.sh 渲染与提交 job |
| [`06-日常运维手册.md`](06-日常运维手册.md) | 各组件怎么看状态/日志/重启：nomad、consul、postgres、minio、harbor、nginx、dnsmasq 等，含巡检清单 |
| [`07-single-node-traffic-architecture.md`](07-single-node-traffic-architecture.md) | 流量处理深度剖析：resolv.conf/glibc 解析原理、dnsmasq 分流、iptables 80→3002、三类流量完整链路、验证与排障 |
| [`08-源码开发与出包流程.md`](08-源码开发与出包流程.md) | 改 Go 源码的完整工作流：部署线 vs 源码线两条开发线、两个仓库+Go SDK 的心智模型、源码工作树搭建（一段可直接粘的八步脚本）、**日常高频循环＝改码→编译→直接换二进制→验证（不出包）**、阶段收尾才做的"重生成 patch + 出 RPM"、报错速查表 |
| [`09-增量快照实现与ARM实测分析.md`](09-增量快照实现与ARM实测分析.md) | 快照存了哪些文件、三套实现对比（FC 原生 / e2b x86 / 本仓库 ARM）、UFFD 写保护如何决定增量精度、ARM 上「读过即脏」的实测数据与优化方向；配套脚本 `benchmark/snapshot.py` |
| [`10-模板与快照存储位置梳理.md`](10-模板与快照存储位置梳理.md) | 模板/快照到底存在哪：本地缓存 vs 持久化存储的分层、`STORAGE_PROVIDER` 如何选 provider、为什么 `.env` 里的 MinIO 在 nomad 单机路径下没生效、想换成 MinIO 的完整改法与回滚 |
| [`11-沙箱绑核与NUMA亲和.md`](11-沙箱绑核与NUMA亲和.md) | 多核大内存机器上怎么把沙箱限制到指定 CPU：两棵 cgroup 树的结构（为什么沙箱不受 hcl 的 resources 约束）、三个方案对比、cpuset 绑核的完整操作（持久化是可选项，默认不绑才是常态）、按大页余量换算并发上限 |
| [`12-orchestrator资源配额调优.md`](12-orchestrator资源配额调优.md) | `template-manager.hcl` 里 `resources { memory, cpu }` 管什么（只管 orchestrator 与模板构建，不管沙箱）、怎么量实测峰值定值、改动必须同步的三个文件、配额不足时从 OOM 到 api 恒 503 的连锁反应链 |

## 推荐阅读路径

**新人第一天**（自顶向下建立框架）：

1. `01-整体架构与组件总览.md` —— 先知道这台机器上到底跑着什么、彼此怎么连。
2. `04-build.sh-i-安装组件详解.md` + `05-build.sh-s-启动部署详解.md` —— 顺着装机的两条命令，把每个组件是怎么来的过一遍。
3. `06-日常运维手册.md` —— 把常用命令跑一遍，对着真实环境验证前两步的理解。

**要动仓库源码/出包的人**，再读：

4. `02-仓库文件地图.md` —— 改一个东西之前，先知道它在仓库里的"源头"是哪个文件。
5. `03-RPM包构建深度解析.md` —— 理解从源码到 `/opt/e2b-infra` 的完整供应链。
6. `08-源码开发与出包流程.md` —— 上面两篇是"原理"，这篇是**你每天照着做的操作流程**：
   环境怎么配、源码工作树怎么搭、改完代码怎么重新生成 patch 和出包。

**要做变更/调参数的人**：读完上面后直接用 `single-node-offline-deploy.md` 第 5 节的场景表操作；
涉及 CPU/NUMA 资源隔离的调优另见 `11-沙箱绑核与NUMA亲和.md`。

## 全文约定

- 本机 IP 记作 `SERVER_IP`（部署前必须写进 `e2b-deploy/dep/.env` 的 `SERVER_IP=`）。
- `/opt/e2b-infra/` 指 RPM 安装出来的部署目录；`e2b-deploy/` 指仓库里与之对应的可读源目录。
- 命令默认以 root 在部署机上执行。
- 文中引用的行为以当前仓库代码为准（spec Release 3，e2b 2.20.0 / e2b_code_interpreter 2.4.1，
  consul 1.21.4 / nomad 1.10.4 / firecracker 定制版（基于 v1.12.1，装到 `/fc-versions/v1.13.1/`）/ harbor v2.13.0）。
