# 手册配图的画图脚本

两本手册的配图都是 SVG，分别放在 `rollback/docs/img/` 和 `criu-rollback/docs/img/`。每张图都由这里的一个 Python 脚本生成，不要手改 SVG。

## 文件

| 文件 | 用途 |
|---|---|
| `draw_<书>_<章>.py` | 一章一个脚本，一个脚本可以画该章的多张图。`<书>` 是 `rollback` 或 `gsd`，`<章>` 是两位章号或附录字母 |
| `common.py` | 公共部分：kit 路径、颜色别名、输出路径函数 `out(书, 文件名)` |
| `kit/` | 复制自 explainer-chart skill 的 `Diagram`、`Chart` 两个类，不在这里修改，来源见 `kit/README.md` |

有几个脚本会复用别的脚本里的函数：

- 数据面板 `gbars`、`dotrows`、`tier_panel` 等定义在 `draw_rollback_20.py`，`draw_rollback_18/19/21.py` 和 `draw_criu_20/D/E.py` 会导入它们，`draw_rollback_C.py` 则通过 `draw_rollback_21.py` 间接用到；
- `draw_rollback_04.py` 里的工具函数被 `draw_rollback_01/02.py` 导入。

修改这些共用函数后，要把所有导入它们的脚本都重跑一遍。

## 生成

只需要 Python 3，不需要第三方包。在本目录执行：

```bash
for f in draw_*.py; do python3 "$f" || echo "失败：$f"; done
```

脚本的输出是确定的：输入不变，重跑后 SVG 逐字节相同。所以用 `git diff` 就能看出这次实际改了哪几张图。

## 约定

- **文件名：** `NN-k-中文名.svg`，k 是这张图在本章里的出现顺序。生成手册时，图注前会自动加上“图 NN-k”。
- **颜色：**
  - teal 表示沙箱级方案，coral 表示原生 pause / resume，purple 表示进程级，gray 表示共同部分；
  - 数据图里，blue 表示 950，amber 表示 920B。
- **沙箱级手册的图里不出现 gsd、CRIU、进程级。**
- **图里的名字、数字、步骤都要能在对应章节的正文里找到。**

## 用新数据更新数据图

数据图的数字都集中写在脚本开头的常量里，每组数字都注明了出处，即对应 md 文件的行号。重测之后，或者 950 有了数据之后，按下面的步骤更新：

1. 先改正文里的表格；
2. 再改脚本开头对应的常量，副标题里的平台、日期、样本数也要一起改；
3. 重跑脚本，打开生成的 SVG 看一遍；
4. 重新生成手册。

正文里写着“待填”的数字不画。进程级手册的 22-1（950 分档）和 24-4（950 同平台对比）要等 950 数据出来后再画。

## 自查

生成 SVG 后，至少用浏览器打开看一遍，确认没有文字溢出、文字压到边框、箭头穿过方框。逐项检查的标准见 explainer-chart skill 第三节。
