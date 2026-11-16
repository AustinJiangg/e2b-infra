# Checkpoint / Restore 手册与内部验证工具

本目录保存快照回滚手册源文件、图表、构建工具与上机验证工具。**正式交付为单文件HTML及KASandbox功能代码**；`e2b-infra`是内部部署、测试与文档制作辅助仓库，客户访问可另行安排。

## 当前手册

当前手册为六篇、30章、五个附录，配图随正文。950为实测主线，920B性能与平台差异在第20章，920B详细数据和工程修复案例在附录。

- [手册首页与完整目录](docs/README.md)
- [交付范围、版本与阅读方法](docs/01-delivery-and-reading.md)
- [功能结果](docs/17-functional-verification.md)、[950性能与HDBSS](docs/18-950-performance.md)、[原生pause / resume分档](docs/19-native-pause-resume.md)、[920B性能与平台差异](docs/20-920b-performance.md)、[并发与长跑](docs/21-concurrency-and-longrun.md)、[验收与结论边界](docs/22-acceptance.md)
- [配置与容量](docs/26-configuration-and-capacity.md)、[测试环境与工具](docs/E-environment-and-tools.md)
- [920B详细结果](docs/B-920b-results.md)、[工程修复案例](docs/C-evolution-and-cases.md)
- [950 测试结果摘要](evidence/950/README.md)
- [面向客户与管理者的价值解读文章](articles/sandbox-rollback.md)、[文章构建与阅读说明](articles/README.md)

讲解固定到`KASandbox_0904/deltabox-dev@57a30632cb9cb8eb67a35dd226d9ef4b40458af4`。单元测试保留在开发分支，完整上机测试工具保留在[`scripts/`](scripts/README.md)。代码未来经`deltabox`合入上游；各轮实际被测身份分别记录，不从讲解基准推定。

## 生成单文件HTML

依赖安装和检查方法见[`tools/README.md`](tools/README.md)。已安装依赖时，在仓库根目录执行：

```bash
bash rollback/tools/build.sh --check
bash rollback/tools/build.sh
```

输出为`rollback/dist/checkpoint-restore-docs.html`，内含分组目录、正文、附录和各章配图（SVG 内嵌），可离线打开。书名与仓库链接由[`book.json`](book.json)维护，分篇和章序由[`contents.json`](contents.json)维护。HTML随仓库提交，[单机部署文档](../single-node-offline-deploy.md)里有下载链接。

本地重建不会提交或推送。

## 内部资料分工

| 目录 | 用途 |
|---|---|
| `docs/` | 当前手册Markdown，参与HTML生成 |
| `diagrams/` | 早期三张手画汇报图，现只供 `slides/` 使用；手册配图在 `docs/img/` |
| `tools/` | 构建、链接检查、画图脚本（`tools/figures/`）和代码统计 |
| `scripts/` | 功能、性能、并发、HDBSS和开发诊断工具 |
| `evidence/` | 已回传测试证据、环境说明与逐文件校验清单 |
| `articles/` | 独立价值解读文章、配图及阅读样式 |
| `stats/` | 固定源码提交的代码统计范围与归档 |
| `slides/` | 既有汇报材料，按各自版本维护 |
| `dist/` | 生成的单文件HTML，随仓库提交，不作为源文件编辑 |

完整E2B部署、RPM和环境维护资料继续在[`../deploy-docs/`](../deploy-docs/README.md)及[`../single-node-offline-deploy.md`](../single-node-offline-deploy.md)维护。客户已有E2B与配套SDK，手册第五篇保留简要构建与组件配对、特性前提、自检、配置和诊断。
