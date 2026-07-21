#!/bin/bash

# ===================== 基础配置 =====================
WORK_DIR=$(cd $(dirname "$0") && pwd)  # 修正：转为绝对路径，避免相对路径问题
DEP_DIR="$WORK_DIR/dep"
E2B_DIR="/opt/e2b-infra"
# 定义默认端口（可通过参数覆盖，后续扩展）
PG_PORT=5432
MINIO_PORT=9000
MINIO_CONSOLE_PORT=9001
HARBOR_HTTP_PORT=2900
NOMAD_PORT=4646
# Harbor 登录凭证（根据实际情况修改）
HARBOR_USER="admin"
HARBOR_PASSWORD="Harbor12345"

# Nomad/Consul 健康检查端口
NOMAD_HTTP_PORT=4646
CONSUL_HTTP_PORT=8500
source $DEP_DIR/.env
HOST_IP=$SERVER_IP
NOMAD_API_URL=$SERVER_IP:$NOMAD_PORT
# ===================== 颜色输出函数（增强可读性）=====================
info() {
    echo -e "\033[34mℹ️ $1\033[0m"
}
success() {
    echo -e "\033[32m✅ $1\033[0m"
}
error() {
    echo -e "\033[31m❌ $1\033[0m"
    exit 1
}
warn() {
    echo -e "\033[33m⚠️ $1\033[0m"
}

#function download_packages() {
    # 下载harbor
    
    # 下载nomad

    # 下载consul

    # 下载docker


#}

# ===================== 安装函数 =====================
function yum_install() {
    yum install -y curl unzip jq tar rsync
    yum install -y dnsmasq
}
function install_postgre() {
    info "开始安装 PostgreSQL..."
    # 检查镜像文件是否存在
    if [ ! -f "$DEP_DIR/postgres.tar" ]; then
        error "PostgreSQL 镜像文件不存在：$DEP_DIR/postgres.tar"
    fi
    
    # 停止并删除已有容器（避免冲突）
    if docker ps -a --format "{{.Names}}" | grep -q "^postgres$"; then
        warn "检测到已有 postgres 容器，先停止并删除"
        docker stop postgres || true
        docker rm postgres || true
    fi
    
    # 启动容器（添加 --restart=always 保证开机自启）
    docker run -d --name postgres \
        -e POSTGRES_USER=postgres \
        -e POSTGRES_PASSWORD=local \
        -e POSTGRES_DB=mydatabase \
        -p "$PG_PORT:$PG_PORT" \
        --restart=always \
        --health-cmd="pg_isready -U postgres" \
        --health-interval=5s \
        --health-timeout=2s \
        --health-retries=5 \
        postgres:latest || error "启动 PostgreSQL 容器失败"
    
    # 等待健康检查通过
    info "等待 PostgreSQL 健康检查通过..."
    for ((i=0; i<10; i++)); do
        if docker inspect --format '{{.State.Health.Status}}' postgres | grep -q "healthy"; then
            success "PostgreSQL 安装并启动成功！端口：$PG_PORT，访问地址：postgresql://postgres:local@$HOST_IP:$PG_PORT/mydatabase"
            return 0
        fi
        sleep 2
    done
    error "PostgreSQL 健康检查超时！"
}



function install_docker() {
    info "开始安装 Docker..."
    # 检查依赖文件
    if [ ! -f "$DEP_DIR/docker-compose" ] || [ ! -f "$DEP_DIR/docker-25.0.5.tgz" ]; then
        error "Docker 依赖文件缺失，请检查 $DEP_DIR 目录"
    fi
    
    # 安装 docker-compose（修正为标准路径 /usr/local/bin/）
    cp -f "$DEP_DIR/docker-compose" /usr/local/bin/ || error "复制 docker-compose 失败"
    chmod +x /usr/local/bin/docker-compose || error "添加 docker-compose 执行权限失败"
    
    # 解压并安装 docker
    tar -xvf "$DEP_DIR/docker-25.0.5.tgz" -C "$DEP_DIR" || error "解压 docker 包失败"
    cp -f "$DEP_DIR/docker/"* /usr/bin/ || error "复制 docker 二进制文件失败"
    
    cp -fv $DEP_DIR/daemon.json /etc/docker

    # 重启 docker 服务
    systemctl daemon-reload || error "daemon-reload 失败"
    systemctl restart docker || error "重启 docker 服务失败"
    systemctl enable docker || warn "设置 docker 开机自启失败（非致命）"
    
    # 验证 docker 是否正常
    if docker --version >/dev/null 2>&1; then
        success "Docker 安装成功！版本：$(docker --version | awk '{print $3}')"
    else
        error "Docker 安装后验证失败"
    fi

    # 加载镜像
    for file in $DEP_DIR/*.tar; do
        if [ -f "$file" ]; then
            echo "正在加载镜像: $file"
            docker load -i "$file"
        fi
    done
    for file in $DEP_DIR/*.tar.gz; do
        if [ -f "$file" ]; then
            echo "正在加载镜像: $file"
            docker load -i "$file"
        fi
    done
}

function install_minio() {
    info "开始安装 MinIO..."
    # 检查依赖文件
    if [ ! -f "$DEP_DIR/minio" ] || [ ! -f "$DEP_DIR/minio.yml" ] || [ ! -f "$DEP_DIR/minio.service" ]; then
        error "MinIO 依赖文件缺失，请检查 $DEP_DIR 目录"
    fi
    
    # 安装 minio 二进制文件
    cp -f "$DEP_DIR/minio" /usr/local/bin || error "复制 minio 二进制文件失败"
    chmod +x /usr/local/bin/minio || error "添加 minio 执行权限失败"
    
    # 创建数据目录
    mkdir -p /root/data/minio || error "创建 MinIO 数据目录失败"
    
    # 复制配置文件
    cp -f "$DEP_DIR/minio.yml" /etc/default/minio.yml || error "复制 minio 配置文件失败"
    cp -f "$DEP_DIR/minio.service" /etc/systemd/system/minio.service || error "复制 minio 服务文件失败"
    
    # 启动并设置自启
    systemctl daemon-reload || error "daemon-reload 失败"
    systemctl enable minio || warn "设置 minio 开机自启失败（非致命）"
    systemctl start minio || error "启动 minio 服务失败"
    
    # 健康检查：MinIO 监听 0.0.0.0，走 127.0.0.1 探测即可；--noproxy 防止
    # http_proxy 环境变量把探测请求劫持到代理（表现为 504）
    info "等待 MinIO 健康检查通过..."
    HEALTH_CHECK=""
    for ((i=0; i<10; i++)); do
        HEALTH_CHECK=$(curl -s -o /dev/null -w "%{http_code}" --noproxy '*' --connect-timeout 2 "http://127.0.0.1:$MINIO_PORT/minio/health/ready")
        if [ "$HEALTH_CHECK" = "200" ]; then
            break
        fi
        sleep 2
    done
    
    if [ "$HEALTH_CHECK" = "200" ]; then
        success "MinIO 安装并启动成功！健康检查通过（HTTP 200）"
        info "控制台访问地址：http://$HOST_IP:$MINIO_CONSOLE_PORT"
        return 0
    else
        error "MinIO 启动失败！健康检查返回码：$HEALTH_CHECK\n建议查看日志：journalctl -u minio -f"
    fi
}

function install_harbor() {
    info "开始安装 Harbor..."
    # 检查安装包
    HARBOR_TAR="$DEP_DIR/harbor-offline-installer-aarch64-v2.13.0.tgz"
    if [ ! -f "$HARBOR_TAR" ]; then
        error "Harbor 安装包不存在：$HARBOR_TAR"
    fi
    
    # 解压安装包
    tar -xvf "$HARBOR_TAR" -C "$WORK_DIR" || error "解压 Harbor 安装包失败"
    cd "$WORK_DIR/harbor" || error "进入 Harbor 目录失败"
    
    # 生成配置文件
    cp -f harbor.yml.tmpl harbor.yml || error "生成 harbor.yml 失败"
    
    # 注释 HTTPS 配置 + 修改 hostname 为本机IP + 修改 HTTP 端口
    sed -i.bak '/^https:/ s/^/#/' harbor.yml
    sed -i.bak '/^  port: 443/ s/^/#/' harbor.yml
    sed -i.bak '/^  certificate: / s/^/#/' harbor.yml
    sed -i.bak '/^  private_key: / s/^/#/' harbor.yml
    sed -i.bak "s/^hostname: .*/hostname: $HOST_IP/" harbor.yml  # 改为本机IP
    sed -i.bak "/^  port: [0-9]\+/ s/[0-9]\+$/$HARBOR_HTTP_PORT/" harbor.yml
    rm -f harbor.yml.bak  # 删除临时备份 
}

function install_nginx() {
    info "开始安装 Nginx..."
    # 安装 nginx（CentOS/RHEL 系）
    yum install -y nginx || error "YUM 安装 Nginx 失败"
    
    # 创建 SSL 目录
    mkdir -p /etc/nginx/ssl || error "创建 Nginx SSL 目录失败"
    
    # 复制配置文件
    if [ ! -f "$DEP_DIR/harbor.cnf" ] || [ ! -f "$DEP_DIR/nginx.conf" ]; then
        error "Nginx 配置文件缺失，请检查 $DEP_DIR 目录"
    fi
    cp -f "$DEP_DIR/harbor.cnf" /etc/nginx/ssl/ || error "复制 harbor.cnf 失败"
    cp -f "$DEP_DIR/nginx.conf" /etc/nginx/nginx.conf || error "复制 nginx.conf 失败"

    # 生成 SSL 证书（CN 改为本机IP）
    openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
        -keyout /etc/nginx/ssl/harbor.key \
        -out /etc/nginx/ssl/harbor.crt \
        -config /etc/nginx/ssl/harbor.cnf \
        -extensions v3_req || error "生成 SSL 证书失败"

    # ↓↓↓ 新增：把自签证书装进系统信任库（E2B 构建器读这里，不读 /etc/docker/certs.d）
    cp -f /etc/nginx/ssl/harbor.crt /etc/pki/ca-trust/source/anchors/harbor-ca.crt || error "复制证书到系统信任库失败"
    update-ca-trust extract || error "更新系统 CA 信任库失败"

    # docker 信任目录（保留，docker daemon 用）
    mkdir -p /etc/docker/certs.d/harbor:443
    cp -f /etc/nginx/ssl/harbor.crt /etc/docker/certs.d/harbor:443/ca.crt

    # 启动并验证 Nginx
    systemctl start nginx || error "启动 Nginx 失败"
    systemctl enable nginx || warn "设置 Nginx 开机自启失败（非致命）"
    if nginx -t >/dev/null 2>&1; then
        nginx -s reload || warn "重载 Nginx 配置失败（非致命）"
        success "Nginx 安装并启动成功！访问地址：http://$HOST_IP"
    else
        error "Nginx 配置文件校验失败：$(nginx -t 2>&1)"
    fi
}

# 兼容 yum/dnf 安装 e2b-infra
function install_e2b() {
    info "开始安装 e2b-infra..."
    pip install e2b==2.20.0
    pip install e2b_code_interpreter==2.4.1
    pip install python-dotenv

    # 单机替换相关文件
    local E2B_DIR="/opt/e2b-infra"
    cp -fv $DEP_DIR/install-nomad.sh /opt/e2b-infra
    cp -fv $DEP_DIR/install-consul.sh /opt/e2b-infra
    cp -fv $DEP_DIR/uninstall-nomad.sh /opt/e2b-infra
    cp -fv $DEP_DIR/uninstall-consul.sh /opt/e2b-infra

    cp -fv $DEP_DIR/consul_1.21.4_linux_arm64.zip /tmp/consul.zip
    cp -fv $DEP_DIR/nomad_1.10.4_linux_arm64.zip /tmp

    cp -fv "$DEP_DIR/.env" "$E2B_DIR/.env"
    cp -fv "$DEP_DIR/template-manager.hcl" "$E2B_DIR/nomad/template-manager.hcl"
    cp -fv "$DEP_DIR/start-client.sh" "$E2B_DIR/start-client.sh"
    cp -fv $DEP_DIR/start-server.sh $E2B_DIR/start-server.sh
    cp -fv $DEP_DIR/init-client.sh $E2B_DIR/init-client.sh
    cp -fv $DEP_DIR/run-nomad.sh $E2B_DIR/run-nomad.sh
    cp -fv $DEP_DIR/run-consul.sh $E2B_DIR/run-consul.sh
    cp -fv $DEP_DIR/deploy.sh $E2B_DIR/deploy.sh

    # 给 SDK 打补丁（路径按你的python版本调整！）
    SITE=$(python3 -c "import e2b,os;print(os.path.dirname(os.path.dirname(e2b.__file__)))")
    echo $SITE
    cp -fv $DEP_DIR/code_interpreter_sync.py $SITE/e2b_code_interpreter/
    cp -fv $DEP_DIR/connection_config.py     $SITE/e2b/
    cp -fv $DEP_DIR/dockerfile_parser.py     $SITE/e2b/template/
    cp -fv $DEP_DIR/build_api.py             $SITE/e2b/template_sync/
    cp -fv $DEP_DIR/main.py                  $SITE/e2b/template_sync/

    python /opt/e2b-infra/patch_e2b.py

    if ! grep -q "address=/.e2b.app/127.0.0.1" /etc/dnsmasq.conf; then
        echo "address=/.e2b.app/127.0.0.1" >> /etc/dnsmasq.conf
        echo "配置已添加"
        systemctl restart dnsmasq
    else
        echo "配置已存在，无需重复添加"
    fi
}

# ===================== 卸载函数 =====================
function uninstall_postgres() {
    info "开始卸载 PostgreSQL..."
    # 容器化部署，无需 pkill，仅操作容器
    docker stop postgres || true
    docker rm postgres || true
    docker rmi postgres:latest || true
    pkill postgres
    success "PostgreSQL 卸载完成"
}

function uninstall_docker() {
    info "开始卸载 Docker..."
    dnf remove docker -y
    # systemctl stop docker || true
    # rm -rf /usr/bin/docker* /usr/local/bin/docker-compose  /usr/bin/containerd* /usr/bin/ctr /usr/bin/runc || true
    success "Docker 卸载完成"
}

function uninstall_minio() {
    info "开始卸载 MinIO..."
    systemctl stop minio || true
    systemctl disable minio || true
    rm -rf /usr/local/bin/minio /etc/default/minio /etc/systemd/system/minio.service /root/data/minio || true
    success "MinIO 卸载完成"
}

function uninstall_harbor() {
    info "开始卸载 Harbor..."
    # Harbor 是容器集群，无需 pkill
    cd "$WORK_DIR/harbor" || warn "Harbor 目录不存在，跳过卸载"
    docker-compose down -v || true
    rm -rf "$WORK_DIR/harbor" || true
    success "Harbor 卸载完成"
}

function uninstall_nginx() {
    info "开始卸载 Nginx..."
    systemctl stop nginx || true
    pkill nginx
    yum remove -y nginx || true
    rm -rf /etc/nginx /var/log/nginx || true
    success "Nginx 卸载完成"
}

function uninstall_nomad() {
    info "开始卸载 Nomad/Consul..."
    local uninstall_nomad_sh="/opt/e2b-infra/uninstall-nomad.sh"
    local uninstall_consul_sh="/opt/e2b-infra/uninstall-consul.sh"
    
    if [ -f "$uninstall_nomad_sh" ]; then
        bash "$uninstall_nomad_sh" --force || warn "卸载 Nomad 失败"
    else
        warn "Nomad 卸载脚本不存在：$uninstall_nomad_sh"
    fi
    
#  pkill consul || warn "未找到 Consul 进程"  # 修正拼写错误
    if [ -f "$uninstall_consul_sh" ]; then
        bash "$uninstall_consul_sh" --force || warn "卸载 Consul 失败"
    else
        warn "Consul 卸载脚本不存在：$uninstall_consul_sh"
    fi
    pkill nomad
    pkill consul
}

function uninstall_e2b() {
    uninstall_nomad
    rpm -e e2b-infra
}

# ===================== 主函数 =====================
# 校验 .env 里的 SERVER_IP 确实是本机地址。配错时各组件健康检查会把请求发到外网
# （若 shell 配了 http_proxy 还会收到代理的 504），报错极具误导性，故在此提前拦截。
function check_host_ip() {
    if ! ip -o -4 addr show | grep -q "inet $HOST_IP/"; then
        error "SERVER_IP=$HOST_IP 不是本机地址！请修改 $DEP_DIR/.env 中的 SERVER_IP 为本机实际 IP（可用 ip -4 addr 查看），再重新执行安装"
    fi
}

function install() {
    info "===== 开始批量安装组件（本机IP：$HOST_IP）====="
    check_host_ip
    yum_install
    # unzip_package
    # iptables -F
    setenforce 0
    # install_docker
    install_postgre
    install_minio
    install_harbor
    install_nginx
    install_e2b
    success "===== 所有组件安装完成 ====="
}
download_packages() {
    local arch=$1  # 使用local限定变量作用域，更规范
    local pkg_dir=$DEP_DIR

    # 1. 校验参数和目录
    if [ -z "$arch" ]; then
        echo "错误：必须传入架构参数（x86/arm64）"
        return 1
    fi
    if [ ! -d "$pkg_dir" ]; then
        echo "目录 $pkg_dir 不存在，正在创建..."
        mkdir -p "$pkg_dir" || { echo "创建目录失败"; return 1; }
    fi

    # 2. 根据架构下载对应包（修复条件判断+补充文件名）
    if [ "$arch" == "x86" ]; then
        echo "开始下载 x86_64 架构软件包..."
        wget -q --show-progress https://download.docker.com/linux/static/stable/x86_64/docker-24.0.5.tgz -O "$pkg_dir/docker-24.0.5.tgz" || { echo "docker下载失败"; return 1; }
        wget -q --show-progress https://github.com/docker/compose/releases/download/v2.40.2/docker-compose-linux-x86_64 -O "$pkg_dir/docker-compose-linux-x86_64" || { echo "docker-compose下载失败"; return 1; }
        wget -q --show-progress https://releases.hashicorp.com/nomad/1.10.4/nomad_1.10.4_linux_amd64.zip -O "$pkg_dir/nomad_1.10.4_linux_amd64.zip" || { echo "nomad下载失败"; return 1; }
        wget -q --show-progress https://releases.hashicorp.com/consul/1.21.4/consul_1.21.4_linux_amd64.zip -O "$pkg_dir/consul_1.21.4_linux_amd64.zip" || { echo "consul下载失败"; return 1; }
    elif [ "$arch" == "arm64" ]; then  # 明确标注arm64，更易读
        echo "开始下载 aarch64/arm64 架构软件包..."
        wget -q --show-progress https://download.docker.com/linux/static/stable/aarch64/docker-24.0.5.tgz -O "$pkg_dir/docker-24.0.5.tgz" || { echo "docker下载失败"; return 1; }
        wget -q --show-progress https://github.com/docker/compose/releases/download/v2.40.2/docker-compose-linux-aarch64 -O "$pkg_dir/docker-compose-linux-aarch64" || { echo "docker-compose下载失败"; return 1; }
        wget -q --show-progress https://releases.hashicorp.com/nomad/1.10.4/nomad_1.10.4_linux_arm64.zip -O "$pkg_dir/nomad_1.10.4_linux_arm64.zip" || { echo "nomad下载失败"; return 1; }
        wget -q --show-progress https://releases.hashicorp.com/consul/1.21.4/consul_1.21.4_linux_arm64.zip -O "$pkg_dir/consul_1.21.4_linux_arm64.zip" || { echo "consul下载失败"; return 1; }
    else
        echo "错误：不支持的架构 $arch，仅支持 x86/arm64"
        return 1
    fi

    echo "所有软件包下载完成，保存路径：$pkg_dir"
    return 0
}
function install_x86() {
    info "===== 开始批量安装组件（本机IP：$HOST_IP）====="

}

function uninstall() {
    info "===== 开始批量卸载组件 ====="
    uninstall_e2b
    uninstall_nginx
    uninstall_harbor
    # uninstall_minio
    uninstall_postgres
    #uninstall_docker
    success "===== 所有组件卸载完成 ====="
}


# ===================== 追加 Nomad 客户端配置到 default.hcl =====================
function append_nomad_client_config() {
    local nomad_config_file="/etc/nomad.d/default.hcl"
    local node_pool_name="api"  # 可根据实际需求修改节点池名称

    # Nomad's client network fingerprint enumerates EVERY host network interface
    # and probes each one's link speed. On a shared host with thousands of veth
    # interfaces (e.g. a neighbour running hundreds of containers) that walk takes
    # minutes, and the HTTP API (4646) only opens after the client finishes — so
    # `wait_for_port 4646` appears to hang. Two mitigations:
    #   - network_interface: pin the primary NIC (holding HOST_IP), skip the
    #     `ip route` auto-detect of the default interface.
    #   - network_speed: hardcode the link speed so Nomad SKIPS the per-interface
    #     ethtool/sysfs speed probe (the part that scales with interface count).
    local net_if net_if_line=""
    net_if=$(ip -o -4 addr show 2>/dev/null | awk -v ip="$HOST_IP" '$4 ~ "^"ip"/"{print $2; exit}')
    [ -n "$net_if" ] && net_if_line="  network_interface = \"$net_if\""

    # 1. 定义要追加的配置内容（替换变量）
    local client_config=$(cat <<EOF
# === 自动追加的客户端配置 ===
client {
enabled = true
node_pool = "$node_pool_name"
$net_if_line
  network_speed = 1000
meta {
    node_pool = "$node_pool_name"
}
}

plugin "raw_exec" {
config {
    enabled = true
}
}
# === 客户端配置结束 ===
EOF
)

    # 2. 检查配置是否已存在（避免重复追加）
    if grep -q "# === 自动追加的客户端配置 ===" "$nomad_config_file" 2>/dev/null; then
        info "Nomad 客户端配置已存在于 $nomad_config_file，跳过追加"
        return 0
    fi

    # 3. 确保配置文件目录存在
    mkdir -p "$(dirname "$nomad_config_file")" || error "创建 Nomad 配置目录失败"

    # 4. 追加配置到文件末尾
    info "追加 Nomad 客户端配置到 $nomad_config_file..."
    echo -e "\n$client_config" >> "$nomad_config_file" || error "追加配置到 $nomad_config_file 失败"
}

# ===================== 通用等待端口启动函数 =====================
# 函数名：wait_for_port
# 功能：等待指定端口启动（监听状态），直到端口可用或超时
# 参数说明：
#   $1 - 目标端口号（必填，如4646）
#   $2 - 端口类型（可选，默认tcp，支持tcp/udp/all）
#   $3 - 检查间隔（可选，默认2秒，单位：秒）
#   $4 - 超时时间（可选，默认300秒，0表示永不超时，单位：秒）
# 返回值：
#   0 - 端口成功启动
#   1 - 超时/参数错误
# 使用示例：
#   wait_for_port 4646 tcp 2 300  # 等待TCP 4646端口，间隔2秒，超时300秒
#   wait_for_port 8080 udp 1 0    # 等待UDP 8080端口，间隔1秒，永不超时
# =================================================================
wait_for_port() {
    # 解析参数（设置默认值）
    local TARGET_PORT=${1:?"参数错误：必须指定目标端口号！"}  # 必填参数，无则报错
    local PORT_TYPE=${2:-tcp}
    local CHECK_INTERVAL=${3:-2}
    local TIMEOUT_SECONDS=${4:-300}

    # 校验参数合法性
    if ! [[ "$TARGET_PORT" =~ ^[0-9]+$ ]]; then
        echo "❌ 错误：端口号必须是数字（当前值：$TARGET_PORT）"
        return 1
    fi
    if ! [[ "$PORT_TYPE" =~ ^(tcp|udp|all)$ ]]; then
        echo "❌ 错误：端口类型只能是 tcp/udp/all（当前值：$PORT_TYPE）"
        return 1
    fi
    if ! [[ "$CHECK_INTERVAL" =~ ^[0-9]+$ ]]; then
        echo "❌ 错误：检查间隔必须是数字（当前值：$CHECK_INTERVAL）"
        return 1
    fi
    if ! [[ "$TIMEOUT_SECONDS" =~ ^[0-9]+$ ]]; then
        echo "❌ 错误：超时时间必须是数字（当前值：$TIMEOUT_SECONDS）"
        return 1
    fi

    # 初始化变量
    local start_time=$(date +%s)
    local elapsed_time=0

    # 内部函数：检查端口监听状态
    check_port_listening() {
        local port=$1
        local type=$2
        case $type in
            tcp)  ss -tln | grep -q ":$port\b" ;;
            udp)  ss -uln | grep -q ":$port\b" ;;
            all)  ss -tuln | grep -q ":$port\b" ;;
        esac
        return $?
    }

    # 打印启动信息
    echo "========================================"
    echo "开始等待 $PORT_TYPE 端口 $TARGET_PORT 启动..."
    echo "检查间隔：$CHECK_INTERVAL 秒 | 超时时间：${TIMEOUT_SECONDS:-永不超时} 秒"
    echo "========================================"

    # 循环检查端口
    while true; do
        # 检查端口是否已启动
        if check_port_listening "$TARGET_PORT" "$PORT_TYPE"; then
            echo -e "\n✅ 端口 $TARGET_PORT ($PORT_TYPE) 已成功启动！"
            return 0
        fi

        # 超时判断（0表示永不超时）
        if [ "$TIMEOUT_SECONDS" -ne 0 ]; then
            local current_time=$(date +%s)
            elapsed_time=$((current_time - start_time))
            if [ "$elapsed_time" -ge "$TIMEOUT_SECONDS" ]; then
                echo -e "\n❌ 超时错误：等待 $TIMEOUT_SECONDS 秒后，端口 $TARGET_PORT ($PORT_TYPE) 仍未启动！"
                return 1
            fi
            # 打印等待进度
            echo -n "⏳ 已等待 $elapsed_time 秒，端口仍未启动... "
            echo "剩余超时时间：$((TIMEOUT_SECONDS - elapsed_time)) 秒"
        else
            echo "⏳ 端口未启动，继续等待...（永不超时）"
        fi

        # 等待指定间隔后重试
        sleep "$CHECK_INTERVAL"
    done
}

function start() {
    info "开始启动 e2b-infra 服务（本机IP：$HOST_IP）..."
    
    # 检查关键目录/文件（增加错误处理）
    [ ! -d "$E2B_DIR" ] && error "e2b-infra 目录不存在：$E2B_DIR"
    [ ! -f "$DEP_DIR/.env" ] && error "env 文件缺失：$DEP_DIR/.env"
    

    # iptables -F
    cd $WORK_DIR/harbor
    bash install.sh || error "Harbor 安装脚本执行失败"

    # prepare 生成的部分配置属主可能是 root，容器内 UID 10000 读不了
    chown -R 10000:10000 "$WORK_DIR/harbor/common/config/registry" \
                         "$WORK_DIR/harbor/common/config/nginx" || true
    docker-compose -f "$WORK_DIR/harbor/docker-compose.yml" restart registry proxy || true

    # 进入 E2B_DIR 并处理错误
    cd "$E2B_DIR" || error "进入 $E2B_DIR 目录失败"
    
    # 执行启动脚本（使用本机IP）
    
    success "Harbor 安装成功！访问地址：http://$HOST_IP:$HARBOR_HTTP_PORT"

    info "启动 Nomad 服务端..."
    bash "$E2B_DIR/start-server.sh" "$HOST_IP" || error "启动 Nomad 服务端失败"
    
    append_nomad_client_config
    
    # 重启nomad服务
    systemctl restart nomad

    
    info "启动 Nomad 客户端..."
    bash "$E2B_DIR/start-client.sh" api "$HOST_IP" || error "启动 Nomad 客户端失败"
    
    # info "初始化 Nomad 客户端..."
    bash "$E2B_DIR/init-client.sh" || error "初始化客户端失败"

    wait_for_port 4646 tcp 1 0
    
    # Docker 登录 Harbor（带凭证，失败时提示）
    info "登录 Harbor 仓库：$HOST_IP:$HARBOR_HTTP_PORT..."
    if ! docker login -u "$HARBOR_USER" -p "$HARBOR_PASSWORD" "$HOST_IP:$HARBOR_HTTP_PORT"; then
        warn "Harbor 登录失败（可能凭证错误或Harbor未就绪），继续执行部署..."
    fi
    info "创建项目"
    curl -X POST "http://localhost:2900/api/v2.0/projects"   -k -u "admin:Harbor12345"   -H "Content-Type: application/json"   -d '{
    "project_name": "e2b-orchestration",
    "public": true}'

    # 删掉服务
    rm -fv $E2B_DIR/bin/orchestrator.Dockerfile
    info "执行部署脚本..."
    bash "$E2B_DIR/deploy.sh" || error "执行部署脚本失败"
    # iptable_clean

    # -w 等待锁；-C 检查是否存在；|| 不存在就添加
    iptables -w -t nat -C PREROUTING -p tcp --dport 80 -j REDIRECT --to-port 3002 2>/dev/null \
    || iptables -w -t nat -A PREROUTING -p tcp --dport 80 -j REDIRECT --to-port 3002
    iptables -w -t nat -C OUTPUT -p tcp -o lo --dport 80 -j REDIRECT --to-port 3002 2>/dev/null \
    || iptables -w -t nat -A OUTPUT -p tcp -o lo --dport 80 -j REDIRECT --to-port 3002
    success "e2b-infra 服务启动完成！所有组件健康检查通过✅"
}

function iptable_clean() {
    source /opt/e2b-infra/.env
    NOMAD_TOKEN=$NOMAD_ACL_TOKEN

    JOBS=$(nomad job status -token "$NOMAD_TOKEN" -json | jq -r '.[].Allocations[].JobID')
    for job in $JOBS; do
        nomad job stop -token "$NOMAD_TOKEN" "$job"
    done
    iptables -F
    systemctl restart docker
    bash $WORK_DIR/harbor/install.sh || error "Harbor 安装脚本执行失败"
    bash "$E2B_DIR/deploy.sh" || error "执行部署脚本失败"
}

function deploy() {
    info "执行部署脚本..."
    bash "$E2B_DIR/deploy.sh" || error "执行部署脚本失败"
}

function redeploy_job() {
    info "快速重跑 nomad job：$1（只 render+重跑该 job，跳过镜像构建与 DB 初始化）..."
    bash "$E2B_DIR/deploy.sh" --only "$1" || error "重跑 job $1 失败"
}

function stop() {
    info "开始停止 e2b-infra 服务..."
    local E2B_DIR="/opt/e2b-infra"
    
    # 检查目录是否存在
    if [ ! -d "$E2B_DIR" ]; then
        warn "e2b-infra 目录不存在：$E2B_DIR，跳过停止操作"
        return 0
    fi
    
    cd "$E2B_DIR" || error "进入 $E2B_DIR 目录失败"
    bash "$E2B_DIR/uninstall-consul.sh" --force || warn "停止 Consul 失败（非致命）"
    bash "$E2B_DIR/uninstall-nomad.sh" --force || warn "停止 Nomad 失败（非致命）"

    # 兜底：确保 consul / nomad agent 进程确实退出（与 uninstall_nomad() 对称）
    pkill nomad 2>/dev/null || true
    pkill consul 2>/dev/null || true

    tasks=("redis" "/client-proxy" "/api" "template-manager")

    # 遍历数组（复用定义的列表，避免冗余）
    for task in "${tasks[@]}"; do
        # 查找进程PID：排除当前脚本PID（$$），避免误杀自己
        pids=$(pgrep -f "^$task" | grep -v $$)  # ^$task 匹配以进程名开头的命令，减少误匹配
        
        # 检查是否找到PID
        if [ -n "$pids" ]; then
            echo "正在关闭 $task 进程，PID列表：$pids"
            # 优雅关闭进程（SIGTERM），重定向错误输出避免干扰
            kill $pids 2>/dev/null
            
            # 验证是否关闭成功
            sleep 1
            remaining_pids=$(pgrep -f "^$task" | grep -v $$)
            if [ -z "$remaining_pids" ]; then
                echo "✅ $task 进程已全部关闭"
            else
                echo "⚠️  部分 $task 进程未优雅关闭，强制终止（PID：$remaining_pids）"
                kill -9 $remaining_pids 2>/dev/null
            fi
        else
            echo "ℹ️  未找到运行中的 $task 进程"
        fi
    done
    success "e2b-infra 服务停止完成！"
}

function make_images() {
    # 1. 保存原镜像的 Entrypoint 和 Cmd 配置
    image_name=$1
    ORIG_ENTRY=$(docker inspect $image_name --format='{{json .Config.Entrypoint}}')
    ORIG_CMD=$(docker inspect $image_name --format='{{json .Config.Cmd}}')

    echo "原 ENTRYPOINT: $ORIG_ENTRY"
    echo "原 CMD: $ORIG_CMD"

    temp_image=temp-$image_name
    # 2. 清理并启动临时容器（用 tail 保持运行，覆盖原 entrypoint）
    docker rm -f $temp_image 2>/dev/null
    docker run -d --name $temp_image --privileged --entrypoint tail \
    $image_name \
    -f /dev/null

    # 3. 进入容器使用 yum 安装组件
    docker exec $temp_image bash -c " \
    yum install -y systemd systemd-sysv openssh-server sudo chrony linuxptp socat curl wget iputils bind-utils iproute nc tcpdump passwd&& \
    yum clean all && \
    rm -rf /var/cache/yum /var/tmp/* /tmp/* \
    "

    docker exec $temp_image bash -c ' \
        wget -O /usr/local/bin/websocat https://github.com/vi/websocat/releases/latest/download/websocat.aarch64-unknown-linux-musl && \
        chmod a+x /usr/local/bin/websocat && \
        websocat --version'


    # 4. 停止容器并导出导入（关键：恢复原来的 Entrypoint 和 Cmd）
    docker stop $temp_image
    docker export $temp_image | docker import \
    --change "ENTRYPOINT $ORIG_ENTRY" \
    --change "CMD $ORIG_CMD" \
    - ${SERVER_IP}:${HARBOR_HTTP_PORT}/e2b-orchestration/openclaw-openviking:custom

    # 5. 推送新镜像
    docker push ${SERVER_IP}:${HARBOR_HTTP_PORT}/e2b-orchestration/openclaw-openviking:custom

    # 6. 验证配置是否与原镜像一致
    docker inspect ${SERVER_IP}:${HARBOR_HTTP_PORT}/e2b-orchestration/openclaw-openviking:custom \
    --format='Entrypoint: {{.Config.Entrypoint}} Cmd: {{.Config.Cmd}}'

    # 7. 清理临时容器
    docker rm -f $temp_image
}

# ===================== 参数解析（完整修正）=====================
# 定义正确的短/长选项：m: 表示 -m 需要接收参数；d对应stop
OPTIONS="iusdfm:r:"
LONGOPTIONS="install,uninstall,start,stop,deploy,make:,redeploy:"  # make:/redeploy: 表示需要参数

# 解析参数（处理 getopt 结果）
# 兼容不同系统的 getopt，增加 -o/-l 明确指定选项
PARSED=$(getopt -o "$OPTIONS" -l "$LONGOPTIONS" --name "$0" -- "$@")
if [ $? -ne 0 ]; then
    error "参数解析失败！用法：$0 --install/--uninstall/--start/--stop/--deploy/--make <参数>"
fi
eval set -- "$PARSED"

# 标记是否传入了有效参数
has_valid_param=0

# 遍历参数（修正循环逻辑，正确处理带参数的选项）
while true; do
    case "$1" in
        -i|--install)
            install
            has_valid_param=1
            shift  # 跳过当前选项（-i/--install）
            ;;
        -u|--uninstall)
            uninstall
            has_valid_param=1
            shift
            ;;
        -s|--start)
            start
            has_valid_param=1
            shift
            ;;
        -d|--stop)
            stop
            has_valid_param=1
            shift
            ;;
        -f|--deploy)
            deploy
            has_valid_param=1
            shift
            ;;
        -r|--redeploy)
            # -r/--redeploy 需要接收 job 名（nomad/ 下 hcl 文件名，不带 .hcl）
            if [ -z "$2" ]; then
                error "--redeploy/-r 必须指定 job 名！例如：$0 -r template-manager"
            fi
            redeploy_job "$2"
            has_valid_param=1
            shift 2
            ;;
        -m|--make)
            # -m/--make 需要接收后续参数，$2 是参数值
            if [ -z "$2" ]; then
                error "--make/-m 必须指定参数！例如：$0 -m ubuntu22.04"
            fi
            make_images "$2"  # 传入正确的参数（$2）
            has_valid_param=1
            shift 2  # 跳过当前选项（-m/--make）+ 参数值
            ;;
        --)
            # 到达参数列表末尾，退出循环
            shift
            break
            ;;
        *)
            # 未知参数，但不立即报错，交给后续的 has_valid_param 判断
            shift
            ;;
    esac
done

# 未传有效参数时提示完整用法
if [ $has_valid_param -eq 0 ]; then
    echo "用法："
    echo "  安装所有组件：$0 --install 或 $0 -i"
    echo "  卸载所有组件：$0 --uninstall 或 $0 -u"
    echo "  启动 e2b-infra 服务：$0 --start 或 $0 -s"
    echo "  停止 e2b-infra 服务：$0 --stop 或 $0 -d"
    echo "  部署 e2b-infra 服务：$0 --deploy 或 $0 -f"
    echo "  快速重跑单个 job：$0 --redeploy <job> 或 $0 -r <job>（例如：$0 -r template-manager，只 render+重跑该 job，跳过镜像构建）"
    echo "  构建镜像：$0 --make <镜像参数> 或 $0 -m <镜像参数>（例如：$0 -m ubuntu22.04）"
    exit 1
fi
