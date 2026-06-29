# e2b-infra

## 介绍

[E2B](https://e2b.dev/ ) 是一套开源的 AI 代码解释执行基础设施。在我们的主仓库 [e2b-dev/e2b](https://github.com/e2b-dev/E2B ) 中，我们提供了 SDK 和 CLI，帮助你自定义和管理运行环境，并在云端部署和运行 AI 代理。

本仓库则包含了支撑整个 E2B 平台运行的底层基础设施代码。

## 部署包内容快照（e2b-deploy/）

`e2b-deploy.tar.gz` 是 RPM 构建真正使用的部署包（见 `e2b-infra.spec` 的 `Source9`），
必须保持压缩包形式。但压缩包在 GitHub 上是不可读的二进制，每次更新别人看不出里面改了什么。

为此，仓库里额外放一份它的**可读目录** `e2b-deploy/`，专门用来展示压缩包里的文本内容。
开发时每次上传新的 `e2b-deploy.tar.gz`，顺手同步更新一下这个目录——这样 git 记录里
既有新的压缩包，又能在目录上一眼看清这次改了什么。

该目录是压缩包内容的镜像，只是有意省略了两样东西：

- `dep/ubuntu-22.04-custom.tar.gz`：约 70MB 的二进制大文件，没必要进仓库（仍保留在压缩包内供构建使用）；
- 压缩包内自带的 `.git/`：二进制、无法 diff，且若保留会让 `e2b-deploy/` 被 git 当成子模块、导致里面文件不被跟踪。

（这两项已在 `.gitignore` 中忽略，更新目录时不会被误提交。）

### 安装教程

#### 前提

- 自建Postpres数据库(请使用postgre 使用14或14以上版本)

- 自建Harbor仓库

- 自建minio数据库

- 至少4个节点，作为4个集群，提供给api集群（边缘节点）、build集群（模板构建节点）、default（沙箱业务节点）nomad server集群

- 仅支持`e2b-2.20.0`及`e2b_code_interpreter-2.4.1`版本，请配套使用

- host内核及BIOS需开启虚拟化支持

- runc版本>=1.0.2

- docker版本>=25.0.3

- k8s版本：

  Client Version: v1.32.5
  Kustomize Version: v5.5.0
  Server Version: v1.32.5

- containerd >= v1.7.13

- 鲲鹏CPU

- 建议200G以上磁盘空间,1.5TB以上内存,为隔离沙箱网络,沙箱发往10.*;169.254.*;172.16.*;127.*;192.168.*网段的请求将被拒绝;



#### nomad形式安装

1. 规划好集群，在集群所有节点上安装本rpm包，部署工具将被放在 `/opt/e2b-infra`


2. 在server节点拷贝参数模板并填写好需要自定义的环境变量`cp env.template .env`

   ```
   export SERVER_IPS="{ip1} {ip2}"                     #填写当前集群server节点的ip，空格分隔（server节点为nomad的server端运行所在的节点）
   export NUM_SERVERS=1                                #填写当前集群server节点的个数
   export REGISTRY_URL="{ip}:{port}/{repository_name}" #填写harbor仓库地址
   export POSTGRES_CONNECTION_STRING="postgresql://{username}:{password}@{ip}:{port}/{database_name}?sslmode=disable"  #填写postgres数据库地址
   export HARBOR_HOST="{ip}:{port}"                    #填写harbor仓库地址
   export MINIO_ENDPOINT = "{ip}:{port}"               #填写minio地址
   export MINIO_ACCESS_KEY = "{minio_access_key}"      #填写minio的access_key
   export MINIO_SECRET_KEY = "{minio_access_secret}"   #填写minio的access_secret
   ```



3. 在server节点执行`bash start-server.sh {当前node要使用的ip}`，将为server节点安装配置consul/nomad、生成consul/nomad的ACL token，启动consul/nomad，consul/nomad的ACL token会写入到.env中

   ```
   export CONSUL_ACL_TOKEN=xxx
   export NOMAD_ACL_TOKEN=xxx
   ```



4. （可选）若有多个nomad server节点，将.env文件分发到各个nomad server节点的/opt/e2b-infra下，并执行`bash start-server.sh {当前node要使用的ip}`，将为nomad server节点安装配置consul/nomad，启动consul/nomad，并自动加入consul/nomad集群。

5. 将.env文件分发到各个client节点，并执行`bash start-client.sh {当前节点所属node_pool} {当前node要使用的ip}` （node_pool为上述api/default/build三选一），将为client节点安装配置consul/nomad，启动consul/nomad，并自动加入consul/nomad集群。

6. 属于build和default的节点执行`bash init-client.sh`，将为自动为沙箱业务节点配置提供沙箱服务的各项配置。

7. 集群启动完成后在server节点执行`bash deploy.sh`，将自动下载镜像，自动打包镜像，并为集群拉起各个依赖服务，注入初始用户。

8. 此时集群已经部署完成，可通过http://{server_ip}:4646/ui访问nomad查看各个服务状态。

#### k8s形式安装

1. 规划好集群，在集群所有节点上安装本包，部署工具将被放在 `/opt/e2b-infra`
2. 属于build和default的节点执行`bash init-client.sh`，将为自动为沙箱业务节点配置提供沙箱服务的各项配置。
3. 为所有给E2B使用的节点打标签，命令：`kubectl label node <nodeName> node-role.kubernetes.io/sandbox=true`
4. 为节点打标签(api/build/default)，命令：`kubectl label node <nodeName> node-role.kubernetes.io/<poolName>=`
5. 执行`bash deploy.sh --type k8s`，将自动下载镜像，自动打包镜像，并为集群拉起各个依赖服务，注入初始用户。
6. 此时集群已经部署完成，可通过`kubectl get pods -ne2b`查看各个服务状态。
7. 若需卸载执行：`helm uninstall e2b-api -n e2b`

#### 与集群交互

安装python依赖

```js
pip install e2b==2.20.0
pip install e2b_code_interpreter==2.4.1
python3 /opt/e2b-infra/patch_e2b.py
```

创建 `.env` 文件：

```env
E2B_ACCESS_TOKEN="sk_e2b_xxx"                                  #E2B的access_token
E2B_API_KEY="e2b_xxx"                                          #E2B的api_key
E2B_DOMAIN="xxx"                                               #对应E2B的client-proxy的3002端口的域名
E2B_API_URL="http://<your_ip>:3000"                            #对应E2B的api的3000端口的域名
E2B_HTTP_SSL="false"
```

##### 构建模板

示例代码

```
# build_prod.py
from dotenv import load_dotenv
load_dotenv()
import os

from e2b import Template, default_build_logger

if __name__ == '__main__':
    Template.build(
        #基础沙箱模板示例代码
        Template().from_dockerfile('FROM harbor:443/e2b-orchestration/ubuntu:22.04-custom'),
        #代码沙箱模板示例代码
        #Template().from_dockerfile('FROM harbor:443/e2b-orchestration/code-interpreter:v1').set_start_cmd("sudo /root/.jupyter/start-up.sh", wait_for_url("http://localhost:49999/health")),
        alias="base",
        cpu_count=1,
        memory_mb=1024,
        on_build_logs=default_build_logger()
    )
```



##### **启动沙箱**

使用沙箱执行命令（示例代码）

```python
from dotenv import load_dotenv
load_dotenv()
import os

from e2b import Sandbox

sbx = Sandbox.create("basev61")
print(sbx.commands.run("whoami"))  # guest
print(sbx.commands.run("pwd"))  # /home/guest

sbx.kill()
```

示例：

```
[root@hostname-api home]# python3 test.py 
CommandResult(stderr='', stdout='user\n', exit_code=0, error='')
CommandResult(stderr='', stdout='/home/user\n', exit_code=0, error='')
```

##### 代码沙箱

使用沙箱执行命令（示例代码）

```
from dotenv import load_dotenv
load_dotenv()
import os
from e2b_code_interpreter import Sandbox

sbx = Sandbox.create("code-interpreter-v9")
execution = sbx.run_code('print("Hello, world!")')
print(execution)
print(sbx.kill())
```

示例：

```
[root@hostname-api home]# python3 run_code.py
Execution(Results: [], Logs: Logs(stdout: ['Hello, world!\n'], stderr: []), Error: None)
```

##### 构建模板用的镜像

拉取官方镜像模板：

```
git clone https://github.com/e2b-dev/code-interpreter.git
```

在code-interpreter/template下创建Dockerfile：

```
FROM python:3.12
USER root
WORKDIR /root
ENV JAVA_VERSION=11
ENV PIP_DEFAULT_TIMEOUT=100 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 JUPYTER_CONFIG_PATH=/root/.jupyter IPYTHON_CONFIG_PATH=/root/.ipython SERVER_PATH=/root/.server JAVA_HOME=/usr/lib/jvm/jdk-${JAVA_VERSION} IJAVA_VERSION=1.3.0 DENO_INSTALL=/opt/deno DENO_VERSION=v2.4.0 R_VERSION=4.5.*
RUN apt-get update && DEBIAN_FRONTEND=noninteractive DEBCONF_NOWARNINGS=yes apt-get install -y build-essential curl git util-linux jq sudo fonts-noto-cjk ca-certificates
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
RUN apt-get update && DEBIAN_FRONTEND=noninteractive DEBCONF_NOWARNINGS=yes apt-get install -y nodejs
COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
RUN ipython kernel install --name 'python3' --user
RUN npm install -g --unsafe-perm git+https://github.com/e2b-dev/ijavascript.git
RUN ijsinstall --install=global
COPY server .server
RUN python -m venv .server/.venv
RUN .server/.venv/bin/pip install --no-cache-dir -r .server/requirements.txt
COPY matplotlibrc .config/matplotlib/.matplotlibrc
COPY start-up.sh .jupyter/start-up.sh
RUN chmod +x .jupyter/start-up.sh
COPY jupyter_server_config.py .jupyter/
RUN mkdir -p .ipython/profile_default/startup
COPY ipython_kernel_config.py .ipython/profile_default/
COPY startup_scripts .ipython/profile_default/startup
RUN apt-get -q update && \
    DEBIAN_FRONTEND=noninteractive DEBCONF_NOWARNINGS=yes \
    apt-get -qq -o=Dpkg::Use-Pty=0 install -y --no-install-recommends \
    systemd systemd-sysv openssh-server sudo chrony linuxptp socat curl
RUN useradd -m user
RUN mkdir -p /home/user
RUN chown -R user:user /home/user
RUN echo 'user ALL=(ALL) NOPASSWD: ALL' >> /etc/sudoers
USER user
WORKDIR /home/user
ENTRYPOINT ["sudo", "/root/.jupyter/start-up.sh"]
```

构建镜像：

```
docker build -t code-interpreter:v1 .
```



#### 依赖安装

##### (可选)1.无网环境准备

若为离线环境，需手动下载以下依赖并上传到离线环境

1、对应操作系统安装unzip jq tar docker rsync dnsmasq的依赖包

2、E2B管理集群组件

```
wget https://releases.hashicorp.com/consul/1.21.4/consul_1.21.4_linux_arm64.zip
wget https://releases.hashicorp.com/nomad/1.10.4/nomad_1.10.4_linux_arm64.zip
wget https://github.com/firecracker-microvm/firecracker/releases/download/v1.13.1/firecracker-v1.13.1-aarch64.tgz
```

3、E2B组件镜像

```
clickhouse-server:25.4
timberio/vector:0.50.0-alpine
grafana/loki:2.9.3
otel/opentelemetry-collector-contrib:0.119.0
redis:7.4.4-alpine
```

4、制作模板过程中沙箱会联网下载依赖软件，无网环境需提前下载好依赖制作成ubuntu:22.04-custom镜像

```
# 拉取官方镜像（本地已有可跳过）
docker pull ubuntu:22.04

# 运行一个可交互容器，安装过程需要网络
docker run -it --name u22-manual ubuntu:22.04 bash

# 0. 避免 apt 交互
export DEBIAN_FRONTEND=noninteractive

# 1. 更新源
apt-get update

# 2. 一次性安装题目要求的全部组件
apt-get install -y \
        systemd systemd-sysv \
        openssh-server \
        sudo \
        chrony \
        linuxptp \
        socat \
        curl

# 3. 可选：做些清理减小体积
apt-get clean
rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# 容器内退出
exit

# 把刚才的容器保存为本地镜像
docker commit u22-manual ubuntu:22.04-custom

# 保存成单个 tar 文件，方便分发/备份
docker save ubuntu:22.04-custom | gzip > ubuntu-22.04-custom.tar.gz

docker rm u22-manual      # 删除临时容器

#推送到对应的镜像仓
docker tag ubuntu:22.04-custom 193.67.6.2:2900/ubuntu:22.04-custom
docker login 193.67.6.2:2900
docker push 193.67.6.2:2900/ubuntu:22.04-custom
```



##### 2.docker安装

```
# 1. 安装docker
yum install -y docker

# 2. 下载官方 v2.23.3 插件（arm64）
curl -Lo /tmp/docker-compose \
  https://github.com/docker/compose/releases/download/v2.23.3/docker-compose-linux-aarch64

# 3. 安装到 docker cli 插件目录
mkdir -p /usr/local/lib/docker/cli-plugins
install -m 755 /tmp/docker-compose /usr/local/lib/docker/cli-plugins/docker-compose

# 4. 验证
docker compose version
# 输出 Docker Compose version v2.23.3 即可

# 5. 配置国内代理
cat /etc/docker/daemon.json 
{
  "registry-mirrors": [
    "https://docker.1ms.run",
    "https://docker.m.daocloud.io",
    "https://hub-mirror.c.163.com",
    "https://mirror.baidubce.com",
    "https://ccr.ccs.tencentyun.com"
  ]
}

# 6.应用配置
systemctl daemon-reload
systemctl restart docker
```



##### 3.postgres准备

```
#拉取对应的postgres镜像后启动
docker run -d --name postgres -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=local -e POSTGRES_DB=mydatabase  -p 5432:5432 --health-cmd="pg_isready -U postgres" --health-interval=5s --health-timeout=2s --health-retries=5 postgres:latest
```

##### （可选）4.minio准备

```
#下载二进制并安装
wget https://dl.min.io/server/minio/release/linux-arm64/minio 
chmod +x minio
sudo mv minio /usr/local/bin/



#设置minio磁盘
mkdir -p /root/data/minio



#写minio配置文件
sudo tee /etc/default/minio << 'EOF'
MINIO_DIR=/root/data/minio
MINIO_OPTS="--console-address :9001"
MINIO_ROOT_USER=minioadmin
MINIO_ROOT_PASSWORD=minioadmin
MINIO_SERVER_URL="http://192.168.2.108:9000" #注意改为你的节点的url
EOF



#写systemctl配置文件
sudo tee /etc/systemd/system/minio.service << 'EOF'
[Unit]
Description=MinIO
Documentation=https://docs.min.io  
Wants=network-online.target
After=network-online.target
AssertFileIsExecutable=/usr/local/bin/minio

[Service]
WorkingDirectory=/usr/local
EnvironmentFile=-/etc/default/minio
ExecStartPre=/bin/bash -c "if [ -z \"${MINIO_DIR}\" ]; then echo \"Variable MINIO_DIR not set in /etc/default/minio\"; exit 1; fi"
ExecStart=/usr/local/bin/minio server $MINIO_DIR --address :9000 --console-address :9001

# Let systemd restart this service always
Restart=always

# Specifies the maximum file descriptor number that can be opened by this process
LimitNOFILE=65536

# Specifies the maximum number of threads this process can create
TasksMax=infinity

# Disable timeout logic and wait until process is stopped
TimeoutStopSec=infinity
SendSIGKILL=no

[Install]
WantedBy=multi-user.target
EOF



#启动minio
sudo systemctl daemon-reload
sudo systemctl enable minio
sudo systemctl start minio
```

##### 5.harbor镜像仓准备

```
wget -c "https://github.com/wise2c-devops/build-harbor-aarch64/releases/download/v2.13.0/harbor-offline-installer-aarch64-v2.13.0.tgz"
tar -zxvf harbor-offline-installer-aarch64-v2.13.0.tgz

cd harbor

#修改harbor.yml，注释掉https相关内容，修改hostname为你的环境的ip
cp harbor.yml.tmpl harbor.yml

bash install.sh
```

##### 6.harbor镜像仓加入docker的insecure-registries

```
mkdir -p /etc/docker/
vi /etc/docker/daemon.json
{
  "insecure-registries": ["193.11.7.2:2900"]
}
systemctl daemon-reload
systemctl restart docker
```

##### 7.harbor镜像仓反向代理

E2B构建镜像时只接受https链接镜像仓，而手动建立的harbor镜像为http方式启动，此时需要使用nginx做一层反代解决该问题

安装nginx

```
yum install nginx
```

生成证书

```bash
# 1. 建目录
mkdir -p /etc/nginx/ssl

# 2. 生成自签名证书（有效期 365 天，CN 匹配 harbor）
cat > /etc/nginx/ssl/harbor.cnf <<EOF
[req]
default_bits = 2048
prompt = no
default_md = sha256
distinguished_name = dn
req_extensions = v3_req

[dn]
CN = harbor

[v3_req]
subjectAltName = @alt_names

[alt_names]
DNS.1 = harbor
EOF
  
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
-keyout /etc/nginx/ssl/harbor.key \
-out /etc/nginx/ssl/harbor.crt \
-config /etc/nginx/ssl/harbor.cnf \
-extensions v3_req

# 3. 重载配置
nginx -t && nginx -s reload
```

nginx配置：

```
[root@hostname-b8ode ~]# cat /etc/nginx/nginx.conf
user nginx;
worker_processes auto;
error_log /var/log/nginx/error.log notice;
pid /run/nginx.pid;

# Load dynamic modules. See /usr/share/doc/nginx/README.dynamic.
include /usr/share/nginx/modules/*.conf;

events {
    worker_connections 1024;
}

http {
    client_max_body_size 2G;
    log_format  main  '$remote_addr - $remote_user [$time_local] "$request" '
                      '$status $body_bytes_sent "$http_referer" '
                      '"$http_user_agent" "$http_x_forwarded_for"';

    access_log  /var/log/nginx/access.log  main;

    sendfile            on;
    tcp_nopush          on;
    keepalive_timeout   65;
    types_hash_max_size 4096;

    include             /etc/nginx/mime.types;
    default_type        application/octet-stream;

    # Load modular configuration files from the /etc/nginx/conf.d directory.
    # See http://nginx.org/en/docs/ngx_core_module.html#include
    # for more information.
    include /etc/nginx/conf.d/*.conf;

    server {
        listen       80;
        listen       [::]:80;
        server_name  _;
        root         /usr/share/nginx/html;

        # Load configuration files for the default server block.
        include /etc/nginx/default.d/*.conf;

        error_page 404 /404.html;
        location = /404.html {
        }

        error_page 500 502 503 504 /50x.html;
        location = /50x.html {
        }
    }
    server {
        listen 443 ssl;
        server_name harbor;
    
        ssl_certificate     /etc/nginx/ssl/harbor.crt;   # 自签即可
        ssl_certificate_key /etc/nginx/ssl/harbor.key;
    
        location / {
            proxy_pass http://127.0.0.1:2900;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto https;
    	}
	}
}
```

拷贝证书到需要访问的机器：

```
scp /etc/nginx/ssl/harbor.crt root@171.1.3.3:/etc/docker/certs.d/harbor:443/ca.crt
```

启动template-manger时加上环境变量：

```
SSL_CERT_FILE = "/etc/docker/certs.d/harbor:443/ca.crt"
```

若是k8s环境还需要挂载：
```
volumes:
  - name: harbor-ca-cert
    hostPath:
      path: /etc/docker/certs.d/harbor:443/ca.crt
      type: File

volumeMounts:
  - name: harbor-ca-cert
    mountPath: /etc/docker/certs.d/harbor:443/ca.crt
    readOnly: true
```

##### （k8s形式部署需要）8.K8s安装

安装依赖

```bash
dnf update -y
dnf install -y conntrack socat ipvsadm ipset curl tar
modprobe ip_vs ip_vs_rr ip_vs_wrr ip_vs_sh nf_conntrack
```

下载 KubeKey

```bash
export KKZONE=cn
curl -sfL https://get-kk.kubesphere.io | VERSION=v3.1.10 sh -
chmod +x kk
```

生成并修改

```bash
./kk create config --with-kubernetes v1.32.5 --name k8s-arm64
```

修改字段

```
spec:
  hosts:
  - {name: hostname-jdtbo.foreman.pxe, address: 171.1.3.2, internalAddress: 171.1.3.2, user: root, password: "Huawei12#$", arch: arm64} #当前hostname、节点ip、用户名、密码、架构
  roleGroups:
    etcd:
    - hostname-jdtbo.foreman.pxe #当前hostname
    control-plane:
    - hostname-jdtbo.foreman.pxe #当前hostname
    worker:
    - hostname-jdtbo.foreman.pxe #当前hostname

```

安装

```bash
export KKZONE=cn
./kk create cluster -f config-k8s-arm64.yaml
```

若coredns报错

```
/etc/resolv.conf 中只保留一条nameserver 127.0.0.1

#让 kubelet 重新生成 coredns 的 resolv.conf
kubectl delete pod -n kube-system -l k8s-app=kube-dns
```

##### （k8s形式部署需要）9.containerd镜像仓配置

写入私有镜像仓到containerd配置

```
cat >>/etc/containerd/config.toml <<'EOF'

[plugins."io.containerd.grpc.v1.cri".registry.mirrors."{ip}:{port}"]
  endpoint = ["http://{ip}:{port}"]

[plugins."io.containerd.grpc.v1.cri".registry.configs."{ip}:{port}".auth]
  username = "admin"
  password = "Harbor12345"
EOF

```

重启contained

```bash
systemctl daemon-reload
systemctl restart containerd
```

验证

```bash
crictl pull {ip}:{port}/e2b-orchestration/api:latest
# 成功即列表可见
crictl images | grep e2b-orchestration
```



### 参与贡献

1.  Fork 本仓库
2.  新建 Feat_xxx 分支
3.  提交代码
4.  新建 Pull Request


### 特技

1.  使用 Readme\_XXX.md 来支持不同的语言，例如 Readme\_en.md, Readme\_zh.md
2.  Gitee 官方博客 [blog.gitee.com](https://blog.gitee.com )
3.  你可以 [https://gitee.com/explore ](https://gitee.com/explore ) 这个地址来了解 Gitee 上的优秀开源项目
4.  [GVP](https://gitee.com/gvp ) 全称是 Gitee 最有价值开源项目，是综合评定出的优秀开源项目
5.  Gitee 官方提供的使用手册 [https://gitee.com/help ](https://gitee.com/help )
6.  Gitee 封面人物是一档用来展示 Gitee 会员风采的栏目 [https://gitee.com/gitee-stars/ ](https://gitee.com/gitee-stars/ )
