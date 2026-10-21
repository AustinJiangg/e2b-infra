# Checkpoint / Restore 手册与内部验证工具

本目录保存快照回滚手册源文件、图表、构建工具与上机验证工具。**正式交付为单文件HTML及KASandbox功能代码**；`e2b-infra`是内部部署、测试与文档制作辅助仓库，客户访问可另行安排。

## 当前手册

当前手册版本为 **v0.5.8（2026-10-21）**：六篇、28章、五个文字附录及一个内嵌配图附录，950为实测主线，920B详细数据和历史修复分别进入附录。

- [手册首页与完整目录](docs/README.md)
- [交付范围、版本与阅读方法](docs/01-delivery-and-reading.md)
- [950功能结果](docs/17-functional-verification.md)、[性能与HDBSS](docs/18-950-performance.md)、[并发与持续运行](docs/19-950-concurrency.md)
- [配置与容量](docs/24-configuration-and-capacity.md)、[测试环境与工具](docs/E-environment-and-tools.md)
- [920B详细结果](docs/B-920b-results.md)、[版本演进与工程修复案例](docs/C-evolution-and-cases.md)
- [本次迁移与检查记录](migration/README.md)
- [950 测试结果摘要](evidence/950/README.md)、[阶段收尾与后续事项](milestones/2026-10-17-sandbox.md)
- [面向客户与管理者的价值解读文章](articles/sandbox-rollback.md)、[文章构建与阅读说明](articles/README.md)

讲解固定到`KASandbox_0904/deltabox-dev@57a30632cb9cb8eb67a35dd226d9ef4b40458af4`。单元测试保留在开发分支，完整上机测试工具保留在[`scripts/`](scripts/README.md)。代码未来经`deltabox`合入上游；各轮实际被测身份分别记录，不从讲解基准推定。

## 生成单文件HTML

依赖安装和检查方法见[`tools/README.md`](tools/README.md)。已安装依赖时，在仓库根目录执行：

```bash
bash rollback/tools/build.sh --check
bash rollback/tools/build.sh
```

输出为`rollback/dist/checkpoint-restore-docs.html`，内含分组目录、正文、附录、Mermaid运行库及矢量图，可离线打开。版本与日期由[`book.json`](book.json)维护，分篇和章序由[`contents.json`](contents.json)维护。

本地重建不会提交、推送、创建标签或更新Release。历史发布件保持各自版本；已发布的阶段版本为 `docs-v0.5.6`；当前修订版由源码重新构建，本轮不更新 Release。

## 内部资料分工

| 目录 | 用途 |
|---|---|
| `docs/` | 当前手册Markdown，参与HTML生成 |
| `diagrams/` | 图表源文件及内嵌SVG |
| `tools/` | 构建、链接、图表、统计和离线交互检查 |
| `migration/` | 内部迁移清单与检查记录，不进入交付HTML |
| `scripts/` | 功能、性能、并发、HDBSS和开发诊断工具 |
| `evidence/` | 已回传测试证据、环境说明与逐文件校验清单 |
| `milestones/` | 阶段基准、发布范围和后续事项 |
| `articles/` | 独立价值解读文章、配图及阅读样式 |
| `stats/` | 固定源码提交的代码统计范围与归档 |
| `slides/` | 既有汇报材料，按各自版本维护 |
| `dist/` | 生成产物，不作为源文件编辑 |

完整E2B部署、RPM和环境维护资料继续在[`../deploy-docs/`](../deploy-docs/README.md)及[`../single-node-offline-deploy.md`](../single-node-offline-deploy.md)维护。客户已有E2B与配套SDK，手册第五篇保留简要构建与组件配对、特性前提、自检、配置和诊断。
