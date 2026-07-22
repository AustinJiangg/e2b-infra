# 安装e2b-infra

## 环境要求

### 硬件要求

- **节点数量**：至少 4 个节点，作为 4 个集群提供给 api 集群（边缘节点）、build 集群（模板构建节点）、default（沙箱业务节点）、nomad server 集群
- **CPU 架构**：鲲鹏 CPU（需开启虚拟化支持）
- **磁盘空间**：建议 200G 以上

### 软件要求

| 组件 | 版本要求 |
|------|----------|
| runc | >= 1.0.2 |
| docker | >= 25.0.3 |
| containerd | >= v1.7.13 |
| k8s | v1.32.5（Kustomize v5.5.0）|
| PostgreSQL | >= 14 |
| Harbor | - |
| MinIO | xxxxxxxxxx kubectl logs -n e2b -l app=e2b-apikubectl logs -n e2b -l app=e2b-client-proxybash |

**注意**：仅支持 `e2b-2.15.3` 及 `e2b_code_interpreter-2.4.1` 版本，请配套使用。

## 部署/安装XXX（软件或特性名称）

在集群所有节点上安装本 rpm 包，部署工具将被放在 `/opt/e2b-infra`。
支持两种部署模式：**Nomad 形式**和 **K8s 形式**。

### Nomad 形式安装

1. **规划集群**，在集群所有节点上安装 rpm 包

2. **在 server 节点配置环境变量**：

   ```bash
   cp env.template .env
   ```

3. **填写以下配置**

   ```shell
   export SERVER_IPS="{ip1} {ip2}"                     # server 节点 IP，空格分隔
   export NUM_SERVERS=1                                # server 节点个数
   export REGISTRY_URL="{ip}:{port}/{repository_name}" # Harbor 仓库地址
   export POSTGRES_CONNECTION_STRING="postgresql://{username}:{password}@{ip}:{port}/{database_name}?sslmode=disable"
   export HARBOR_HOST="{ip}:{port}"                    # Harbor 地址
   export MINIO_ENDPOINT="{ip}:{port}"                 # MinIO 地址
   export MINIO_ACCESS_KEY="{minio_access_key}"        # MinIO access key
   export MINIO_SECRET_KEY="{minio_access_secret}"     # MinIO secret key
   ```

4. **启动 server 节点**

   ```bash
    bash start-server.sh {当前 node 要使用的 ip}
   ```

    执行后将会自动生成 consul/nomad 的 ACL token 并写入 .env，请妥善保存。

5. **（可选）多 server 节点：**将 .env 分发到各 server 节点并执行上述命令。

6. **启动 client 节点：**将 .env 分发到 client 节点，执行：

   ```bash
   bash start-client.sh {node_pool} {当前 node 要使用的 ip}
   ```

    其中 node_pool 为 api/build/default 三选一。

7. **初始化 client 节点：**属于 build 和 default 的节点执行：

   ```bash
   bash init-client.sh
   ```

8. **部署服务：**在 server 节点执行：

   ```bash
   bash deploy.sh
   ```

   部署完成后，可通过 http://{server_ip}:4646/ui 访问 nomad 查看服务状态。

### K8s 形式安装

1. **规划集群**，在集群所有节点上安装本包，部署工具将被放在 `/opt/e2b-infra`

2. **初始化 client**：属于 build 和 default 的节点执行 `bash init-client.sh`

3. **节点打标签**：

   ```bash
   kubectl label node <nodeName> node-role.kubernetes.io/sandbox=true
   kubectl label node <nodeName> node-role.kubernetes.io/<poolName>=
   ```

   其中 poolName 为 api/build/default。

4. **部署服务**：

   ```bash
   bash deploy.sh --type k8s
   ```

5. **查看状态**：

   ```bash
   kubectl get pods -n e2b
   ```

6. **卸载**：

   ```bash
   helm uninstall e2b-api -n e2b
   ```

## 配置/调测e2b-infra

安装时通过.env配置e2b-infra，重要参数如下：

```shell
# ==========================================
# E2B Infrastructure Environment Variables
# ==========================================

# --- Basic variables ---
# Nomad Server 节点 IP 列表,空格分隔多个IP
export SERVER_IPS="193.12.7.2"

# Nomad Server 节点数量,用于集群仲裁配置
export NUM_SERVERS=1

# Consul 服务版本号
export CONSUL_VERSION=1.21.4

# Nomad 编排器版本号
export NOMAD_VERSION=1.10.4

# 沙箱存储后端: redis | memory
export SANDBOX_STORAGE_BACKEND=redis

# Firecracker 轻量级虚拟化版本,用于隔离沙箱
export FIRECRACKER_VERSION=1.13.1


# --- Database / Redis / Harbor ---
# Harbor 镜像仓库完整地址,包含项目路径
export REGISTRY_URL=193.11.7.2:2900/e2b-orchestration-202609-new

# PostgreSQL 连接字符串,格式: postgresql://user:pass@host:port/db?sslmode=disable
export POSTGRES_CONNECTION_STRING="postgresql://postgres:local@193.11.7.2:5432/mydatabase?sslmode=disable"

# MinIO 对象存储服务端点,S3 兼容存储
export MINIO_ENDPOINT="193.11.7.2:9000"

# MinIO 访问密钥(Access Key)
export MINIO_ACCESS_KEY="minioadmin"

# MinIO 秘密密钥(Secret Key)
export MINIO_SECRET_KEY="minioadmin"


# --- Other secrets ---
# 日志收集器服务地址,用于集中日志采集
export LOGS_COLLECTOR_ADDRESS=localhost:30006

# OpenTelemetry 链路追踪 gRPC 端点,用于分布式追踪
export OTEL_COLLECTOR_GRPC_ENDPOINT=localhost:4317

# 本地集群 API 端点地址
export LOCAL_CLUSTER_ENDPOINT=localhost:3001

# --- ClickHouse (optional) ---
# ClickHouse 数据备份存储桶名称(空表示不备份)
export CLICKHOUSE_BACKUPS_BUCKET_NAME=

# ClickHouse 用户名
export CLICKHOUSE_USERNAME=e2b

# ClickHouse 密码
export CLICKHOUSE_PASSWORD=clickity-clicky-click

# ClickHouse 默认数据库名
export CLICKHOUSE_DATABASE=default

# ClickHouse 服务版本
export CLICKHOUSE_VERSION=25.4

# ClickHouse CPU 资源限制(单位: 毫核,40000=4核)
export CLICKHOUSE_RESOURCES_CPU_COUNT=40000

# ClickHouse 内存资源限制(单位: MB)
export CLICKHOUSE_RESOURCES_MEMORY_MB=40960

# ClickHouse 集群内部通信密钥
export CLICKHOUSE_SERVER_SECRET=123456789

# ClickHouse 服务监听地址
export CLICKHOUSE_HOST=127.0.0.1


# --- Loki / Logs collector ---
# 日志收集器公网 IP,用于外部日志推送
export LOGS_COLLECTOR_PUBLIC_IP=127.0.0.1

# Loki 日志聚合服务版本
export LOGS_COLLECTOR_VERSION=0.50.0-alpine


# --- docker-reverse-proxy ---
# Harbor 仓库地址,用于 Docker 反向代理拉取镜像
export HARBOR_HOST=193.11.7.2:2900

# Harbor 项目名称,通常是 e2b-orchestration
export HARBOR_PROJECT=e2b-orchestration

# Harbor 登录用户名
export HARBOR_USERNAME=admin

# Harbor 登录密码
export HARBOR_PASSWORD=Harbor12345


# --- edge ---
# 客户端代理 CPU 限制(毫核)
export CLIENT_PROXY_RESOURCES_CPU_COUNT=40000

# 客户端代理内存限制(MB)
export CLIENT_PROXY_RESOURCES_MEMORY_MB=40960

# 客户端代理内存硬上限(MB),超出会 OOM
export CLIENT_PROXY_MAX_RESOURCES_MEMORY_MB=61440

# Loki 日志服务地址,用于边缘日志上报
export LOKI_URL=127.0.0.1:3100

# API 服务 gRPC 监听地址
export API_GRPC_ADDRESS=localhost:5009


# --- loki ---
# Loki 数据存储桶名称
export LOKI_BUCKET_NAME=default

# Loki CPU 资源限制(毫核)
export LOKI_RESOURCES_CPU_COUNT=40000

# Loki 代理内存限制(MB)
export LOKI_PROXY_RESOURCES_MEMORY_MB=40960

# Loki 代理内存硬上限(MB)
export LOKI_PROXY_MAX_RESOURCES_MEMORY_MB=61440

# Loki 服务版本
export LOKI_VERSION=2.9.3


# --- orchestrator ---
# 环境构建超时时间,控制模板构建最大时长
export ENVD_TIMEOUT=60s

# 沙箱模板存储桶名称,存放 Firecracker 根文件系统
export TEMPLATE_BUCKET_NAME=e2b-dev-fc-templates

# 是否允许沙箱访问互联网,true 表示允许出站连接
export ALLOW_SANDBOX_INTERNET=true


# --- otel-collector ---
# OpenTelemetry 收集器 CPU 限制(毫核)
export OTEL_COLLECTOR_RESOURCES_CPU_COUNT=40000

# OTel 收集器代理内存限制(MB)
export OTEL_COLLECTOR_PROXY_RESOURCES_MEMORY_MB=40960

# OTel 收集器内存硬上限(MB)
export OTEL_COLLECTOR_PROXY_MAX_RESOURCES_MEMORY_MB=61440

# Grafana 用户名(用于远程写入)
export GRAFANA_USERNAME=

# Grafana OTEL 收集器令牌(用于远程认证)
export GRAFANA_OTEL_COLLECTOR_TOKEN=

# Grafana OTLP 远程写入端点 URL
export GRAFANA_OTLP_URL=

# OpenTelemetry 收集器版本
export OTEL_COLLECTOR_VERSION=0.119.0


# --- redis ---
# Redis 服务端口标识符
export REDIS_PORT_NAME=redis

# Redis 服务镜像版本
export REDIS_VERSION=7.4.4-alpine


# --- template-manager ---
# 模板构建缓存存储桶名称,存放 Docker 构建缓存层
export BUILD_CACHE_BUCKET_NAME=e2b-dev-fc-cache


# --- Ports ---
# API HTTP 服务端口
export API_PORT=3000

# API gRPC 服务端口,用于内部高性能通信
export API_GRPC_PORT=5009

# 边缘 API HTTP 端口
export EDGE_API_PORT=3001

# 边缘代理端口,客户端 SDK 连接端口
export EDGE_PROXY_PORT=3002

# 边缘健康检查端口
export EDGE_HEALTH_PORT=3003

# 编排器 gRPC 端口,用于沙箱调度
export ORCHESTRATOR_PORT=5008

# 编排器代理端口
export ORCHESTRATOR_PROXY_PORT=5007

# 模板管理服务端口
export TEMPLATE_MANAGER_PORT=5008

# Redis 服务监听端口
export REDIS_PORT=6379

# Docker 反向代理服务端口,用于镜像拉取代理
export DOCKER_REVERSE_PROXY_PORT=5000

# Loki 日志服务监听端口
export LOKI_SERVICE_PORT=3100

# 日志代理服务端口
export LOGS_PROXY_PORT=30006

# 日志健康检查代理端口
export LOGS_HEALTH_PROXY_PORT=44313

# OpenTelemetry gRPC 接收端口
export OTEL_COLLECTOR_GRPC_PORT=4317

# ClickHouse 服务端口
export CLICKHOUSE_SERVER_PORT=9010

# ClickHouse 指标暴露端口
export CLICKHOUSE_METRICS_PORT=9363

# 内部 DNS 服务端口,用于服务发现
export DNS_PORT=5353


# API 服务部署的节点池标签,用于 Nomad/K8s 节点选择
export API_NODE_POOL=api

# ClickHouse 服务部署的节点池标签
export CLICKHOUSE_NODE_POOL=api

# 构建服务部署的节点池标签,通常是有大磁盘和 Docker 的节点
export BUILD_NODE_POOL=build
```
