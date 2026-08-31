# 汇报用 slidev 工程

## 在 WSL 里起服务，在 Windows 浏览器看

```bash
cd ~/projects/e2b-repo/e2b-infra/rollback/slides
npm run dev
```

然后在 **Windows 的浏览器**打开 <http://localhost:3030>。
WSL2 默认做 localhost 转发，不用查 WSL 的 IP，也不用改防火墙。
（万一转发没生效：`ip addr show eth0` 拿到 WSL 的 172.x 地址，用 `http://172.x.x.x:3030`。）

`npm run dev` 已经带 `--remote`，所以同一局域网的其他机器也能访问。

## 常用

| 命令 | 干什么 |
|---|---|
| `npm run dev` | 起开发服务器，改 `slides.md` 自动热更新 |
| `npm run check` | **逐页检查有没有内容被画布裁掉**（要先开着 dev） |
| `npm run build` | 出静态站到 `dist/`，可以直接拷给别人 |
| `npm run export` | 导 PDF 到 `checkpoint-restore.pdf` |

演讲者模式：<http://localhost:3030/presenter>。按 `f` 全屏，`o` 看幻灯片总览。

## 改完 slides.md 一定要跑一次 `npm run check`

slidev 的画布是**固定 980×552**，超出的内容被 `overflow:hidden` 直接切掉：
不报错、不滚动、在编辑器里也看不出来，只有翻到那一页才发现表格少了两行。
`npm run check` 把每页子元素的边界和画布比一遍，越界就打出越界多少像素、是哪个元素，
有问题时退出码非 0。**2026-08-31 就是这么发现第 8 页裁掉 118px、第 9 页裁掉 134px 的。**

内容塞不下时，优先收紧行距（表格单元格的上下 padding 默认 0.5rem，十几行就吃掉 100px 以上）
或重排成两列，别再往下缩字号 —— 投屏上 `text-xs`（12px）已经是下限。

## 导 PDF 需要系统装中文字体

**这条最容易踩**：WSL 默认一个中文字体都没有（`fc-list :lang=zh-cn` 是空的），
而 PDF 是在 WSL 里用 headless chromium 渲染的，**导出来整份中文全是方框**。
Windows 浏览器看 dev server 一切正常，因为那边用的是 Windows 的字体 —— 所以这个问题
只在 PDF（和 WSL 里的截图）上暴露。

本机已经装好了（2026-08-31，非 root，装在用户目录）：

```bash
apt-get download fonts-noto-cjk                       # 不需要 root
dpkg -x fonts-noto-cjk_*.deb /tmp/noto
cp /tmp/noto/usr/share/fonts/opentype/noto/*.ttc ~/.local/share/fonts/
fc-cache -f
fc-list :lang=zh-cn family | sort -u                  # 确认非空
```

`~/.config/fontconfig/fonts.conf` 里另外加了一条：zh-CN 优先解析到 **Noto Sans CJK SC**。
不加的话 fontconfig 会先挑到 JP 那份，一部分字（骨、直、门…）会是日文字形。

## 导出 / 检查要带 LD_LIBRARY_PATH

headless chromium 缺 `libnspr4` 等系统库（装它们要 root）。库已经解包在 `.local-libs/`：

```bash
export LD_LIBRARY_PATH=$PWD/.local-libs/usr/lib/x86_64-linux-gnu:$LD_LIBRARY_PATH
npm run export
```

`npm run dev` 不需要这个，它不起 chromium。

## 素材在哪

| 要什么 | 去哪拿 |
|---|---|
| 完整技术文档（23 篇） | `../docs/` |
| 当前验收状态与口径 | `../docs/17-observability-and-verification.md` |
| 交付件与中间产物的划分、测试覆盖与缺口 | `../../../交付件清单.md` |
| 工具箱逐脚本的验证状态、双轨 commit | `../test-950/MANIFEST.md` |
| 耗时对照与三个发现 | `../test-950/耗时基准结论.md` |
| 原始数据（12 份报告，含 950 那轮的原始终端记录） | `../test-950/reports/` |
| 方案设计（最终落地的两条路线） | `../../../rollback-version-2/Fable5-...-v3-ext4.md`、`...-XFS-Reflink-方案总结.md` |

Node 装在 `~/.local/node`（v24 LTS，非 root 安装），`~/.bashrc` 里已加 PATH。
