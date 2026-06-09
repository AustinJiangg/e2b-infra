# 安全管理

## 访问控制

### ACL Token 管理

Nomad 和 Consul 使用 ACL Token 进行访问控制：

- `CONSUL_ACL_TOKEN`：Consul 的 ACL token，用于服务发现
- `NOMAD_ACL_TOKEN`：Nomad 的 ACL token，用于任务调度

Token 在 `start-server.sh` 执行时自动生成并写入 `.env` 文件。

### API 认证

- `E2B_ACCESS_TOKEN`：格式 `sk_e2b_xxx`，用于 API认证
- `E2B_API_KEY`：格式 `e2b_xxx`，用于 API 调用

## 网络安全

### 端口管理

需确保以下端口安全：

| 端口 | 服务 | 说明 |
|------|------|------|
| 4646 | Nomad UI | 限制管理员 IP 访问 |
| 3000 | E2B API | 开放给应用服务器 |
| 3002 | Client Proxy | 开放给终端用户 |
| 5432 | PostgreSQL | 仅限集群内部访问 |
| 9000 | MinIO | 仅限集群内部访问 |

## 镜像安全

- Harbor 仓库应配置 HTTPS 和访问控制
- 建议使用项目隔离，如 `e2b-orchestration`

## 数据安全

### PostgreSQL

使用强密码和 SSL 连接：

```bash
export POSTGRES_CONNECTION_STRING="postgresql://{username}:{password}@{ip}:{port}/{database_name}?sslmode=disable"
```

### MinIO

配置访问密钥和 secret key：

```bash
export MINIO_ACCESS_KEY="{minio_access_key}"
export MINIO_SECRET_KEY="{minio_access_secret}"
```

## 沙箱安全

- 沙箱运行于隔离的 Docker 容器中
- 每个沙箱有独立的资源限制（cpu_count、memory_mb）
- 用户权限限制：`user` 用户具有 sudo 权限（NOPASSWD），但运行在隔离环境
