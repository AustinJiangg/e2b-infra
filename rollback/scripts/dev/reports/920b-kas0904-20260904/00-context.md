# 920B 实测存档 —— KASandbox_0904 交付件（2026-09-04）

## 被测对象

全部跑在**从 KASandbox_0904 源码构建**的二进制上（`deltabox` 分支 `cd66b2140`）：

| 组件 | 来源 | sha256(前16) |
|---|---|---|
| orchestrator | `go build ./packages/orchestrator`（远程 clone 的那份） | `aa13d05e9a593c3c` |
| firecracker | `tools/devtool build --release`，aarch64-musl 静态 | `05774e5376d27623` |
| fc-netns-exec | `go build ./packages/orchestrator/cmd/fc-netns-exec` | 现装于 `/opt/e2b-infra/bin/` |
| **py-sdk** | `py-sdk/scripts/build-wheel.sh` 出的 `e2b-2.19.0-py3-none-any.whl`，装进独立 venv | 含 `e2b/checkpointd`，无 `e2b/gsd` |

**机器**：920B（`193.70.8.2`），Kunpeng 920，openEuler 24.03 LTS，aarch64。
**脏页后端**：`kvm-wp`（软件写保护）—— 920B 的 KVM **不支持** cap 502，
环境里设了 `FC_TRACK_DIRTY_PAGES=true` 强制开启跟踪，所以走到软件写保护。
**耗时数字都含 VM exit 开销，950 上有 HDBSS 会明显更快。**

## 结果

| 日志 | 脚本 | 结果 |
|---|---|---|
| **`ours-verify.log`** | `deploy/checkpoint_verify.py` + **我们的 SDK** | **59/59 ALL PASS** ← 三部分交付件齐全的那一轮 |
| `e2e-verify.log` | 同脚本，我们的二进制 + **phz 的旧 gsd SDK** | 57/57 ALL PASS |
| `kas0904-verify.log` | 同脚本，原 infra-arm 二进制 + phz SDK（对照） | 57/57 ALL PASS |
| `t-correctness.log` | `test-950/correctness.py` | **31/31 ALL PASS** |
| `t-timing.log` | `test-950/timing.py ext4 /` | **ALL CORRECT** |
| `t-loop.log` | `test-950/loop.py ext4 200` | **STABLE**，200 次 failures 0 |
| `t-pause.log` / **`ours-pause.log`** | `pause_verify.py`（本次新写，见下），分别用 phz SDK / 我们的 SDK | pause 修复**两次都验证通过** |
| `ours-pause2.log` | 同上，SDK 打了下面那个两行补丁之后 | 路由修好，暴露出更深一层问题 |

另外 `hdbss_evidence.py` 三层取证一致（未落盘）：`KVM_CHECK_EXTENSION(502)=0`、
Firecracker 自报 `kvm-wp`、冷/热写 = 4.56 —— 确认是软件写保护。

### timing.py 的链深表（差分树的核心结论）

| 链深 | create p50 | 深集回退↓ | 深集前滚↑ | 浅集 p50 |
|---|---|---|---|---|
| 5 | 0.064 s | 0.049 s | 0.045 s | 0.037 s |
| 20 | 0.062 s | 0.076 s | 0.072 s | 0.039 s |
| 50 | 0.063 s | 0.124 s | 0.112 s | 0.046 s |

create **不随链深增长**；restore 只跟要跨的纪元数走。

## pause_verify.py（本次新写）

`infra-arm@ecdad325c` / KASandbox_0904 里那个 pause 修复**原本只有 Go 单测**
（`export_layers_test.go`，3 个用例，本次也跑过，全 PASS），没有端到端脚本；
而已有的所有验收脚本都不做 pause，**抓不到这个 bug**。这个脚本补上：

写 16 MiB 随机数据 → checkpoint（写层封存、另开层）→ 再写 16 MiB →
pause / resume → **用 `O_DIRECT` 绕开 guest page cache** 读回来比 sha256。
必须 O_DIRECT：小文件走缓存能把这个 bug 完全盖住，当初第一次验就是这么假通过的。

实测结果：

```
before.bin  写入 adcb455d28c9582a  读回 adcb455d28c9582a
after.bin   写入 1c81366a3bc06793  读回 1c81366a3bc06793
  [PASS] **checkpoint 之前**写的 16MB 在 pause/resume 后完好   ← 这条抓 bug
  [PASS] checkpoint 之后写的 16MB 在 pause/resume 后完好
```

## 遗留问题：`sb.checkpoint.*` 在 `Sandbox.connect()` 得到的对象上不可用

脚本第 6 节（pause/resume 之后再 restore）失败，`missing header`。定位如下：

| 场景 | 结果 |
|---|---|
| `Sandbox.create()` 拿到的对象，`create`/`list`/`restore` | ✅ 全部 OK |
| pause/resume 后用 `Sandbox.connect()` 的新对象，`list`/`restore` | ❌ `missing header` |
| pause/resume 后**用 pause 之前那个对象**，`list` | ✅ OK |
| **我们的 checkpointd SDK** | ❌ 同样失败 —— **不是 phz 版特有的** |

第三行说明**服务端是好的**，问题在 SDK 的 `connect()` 路径。
`missing header` 由 `packages/shared/pkg/proxy/handler.go` 抛出（沙箱代理路由失败）。

代码上看，`py-sdk/e2b/sandbox_sync/main.py` 里：

* `_create` 设了 `X-Access-Token`、`E2b-Sandbox-Id`、`E2b-Sandbox-Port`
* `_cls_connect_sandbox` **只设了 `X-Access-Token`**

这是**上游 / deltabox 既有的 SDK 行为，不是本次交付引入的**，但我们的 checkpoint
功能会因此在 `connect()` 路径上不可用。

**根因已实证确认。** 在 venv 里给 `_cls_connect_sandbox` 补上这两行：

```python
sandbox_headers["E2b-Sandbox-Id"] = sandbox.sandbox_id
sandbox_headers["E2b-Sandbox-Port"] = str(ConnectionConfig.envd_port)
```

错误立刻从 `invalid_argument: missing header` 变成
`not_found: checkpoint ckpt_... not found` —— **路由通了**（见 `ours-pause2.log`）。

### 由此暴露的第二个问题：checkpoint 不跨 pause/resume

补好头之后，restore 报的是"**找不到这个 checkpoint**"。也就是说
**pause/resume 之后，这个沙箱的 checkpoint 账本没有了**。

这一条从安全性上讲不算坏（拒绝，而不是拿着失效的层栈去恢复、造成静默损坏），
但它是一个**没有写进任何文档的行为边界**：用户做了 checkpoint、把沙箱 pause 掉，
resume 回来之后就再也回不到那个 checkpoint 了。

两条都建议跟进：前者两行即可修；后者要么支持、要么明确写进文档。

## 说明

测试期间临时把 920B 的 orchestrator / firecracker 换成了上面那两个二进制，
测完**已全部还原**（原 `8a7a9e8fc280712f` / `9bfa37ab2a6b67fe`），并验证过能
正常建沙箱。原始二进制备份留在服务器 `/root/swap-backup/`。
