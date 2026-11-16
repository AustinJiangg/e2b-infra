# 单文件HTML产物

产物为 `checkpoint-restore-docs.html`，随仓库提交，可直接下载。内容包括六篇、30 章和五个附录，配图以 SVG 内嵌；双击用浏览器打开，不联网、不装任何东西。

在 `e2b-infra` 仓库根目录重新构建与检查：

```bash
bash rollback/tools/build.sh --check
bash rollback/tools/build.sh
```

源文件入口为[`../docs/README.md`](../docs/README.md)，构建方法见[`../tools/README.md`](../tools/README.md)。修改源文件后重新生成，不直接编辑HTML。
