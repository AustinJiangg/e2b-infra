# checkpointd —— 沙箱 checkpoint / restore 的 SDK 层

给沙箱做**状态快照**并**回滚**：内存、vCPU、设备状态、磁盘，一次性存下来，
之后可以把沙箱原样放回那一刻。

## 它到底跑在哪

这一点最容易误会，先说清楚：

**沙箱里没有任何守护进程。** SDK 把请求发到沙箱地址上的 49984 端口，
但这个端口**由宿主上的 orchestrator 截下来自己应答**，不会转发进沙箱。
原因很直接：做一次 checkpoint 要暂停虚机、驱动 hypervisor 的快照接口，
这两件事沙箱内部谁也干不了。

所以：

- `is_running()` / `is_available()` 探的是**宿主侧那个服务**，不是沙箱内的什么进程。
  沙箱活着它就返回 True。
- checkpoint 存在**宿主本地**，跟着沙箱生死走 —— 沙箱销毁，checkpoint 一起没。
  这跟 E2B 的 snapshot API 不是一回事，后者是持久化的。
- 用的是独立端口和独立 proto，envd 一行代码都没改。

## 模块结构

```
e2b/
├── checkpointd/                      # 底层通信
│   ├── api.py                        # /health 路由常量 + HTTP 错误映射
│   └── checkpoint/
│       ├── checkpoint_pb2.py         # protobuf 消息
│       ├── checkpoint_pb2.pyi        # 类型桩
│       └── checkpoint_connect.py     # Connect-RPC 客户端
├── sandbox/checkpoint/types.py       # 用户面向类型：CheckpointInfo
├── sandbox_sync/checkpoint.py        # 同步 Checkpoint
├── sandbox_async/checkpoint.py       # 异步 AsyncCheckpoint
├── connection_config.py              # checkpointd_port=49984 + get_checkpointd_url()
└── sandbox/main.py                   # SandboxBase 增加 checkpointd_api_url
```

RPC 错误映射复用 `e2b.envd.rpc.handle_rpc_exception()`；
认证复用沙箱自己的 traffic access token（已经在公共 header 里，
**不需要**额外的 Authorization header）。

## API

### `CheckpointInfo`

| 字段 | 类型 | 说明 |
|---|---|---|
| `checkpoint_id` | `str` | checkpoint ID |
| `name` | `Optional[str]` | 创建时指定的名称 |
| `created_at` | `Optional[int]` | 创建时间戳（秒） |
| `mem_mode` | `Optional[str]` | `"incremental"` / `"full"`，见下 |

**`mem_mode` 是这一版新加的，值得专门看一眼。**
`"incremental"` 表示这次只写了自上次 checkpoint 以来被改动的内存页；
`"full"` 表示把整份 guest 内存都拷了一遍。

沙箱的**第一次** checkpoint 没有基线可比，必然是 `"full"`，这是正常的。
但如果**之后**还出现 `"full"`，说明宿主没有在跟踪脏页 —— 每次 checkpoint 都在
做全量拷贝，慢一到两个数量级、空间也多花几十倍，而且**不会报任何错**。
这个字段就是调用方唯一能自己发现这件事的途径。

服务端不上报这个字段时为 `None`（老版本兼容）。

### 方法

| 方法 | 返回 | 说明 |
|---|---|---|
| `create(name=None, request_timeout=None)` | `CheckpointInfo` | 建一个 checkpoint |
| `restore(checkpoint_id, request_timeout=None)` | `bool` | 回滚到指定 checkpoint |
| `list(request_timeout=None)` | `List[CheckpointInfo]` | 列出本沙箱的全部 checkpoint |
| `delete(checkpoint_id, request_timeout=None)` | `bool` | 删除一个 checkpoint |
| `is_available(request_timeout=None)` | `bool` | checkpoint 接口是否应答 |
| `is_running(request_timeout=None)` | `bool` | 同上，旧名字，为兼容保留 |

同步：`sandbox.checkpoint.create()`；异步：`await sandbox.checkpoint.create()`。

用法示例见 e2b-infra 仓库的
`e2b-deploy/dep/e2b-sdk-checkpoint/使用说明.md`（部署后在 `/opt/e2b-infra/dep/e2b-sdk-checkpoint/`）。

## 重新生成

正路：

```bash
make generate-checkpointd      # buf + protoc-gen-connect-python
```

没有那套工具链时，`scripts/regen-checkpoint-pb.py` 能就地改序列化描述符，
只改真正要改的部分，不会把 buf managed 模式塞进去的那些选项弄丢。
