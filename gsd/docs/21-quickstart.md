# 21 · 快速上手

## 本章目标

读完本章，你应该能：

1. 把仓库里的 `py-sdk` 装进业务代码用的解释器，并确认 `Sandbox.checkpoint` 可用；
2. 用同步与异步两种写法跑通"起后台进程 → create → 改动 → restore → list → delete"，一棵进程树和两棵进程树各一次；
3. 读懂 `CheckpointInfo` 的每个字段，知道 `restore()` 的耗时要到 `last_timings` 里取；
4. 说清 SDK 是怎么找到沙箱里的 gsd 的，哪些环境变量会改变这条路径；
5. 第一次使用时遇到"进程树之间共享管道被拒""镜像放不下""restore 半途失败"三种情况，知道各自该怎么办。

上一章（[20](20-results-and-gaps.md)）给完了实测结果与缺口，第五部分到此结束。第六部分是实践手册，不再追问"为什么这样设计"，只讲怎么用、怎么部署、怎么运维、出了问题怎么查：
本章与下一章（[22](22-semantics-and-limits.md)）面向在代码里调用 checkpoint / restore 的开发者，[23](23-deployment.md) 到 [26](26-acceptance-runbook.md) 面向部署、运维与验收。
本章只做一件事：把 SDK 装好，跑通第一个例子。回滚的语义与边界在 [22](22-semantics-and-limits.md)，错误码与异常类的完整对照在 [12](12-failure-semantics.md)。

---

## 1. 前提

### 1.1 服务端

宿主已经切到 gsd 这套栈，并且在这套栈下建过模板 `base-gsd`（做法见 [23](23-deployment.md)）。本章示例都用这个模板。

checkpoint 接口由沙箱**里面**的守护进程 gsd 提供，监听 guest 的 49984 端口；宿主上的代理只负责把请求按端口转进去，不参与 checkpoint 本身（请求路径见 [04](04-architecture.md)）。
所以"这个沙箱能不能做 checkpoint"取决于它的模板：只有在 gsd 栈下建出来的模板，里面才有 gsd、补丁内核和 overlay 根文件系统。拿一个普通模板起的沙箱调用本章的接口，`is_running()` 会返回 `False`。

### 1.2 SDK：仓库里的 `py-sdk`

checkpoint 能力不在 PyPI 的 `e2b` 包里，而在代码仓 `py-sdk/` 目录下的这一份（`pyproject.toml` 里的版本号是 `2.19.0`，在上游同版本的基础上增加了 `e2b/gsd/`、`e2b/sandbox*/checkpoint*` 等文件）。
它与整机级回滚手册里用的那套 SDK（`e2b==2.20.0` 加覆盖层）**不是同一份**：两者都提供 `sandbox.checkpoint`，方法名也相近，但线协议的字段、异常类和返回值不同，连到的服务端也不同（一个是 guest 里的 gsd，一个是宿主上的 orchestrator）。
同一个解释器里只能装其中一份。

装法有两种：

```bash
# 做法一：不安装，用 PYTHONPATH 指过去。解释器里要已经有 e2b 的依赖（httpx、httpcore、protobuf 等，
# 列表见 py-sdk/pyproject.toml）。本书全部现场验证用的都是这种做法。
export PYTHONPATH=<代码仓>/py-sdk

# 做法二：装进解释器（需要能取到构建后端 poetry-core；离线环境下这一步未验证）
pip install <代码仓>/py-sdk
```

用哪种都要保证：业务代码用哪个 Python，就让哪个 Python 看到这份 SDK。确认办法是一行导入：

```bash
python3 -c "
import e2b
from e2b import Sandbox, AsyncSandbox, CheckpointInfo, CheckpointRestoreException, NotEnoughSpaceException
print(e2b.__file__)
print('checkpoint SDK ok, port', e2b.ConnectionConfig.checkpointd_port)"
```

第一行输出应当落在 `<代码仓>/py-sdk/e2b/` 下（或做法二的 site-packages 里），最后一行的端口应当是 `49984`。
如果导入 `CheckpointRestoreException` 失败，说明解释器拿到的是 PyPI 的 `e2b` 或整机级那一套，而不是这一份。

### 1.3 连接凭据

SDK 从环境变量读连接配置，和上游 `e2b` 完全一样：

| 变量 | 作用 |
|---|---|
| `E2B_API_KEY` | 建沙箱用的团队 API key |
| `E2B_API_URL` | API 服务地址；不设时是 `http://api.<E2B_DOMAIN>` |
| `E2B_DOMAIN` | 沙箱域名，不设时是 `e2b.app`；沙箱地址 `49984-<沙箱 ID>.<域名>` 要能解析到宿主 |
| `E2B_HTTP_SSL` | 设为 `false` 时不校验证书（见 §6） |

这几项的取值由部署决定，不在本书范围内；值本身（尤其是 API key）不要写进脚本，放在环境变量或 `~/.e2b/config.json` 里。

---

## 2. 第一个例子：一棵进程树

gsd 回滚的是两样东西：沙箱里**用户（uid 1000）的进程**，和**根文件系统**。下面的例子让两者同时变化，再一起退回去。
后台命令是一个 shell 循环，计数器放在 shell 变量里（内存），每拍把当前值追加到一个文件（磁盘）；另外再放两个普通文件作对照，其中一个在 `/tmp` 下。

把下面的内容存成 `gsd_quickstart.py`，设好 §1.3 的环境变量后运行。

```python
import time
from e2b import Sandbox

TEMPLATE = "base-gsd"


def sh(sbx, cmd):
    """以默认用户（uid 1000）跑一条前台命令，返回标准输出。"""
    return sbx.commands.run(cmd).stdout.strip()


sbx = Sandbox.create(template=TEMPLATE, timeout=600)
try:
    # gsd 在不在：GET /health 能通就是 True（degraded 时也是 True，见 §5.1）
    print("gsd running:", sbx.checkpoint.is_running())

    # 一个后台命令 = 一棵用户进程树。计数器 i 在内存里，每 0.2 秒把它追加到文件。
    # timeout=0：不限制这条命令的连接时长（默认 60 秒）。
    sbx.commands.run(
        "sh -c 'i=0; while :; do i=$((i+1)); echo $i >> /home/user/count.log; sleep 0.2; done'",
        background=True,
        timeout=0,
    )
    time.sleep(1)
    sh(sbx, "echo v1 > /home/user/state.txt; echo v1 > /tmp/scratch.txt")

    # ---- checkpoint ----
    cp1 = sbx.checkpoint.create(name="v1")
    n1 = int(sh(sbx, "tail -n 1 /home/user/count.log"))
    print("created:", cp1.checkpoint_id, "trees =", cp1.process_trees,
          "has_process_image =", cp1.has_process_image, "image_bytes =", cp1.image_bytes)
    print("create timings (ms):", cp1.timings)

    # ---- 改动：改文件、删文件、建文件；计数器继续往前走 ----
    sh(sbx, "echo v2 > /home/user/state.txt; rm /tmp/scratch.txt; touch /home/user/new.txt")
    time.sleep(2)
    n2 = int(sh(sbx, "tail -n 1 /home/user/count.log"))

    # ---- restore：进程和文件一起回到 cp1 ----
    ok = sbx.checkpoint.restore(cp1.checkpoint_id)
    print("restore:", ok, "timings (ms):", sbx.checkpoint.last_timings)
    n3 = int(sh(sbx, "tail -n 1 /home/user/count.log"))
    print("counter  at checkpoint ~", n1, " before restore", n2, " after restore", n3)
    print("state.txt  :", sh(sbx, "cat /home/user/state.txt"))            # v1
    print("scratch.txt:", sh(sbx, "cat /tmp/scratch.txt"))                # v1，/tmp 也回滚
    print("new.txt    :", sh(sbx, "test -e /home/user/new.txt && echo exists || echo gone"))  # gone

    time.sleep(1)
    print("counter keeps running:", sh(sbx, "tail -n 1 /home/user/count.log"))

    # ---- 列出与删除 ----
    for info in sbx.checkpoint.list():
        print("list:", info.checkpoint_id, info.name, info.created_at, info.process_trees, info.image_bytes)
    sbx.checkpoint.delete(cp1.checkpoint_id)
    print("left:", [i.name for i in sbx.checkpoint.list()])
finally:
    sbx.kill()   # 沙箱销毁时，里面的全部 checkpoint 一起消失
```

预期看到的要点：

- `create()` 返回的 `process_trees` 是 1，`has_process_image` 是 `True`，`image_bytes` 是这次写出的进程镜像大小；
- restore 之后计数器的值回到 `n1` 附近（比 `n2` 小），并且继续增长——被恢复的是 checkpoint 那一刻的进程，连同它内存里的 `i`；文件末行与内存里的计数器仍然对得上；
- `state.txt` 和 `/tmp/scratch.txt` 读回 `v1`，checkpoint 之后新建的 `new.txt` 不见了；
- 删掉 `cp1` 之后 `list()` 为空。

两处容易忽略的细节。第一，`sh()` 里的前台命令也是 uid 1000 的进程，只是它们在 `create()` 之前就结束了，所以没有被算成一棵树；如果 `create()` 的那一刻有前台命令还在跑，它同样会被保存下来（[22](22-semantics-and-limits.md)）。
第二，被恢复出来的进程不再是原来那个由 envd 拉起的进程：它的进程号不变，但父进程变成了 PID 1，通向 envd 的标准输出管道也接不回去。所以需要在 restore 之后还能看到的输出，应当像例子里这样写进文件，而不是指望 `on_stdout` 回调（[22](22-semantics-and-limits.md)）。

---

## 3. 两棵进程树

每个 `commands.run(..., background=True)` 是一棵独立的进程树。`create()` 保存当时的全部树，`restore()` 把目标 checkpoint 里的全部树恢复出来，接口不变。下面在 §2 的基础上再起一个后台命令（接在 `cp1` 的 restore 之后、删除之前即可）：

```python
    # 第二个后台命令 = 第二棵树
    sbx.commands.run(
        "sh -c 'while :; do date +%s >> /home/user/clock.log; sleep 1; done'",
        background=True,
        timeout=0,
    )
    time.sleep(2)

    cp2 = sbx.checkpoint.create(name="two-trees")
    print("trees =", cp2.process_trees)                              # 2
    print("parent is cp1:", cp2.parent_checkpoint_id == cp1.checkpoint_id)
    print("image_bytes =", cp2.image_bytes, "stopped_ms =", cp2.timings.get("stopped_ms"))

    # 回到 cp1：cp1 里只有计数器那一棵树，cp1 之后才起的时钟进程被杀掉，不会被恢复
    sbx.checkpoint.restore(cp1.checkpoint_id)
    time.sleep(2)
    print("clock.log :", sh(sbx, "test -e /home/user/clock.log && echo exists || echo gone"))  # gone

    # 再到 cp2：两棵树都回来
    sbx.checkpoint.restore(cp2.checkpoint_id)
    time.sleep(2)
    print("clock.log lines:", sh(sbx, "wc -l < /home/user/clock.log"))
```

这里能看到三件事：

- **增量按树做。** 计数器那棵树从 `cp1` 起一直是同一个根进程，`cp2` 里它的镜像只存 `cp1` 之后改过的内存页，所以 `cp2.parent_checkpoint_id` 指向 `cp1`；时钟那棵树是新起的，存全量。只要有一棵树是增量，`parent_checkpoint_id` 就有值（[09](09-checkpoint.md)）。
- **restore 先杀后恢复。** 不管目标 checkpoint 里有什么，当前的用户进程都会被全部杀掉；目标里没有的进程（这里是回到 `cp1` 时的时钟进程）就此消失（[10](10-restore.md)）。
- **restore 不剪枝。** 回到 `cp1` 之后 `cp2` 仍然在，可以再前进到它。历史是一棵树，怎么分支、怎么删见 [22](22-semantics-and-limits.md)。

多棵树时各棵依次保存、依次恢复，耗时随树的数量大致线性增加；树与树之间不是同一瞬间冻住的，也不能共享管道或 socket（§7.1）。这些限制的由来在 [08](08-process-trees.md)。

---

## 4. 异步写法

`AsyncSandbox` 的 `checkpoint` 是 `AsyncCheckpoint`，方法名、参数、返回值与同步版完全相同，只是都要 `await`。下面这段同时演示了第一次使用时最该处理的两个异常：

```python
import asyncio
from e2b import AsyncSandbox, CheckpointRestoreException, NotEnoughSpaceException


async def main():
    sbx = await AsyncSandbox.create(template="base-gsd", timeout=600)
    try:
        if not await sbx.checkpoint.is_running():
            raise RuntimeError("这个模板里没有 gsd")

        await sbx.commands.run(
            "sh -c 'i=0; while :; do i=$((i+1)); echo $i >> /home/user/count.log; sleep 0.2; done'",
            background=True,
            timeout=0,
        )
        await asyncio.sleep(1)

        try:
            cp = await sbx.checkpoint.create(name="step-1")
        except NotEnoughSpaceException as e:
            # 镜像放不下或层栈太深；什么都没改。删掉一些旧 checkpoint 再试（§7.2）
            print("create refused:", e)
            return
        print(cp.checkpoint_id, cp.process_trees, cp.timings)

        await sbx.commands.run("echo scratch > /home/user/tmp.txt")

        try:
            await sbx.checkpoint.restore(cp.checkpoint_id)
        except CheckpointRestoreException as e:
            # 进程已经被杀、restore 没做完；此时不能 create，只能再 restore 一次（§7.3）
            print("restore failed half way:", e)
            await sbx.checkpoint.restore(cp.checkpoint_id)
        print("restore timings (ms):", sbx.checkpoint.last_timings)

        for info in await sbx.checkpoint.list():
            print(info.checkpoint_id, info.name, info.image_bytes)
        await sbx.checkpoint.delete(cp.checkpoint_id)
    finally:
        await sbx.kill()


asyncio.run(main())
```

---

## 5. 接口一览与返回值

### 5.1 五个方法

全部挂在 `sandbox.checkpoint` 下。`request_timeout` 的单位是秒，不传时用连接配置里的默认值（60 秒），传 `0` 表示不限时。

| 方法 | 返回 | 说明 |
|---|---|---|
| `create(name=None, request_timeout=None)` | `CheckpointInfo` | 保存当前全部用户进程树和根文件系统。`name` 只是标签，可以重复，也可以不给 |
| `restore(checkpoint_id, request_timeout=None)` | `bool` | 回到指定 checkpoint。成功时返回 `True`；失败一律抛异常，不会返回 `False` |
| `list(request_timeout=None)` | `List[CheckpointInfo]` | 本沙箱可见的 checkpoint，按创建时间从旧到新 |
| `delete(checkpoint_id, request_timeout=None)` | `bool` | 删除一个 checkpoint。成功时返回 `True`；ID 不存在时抛 `NotFoundException` |
| `is_running(request_timeout=None)` | `bool` | 向 gsd 的 `/health` 发一次 GET。能通返回 `True`，代理回 502（沙箱里没有 gsd 在听）返回 `False` |

关于 `is_running()` 要多说一句：gsd 的 `/health` 在服务降级（degraded）时仍然回 HTTP 200，只是响应体里带原因，所以 `is_running()` 为 `True` 只说明 gsd 进程活着，不说明 create / restore 当前可用。
降级的含义与查看办法见 [12](12-failure-semantics.md) 和 [24](24-configuration-and-observability.md)。

checkpoint 的 ID 由 gsd 生成，形如 `ckpt_<纳秒时间戳>`，只在这一个沙箱里有意义。

### 5.2 `CheckpointInfo`

`create()` 和 `list()` 返回的都是它；下表的"来源"一列说明哪个方法会填这个字段。

| 字段 | 类型 | 来源 | 含义 |
|---|---|---|---|
| `checkpoint_id` | `str` | 两者 | checkpoint 的 ID |
| `name` | `Optional[str]` | 两者 | 创建时给的名字 |
| `created_at` | `Optional[int]` | 两者 | 创建时间，Unix **纳秒**，取的是沙箱里 gsd 的时钟 |
| `has_process_image` | `bool` | 两者 | 是否保存了用户进程。`False` 表示创建时没有用户进程，这个 checkpoint 只有文件系统；restore 到它会杀掉当前用户进程并且不恢复任何进程 |
| `process_trees` | `int` | 两者 | 保存了几棵用户进程树；`has_process_image` 为 `False` 时是 0 |
| `parent_checkpoint_id` | `Optional[str]` | 两者 | 这个 checkpoint 的进程镜像是在哪个 checkpoint 的基础上做的增量；全部树都是全量、或没有进程时为 `None`。父节点可能已经被删除（此时它不在 `list()` 里，但镜像还留着，见 [22](22-semantics-and-limits.md)） |
| `image_bytes` | `int` | 两者 | 这个 checkpoint 自己的进程镜像占用的字节数，不含父镜像。镜像放在 guest 内存里的 tmpfs 上，所以这也是它占掉的 guest 内存 |
| `timings` | `Optional[Dict[str, float]]` | 仅 `create()` | gsd 内各阶段耗时，毫秒。键有 `total_ms`、`find_pids_ms`、`precheck_ms`、`criu_dump_ms`、`overlay_sink_ms`、`stopped_ms`；没有发生的阶段不出现（例如没有用户进程时就没有 `criu_dump_ms`）。键的定义见 [24](24-configuration-and-observability.md) |
| `clear_refs_failed` | `int` | 仅 `create()` | checkpoint 之后有几个进程没能重置脏页标记。非 0 不影响这次 checkpoint 的正确性，只意味着这些进程下一次的增量会偏大（[25](25-troubleshooting.md)） |

### 5.3 `last_timings`

`restore()` 只返回 `bool`，它的分段耗时放在 `sandbox.checkpoint.last_timings` 里：最近一次**成功的** `create()` 或 `restore()` 的各阶段毫秒数，第一次调用之前是 `None`。
restore 的键是 `total_ms`、`find_pids_ms`、`precheck_ms`、`kill_ms`、`overlay_restore_ms`、`criu_restore_ms`。多棵树时 `criu_dump_ms` 与 `criu_restore_ms` 是各棵树之和。

这些数字是 gsd 在 guest 里自己计的，不含网络往返和排队等锁的时间；SDK 一侧的墙钟时间会比 `total_ms` 大。

---

## 6. 连接配置

SDK 怎么找到 gsd，由 `ConnectionConfig.get_checkpointd_url()` 决定，按下面的顺序取第一个有值的：

1. 显式给的 `checkpointd_url`（`Sandbox.create(..., checkpointd_url="...")` 这类连接参数，或环境变量 `E2B_CHECKPOINTD_URL`）；
2. `sandbox_url`（连接参数或 `E2B_SANDBOX_URL`）——**原样使用**，不改端口；
3. 都没有时是 `http://49984-<沙箱 ID>.<沙箱域名>`。

第 2 条需要解释。`sandbox_url` 一般指向一个按请求头路由的代理：它看 `E2b-Sandbox-Id` 和 `E2b-Sandbox-Port` 两个头决定把请求转给哪个沙箱的哪个端口。
SDK 发给 gsd 的请求里，`E2b-Sandbox-Port` 已经是 `49984`，所以同一个代理地址就能到 gsd，不需要也不应该去改 URL 里的端口。
要注意本仓库自带的宿主代理只在本地开发环境下才采用这两个头，其余情况一律按 `Host`（`49984-<沙箱 ID>.<域名>`）解析目标（[04](04-architecture.md)）；所以第 2 条只适用于确实按头路由的代理，经自带代理访问时用第 3 条的地址。
反过来，如果你的 `sandbox_url` 是直接指向某个沙箱 envd 的地址（不经按头路由的代理），那它到不了 gsd，这时要另外设 `checkpointd_url`。

其余几项：

- **协议。** 这个分支的自建部署走明文 HTTP，上面第 3 条生成的地址固定是 `http://`。`E2B_HTTP_SSL=false` 只关闭 httpx 的证书校验，不改变协议。
- **调试模式。** `E2B_DEBUG=true`（或连接参数 `debug=True`）时沙箱地址变成 `localhost:<端口>`，gsd 就是 `http://localhost:49984`；用于把 SDK 指向本机转发出来的端口。
- **鉴权。** gsd 校验请求头 `X-Access-Token`，用的是和 envd 同一个沙箱访问令牌。`Sandbox.create()` 默认建的是 secure 沙箱，SDK 会自动带上这个头，调用方不需要做任何事；用 `Sandbox.connect()` 连到已有沙箱时同样自动处理。用 `allow_public_traffic=False` 创建的沙箱还带一个入口流量令牌，宿主代理要求访问 gsd 的请求带上它；SDK 从提交 `dcca61266` 起自动处理，更早的 SDK 在这类沙箱上所有 checkpoint 调用都会收到 403（[04](04-architecture.md)）。
  非 secure 沙箱（`secure=False`）不要求令牌。鉴权规则见 [04](04-architecture.md)。
- **超时。** `request_timeout` 是 SDK 等响应的时间。它和 gsd 自己给 CRIU 设的时限（每棵树 60 秒）是两回事：后者到期时 gsd 会主动结束这次操作并返回错误。调用方看到的"超时"一共有三种来源（gsd 的保存时限、gsd 读不到沙箱元数据、SDK 的 `request_timeout`），异常类型和含义各不相同，区分办法见 [12](12-failure-semantics.md) 的"SDK 侧的异常映射"一节。
  默认的 60 秒对常见负载足够；进程树多或内存很大时，把 `request_timeout` 调大，避免 SDK 先于 gsd 放弃——SDK 断开并不会让 gsd 停下正在做的 checkpoint。

---

## 7. 第一次用会遇到的三件事

### 7.1 `create()` 被拒：两棵树之间有管道或 socket

现象是 `create()` 抛 `SandboxException`，信息以 `Checkpoint request refused by GSD (failed_precondition):` 开头，后面形如：

```
user process trees t0 (root pid 812) and t1 (root pid 840) share pipe pipe:[10573]
(pid 812 fd 1 writes, pid 840 fd 0 reads); each tree is checkpointed on its own and the
connection would be lost on restore; nothing was changed. Start the connected processes
from one command so they form a single tree
```

原因：每棵树单独保存、单独恢复，树与树之间的匿名管道恢复之后接不回去，而且这种断开是静默的，所以 gsd 在动手之前就拒绝，什么都没有改。
它通常出现在"一条命令里的某个进程脱离了原来的进程树"的时候——比如管道一端的进程被放到后台、原来的 shell 退出后它被 PID 1 收养，于是管道的两端落在了两棵树里。

改法是让相连的进程待在同一棵树里，由同一条命令起、并且让父 shell 等着它们：

```python
# 不行：cat 被放到后台并脱离，和 producer 分属两棵树，中间是一条匿名管道
sbx.commands.run("bash -c '{ producer; } | { ( cat <&0 > /home/user/out.txt & ); }; wait'", background=True, timeout=0)

# 可以：producer 和 consumer 都是同一个 shell 的子进程，是一棵树
sbx.commands.run("sh -c 'producer | consumer > /home/user/out.txt'", background=True, timeout=0)
```

各自独立的后台命令之间如果确实要交换数据，用文件。两棵树之间用 unix socket 相连的情况 gsd 的预检查不出来，由 CRIU 在保存时报错，`create()` 抛 `SandboxException`（信息以 `Code.internal:` 开头，并带 CRIU 日志的尾部），同样什么都没改。

### 7.2 `NotEnoughSpaceException`：放不下

`create()` 抛这个异常有两种原因，信息里写得很清楚：

- `not enough room for checkpoint images: estimated images … snapshot store free …, MemAvailable …`：进程镜像放在 guest 内存里一个 2 GiB 的 tmpfs 上，gsd 在保存之前按进程占用的内存估算镜像大小，tmpfs 剩余空间或 guest 可用内存任何一个不够就拒绝。
  处理办法是 `delete()` 掉不再需要的 checkpoint（每个的占用看 `image_bytes`），或者让沙箱里的进程少占些内存。
- `layer stack too deep`：文件系统每做一次 checkpoint 多一层，层数有上限。处理办法是删掉旧 checkpoint，或者 restore 到较早的 checkpoint 再继续（[22](22-semantics-and-limits.md)）。

两种情况下沙箱都没有被改动，进程照常在跑，可以直接重试。

### 7.3 `CheckpointRestoreException`：restore 半途失败

restore 分两段：先做完所有检查与准备，再杀掉当前用户进程、切文件系统、恢复进程。第一段失败时什么都没动，抛的是普通异常（例如镜像不可用时的 `SandboxException`）；
**杀进程之后**再失败，抛的就是 `CheckpointRestoreException`。这时沙箱处于一个中间状态：原来的用户进程已经没了，信息里会写明文件系统停在旧状态（`filesystem at old state`）还是已经切到了目标（`filesystem at target state`）；多棵树时还会写明哪几棵已经恢复、哪棵失败、哪几棵没有尝试。

此后 gsd 拒绝 `create()`（不能把一个中间状态存成 checkpoint），直到有一次 restore 成功。所以调用方唯一该做的事是**再 restore 一次**——可以是同一个 checkpoint，也可以换一个；成功之后一切恢复正常。
如果反复失败，按 [25](25-troubleshooting.md) 的"restore 返回 data_loss"一条取日志。

---

## 代码位置

| 内容 | 位置 |
|---|---|
| 同步接口 `Checkpoint`：`is_running` / `create` / `restore` / `list` / `delete` | `py-sdk/e2b/sandbox_sync/checkpoint.py:57` / `:85` / `:126` / `:155` / `:191` |
| 异步接口 `AsyncCheckpoint`（同名方法，行号相同） | `py-sdk/e2b/sandbox_async/checkpoint.py` |
| `CheckpointInfo`、`timings_dict`、`process_trees_of` | `py-sdk/e2b/sandbox/checkpoint/types.py:4`、`:62`、`:79` |
| gsd 地址与请求头：`get_checkpointd_url`、`checkpointd_headers` | `py-sdk/e2b/connection_config.py:168`、`:250`；`py-sdk/e2b/gsd/api.py:26` |
| 错误码到异常的映射 `CHECKPOINT_RPC_ERROR_MAP` | `py-sdk/e2b/gsd/api.py:59` |
| `CheckpointRestoreException`、`NotEnoughSpaceException` | `py-sdk/e2b/exceptions.py:122`、`:50` |
| `Sandbox.checkpoint` 属性与构造 | `py-sdk/e2b/sandbox_sync/main.py:96`、`:138` |
| 树间共享管道的拒绝信息 | `CreateCheckpoint`（`ckpt/service.go:163`）、`describe`（`ckpt/shared.go:149`） |

---

## 本章要点

- checkpoint 接口由沙箱里的 gsd 提供，能不能用取决于模板：只有 gsd 栈下建的模板（本书用 `base-gsd`）才有；客户端要用代码仓里的 `py-sdk`，它和整机级回滚用的 SDK 不是同一份，不能混装。
- 五个方法都在 `sandbox.checkpoint` 下，同步与异步的签名相同；`restore()` 与 `delete()` 成功时返回 `True`，失败一律抛异常。
- 被回滚的是 uid 1000 的全部用户进程树和根文件系统（包括 `/tmp`）。每个后台命令是一棵树；restore 先杀掉当前全部用户进程，再恢复目标 checkpoint 里的树。
- `CheckpointInfo.timings` 只有 `create()` 才填；`restore()` 的分段耗时在 `sandbox.checkpoint.last_timings`。这些数字是 gsd 自己计的，不含网络与排队。
- `parent_checkpoint_id` 有值表示进程镜像是增量；`image_bytes` 是这个 checkpoint 占掉的 guest 内存。
- SDK 找 gsd 的顺序是 `checkpointd_url` → `sandbox_url`（原样使用）→ `http://49984-<沙箱 ID>.<域名>`；secure 沙箱的访问令牌由 SDK 自动带上。
- 上手阶段的三种异常各有固定的处理：树间管道被拒就把相连的进程放进同一条命令；`NotEnoughSpaceException` 就删旧 checkpoint；`CheckpointRestoreException` 之后只能先再 restore 一次。
