# 维护特性

## 升级软件

仅支持卸载重装

## 卸载软件

### Nomad 形式卸载

1. 在 server 节点停止服务部署
2. 在各节点停止 consul 和 nomad 服务：
3. 执行rpm删除e2b-infra包

### K8s 形式卸载

```bash
helm uninstall e2b-api -n e2b
```

## 查询命令

**Nomad 模式**：

- 访问 `http://{server_ip}:4646/ui` 查看各个服务状态
- 使用 `nomad status` 查看节点状态

**K8s 模式**：

```bash
kubectl get pods -n e2b
```

### 查看沙箱状态

调用sdk查询，创建沙箱并查询示例：

```bash
import os
import time
from dotenv import load_dotenv
load_dotenv()
from e2b import Sandbox

sbx = Sandbox.create("basev7")
print(f"沙箱ID: {sbx.sandbox_id}")
print(f"沙箱URL: {sbx.get_host(80)}\n")

try:
    paginator = Sandbox.list()
    sandboxes = paginator.next_items()
    
    if sandboxes:
        print(f"\n当前共有 {len(sandboxes)} 个运行中的沙箱：\n")
        for idx, sb in enumerate(sandboxes, 1):
            marker = " <-- 刚创建的沙箱" if sb.sandbox_id == sbx.sandbox_id else ""
            print(f"[{idx}] 沙箱ID: {sb.sandbox_id}{marker}")
            print(f"    完整信息: {sb}")
            print(f"    __dict__: {sb.__dict__}")
            print()
    else:
        print("\n当前没有运行中的沙箱\n")
        
except Exception as e:
    print(f"查询沙箱列表时出错: {e}")
    import traceback
    traceback.print_exc()

sbx.kill()
```

## 收集日志

### Nomad 模式日志收集

- 查看特定服务日志：`nomad logs -job e2b-api`
- 访问nomad web页面：http://{server_ip}:4646/ui ，通过web UI查询日志

### K8s 模式日志收集

通过kubectl命令查询，示例：

```bash
kubectl logs -n e2b -l app=e2b-api
kubectl logs -n e2b -l app=e2b-client-proxy
```
