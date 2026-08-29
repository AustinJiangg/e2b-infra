# rollback —— checkpoint / restore 的说明、配图与验证工具箱

沙箱活着时的高频快速回退。本目录是这套功能的**文档与验证材料**；
实现代码在仓库根的 `0001-adapted-for-arm-architecture.patch` 与 `firecracker.arm` 里。

| 子目录 | 内容 | 给谁 |
|---|---|---|
| [`diagrams/`](diagrams/) | 设计说明（`checkpoint-restore-design.md`）+ 三张汇报用 SVG + mermaid 源 | 想读懂设计、或要对外汇报的人 |
| [`test-950/`](test-950/) | 开发态验证工具箱：宿主自检 / 造数据卷 / 两套之间切换 / 穷举与劣化排查脚本，以及 11 份实测报告 | 我们自己，做上机验证与排查 |

## 从哪读起

1. **理解这套东西** → [`diagrams/checkpoint-restore-design.md`](diagrams/checkpoint-restore-design.md)，从 §0 定位开始；
2. **看当前实测到哪一步** → 同一文档的 §8；
3. **要在机器上验收** → 仓库根的 `benchmark/checkpoint_verify.py`（正确性）和
   `benchmark/checkpoint_bench.py`（性能）。这两个是**交付态**脚本，各自单文件、零共享依赖，
   两条命令跑完；
4. **要做深入排查** → `test-950/`，那是**开发态**工具箱，脚本之间有共享模块，需要配路径。

> 交付态与开发态这两套测试脚本**不要混用**。宿主探针在两个交付脚本里故意重复了一份，
> 就是为了防止只拷走一半、剩下的静默变成 unknown。

## 当前状态

2026-08-29 已在 950 上通过正确性验收，**脏页后端为 HDBSS（硬件标脏）**，59 项校验全过。
性能分档基准尚未在 950 上跑过；920B 上的全部历史数字来自软件写保护路径，两者口径不同。
详见设计说明 §8。

## 关于 `test-950/bin/`

四个被测二进制（两个 orchestrator 各 108 MB、两个 Firecracker）**不进本仓库** ——
它们是构建产物，可从 `infra-arm` / `KASandbox` 的对应 commit 重建，且超过 GitHub 的单文件上限。
目录里只保留 `SHA256SUMS`，用来对上「当时验的是哪一版」。二进制本身在 WSL 工作区。

## 不在这里的东西

方案设计稿、原理调研、过程日志与一次性探针仍在 WSL 工作区 `e2b-repo/`，尚未同步到本仓库。
`infra-arm` 与 `KASandbox` 的源码分支各自推到自己的 origin，不在这里重复存放。
