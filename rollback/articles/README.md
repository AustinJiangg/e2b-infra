# 快照回滚价值解读文章

独立于技术手册维护的图文长文，面向客户、管理者和非本领域的软件工程师。

- 正文：[让 AI Agent 可以回到上一步](sandbox-rollback.md)，v0.1.0，2026-10-17。
- 阅读版：生成到 `../dist/sandbox-rollback-article.html`。正文、样式和四张 SVG 配图全部内嵌，支持离线阅读、明暗切换、手机阅读与打印排版。
- 技术依据：《E2B快照回滚技术手册》v0.5.6。文内编号跳到本页依据，依据中的章节链接打开同目录的 `checkpoint-restore-docs.html`；需要跳转查阅时将两份 HTML 放在一起。
- 配图：[任务流程](assets/task-flow.svg)、[恢复边界](assets/state-boundary.svg)、[增量与原地恢复](assets/incremental-restore.svg)、[性能曲线](assets/performance.svg)。

在仓库根目录生成：

```bash
python3 rollback/tools/build_article.py
python3 rollback/tools/mdlinks.py rollback/articles
```

使用手册已有的 Python `markdown-it-py` 与 `tools/node_modules` 依赖。性能图由 D3 生成，数据直接读取正文表格；其余 SVG 由生成器绘制。修改文章后重新运行生成器即可更新阅读版与配图。

文章采用独立版本号，本次文章与手册 v0.5.6 一同阶段归档，功能代码的交付分支合入另行处理。案例为示意；实测数据按来源轮次标注。SDK 示例已核对接口与语法，本轮未连接实际沙箱执行。
