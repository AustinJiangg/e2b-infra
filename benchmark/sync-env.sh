#!/usr/bin/env bash
# 从磁盘上的“真相源”把易变的配置同步进 benchmark/.env（其它行原样保留）：
#   E2B_ACCESS_TOKEN / E2B_API_KEY <- /root/.e2b/config.json（deploy.sh 首次 seed-db 后写入）
#   NOMAD_TOKEN                     <- ${NOMAD_DATA_DIR:-/data/nomad}/acl.token（Nomad ACL bootstrap 持久化）
#   E2B_API_URL                     <- ${E2B_DEPLOY_ENV:-/opt/e2b-infra/dep/.env} 里的 SERVER_IP（仅当还是占位符/为空时）
#
# 用法: bash sync-env.sh
# 覆盖默认路径: E2B_CONFIG_JSON=/path/config.json NOMAD_DATA_DIR=/path/nomad \
#               E2B_DEPLOY_ENV=/path/dep/.env SERVER_IP=1.2.3.4 bash sync-env.sh
#
# 只改这几行的值，不动 .env 里的其它配置（E2B_DOMAIN / E2B_HTTP_SSL 等）。
#
# 关于 /root/.e2b/config.json：它是 deploy.sh 在“数据库里还没有 E2B 团队”时执行 seed-db
# 才写一次的一次性产物（见 e2b-deploy/dep/deploy.sh 的 seed 区块）。数据库里存的是
# 哈希（seed-db.go 的 ApiKeyHash / AccessTokenHash），**明文只在这个文件和本 .env 里**。
# 所以它不存在是常态（重复执行本脚本时就会看到），只要 .env 里已有值就继续沿用；
# 但两边都丢了就只能清库重新 seed —— 建议把 .env 备份好。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
CONFIG="${E2B_CONFIG_JSON:-/root/.e2b/config.json}"
ACL_TOKEN_FILE="${NOMAD_DATA_DIR:-/data/nomad}/acl.token"
DEPLOY_ENV="${E2B_DEPLOY_ENV:-/opt/e2b-infra/dep/.env}"

# .env 不存在则从模板初始化
if [[ ! -f "$ENV_FILE" ]]; then
  cp "$SCRIPT_DIR/.env.example" "$ENV_FILE"
  echo "已从 .env.example 初始化 $ENV_FILE"
fi

command -v jq >/dev/null 2>&1 || { echo "错误: 需要 jq 来解析 $CONFIG" >&2; exit 1; }

# 读某个 env 文件里 KEY 的值：剥引号、去行尾注释、去首尾空白。取不到就输出空串。
read_env_val() {
  local file="$1" key="$2" v
  [[ -r "$file" ]] || return 0
  v="$(awk -v k="$key" '
    $0 ~ "^[[:space:]]*(export[[:space:]]+)?" k "=" && !seen {
      seen=1
      sub("^[[:space:]]*(export[[:space:]]+)?" k "=", "", $0)
      print $0
    }' "$file")"
  if [[ "$v" == \"*\"* ]]; then
    v="${v#\"}"; v="${v%%\"*}"
  elif [[ "$v" == \'*\'* ]]; then
    v="${v#\'}"; v="${v%%\'*}"
  else
    v="${v%%#*}"
  fi
  v="${v#"${v%%[![:space:]]*}"}"
  v="${v%"${v##*[![:space:]]}"}"
  printf '%s' "$v"
}

# 改或追加一行 KEY="VALUE"。用 awk 把值当变量传入（不拼进程序体），
# 避免 sed 分隔符 / & / 转义等踩雷。
upsert() {
  local key="$1" val="$2" tmp
  tmp="$(mktemp)"
  awk -v k="$key" -v v="$val" '
    $0 ~ "^[[:space:]]*(export[[:space:]]+)?" k "=" && !seen { print k "=\"" v "\""; seen=1; next }
    { print }
    END { if (!seen) print k "=\"" v "\"" }
  ' "$ENV_FILE" > "$tmp"
  mv "$tmp" "$ENV_FILE"
}

# 取到有效值才写；空 / null 跳过，且绝不用空值覆盖 .env 里已有的
sync_one() {
  local key="$1" val="$2" src="$3"
  if [[ -z "$val" || "$val" == "null" ]]; then
    if [[ -n "$(read_env_val "$ENV_FILE" "$key")" ]]; then
      echo "跳过 $key：$src 没取到有效值，沿用 .env 里已有的"
    else
      echo "跳过 $key：从 $src 没取到有效值，且 .env 里也是空的" >&2
    fi
    return
  fi
  upsert "$key" "$val"
  echo "已同步 $key（来自 $src）"
}

# ---- E2B token ----
if [[ -r "$CONFIG" ]]; then
  sync_one E2B_ACCESS_TOKEN "$(jq -r '.accessToken // empty' "$CONFIG" 2>/dev/null || true)" "$CONFIG"
  sync_one E2B_API_KEY      "$(jq -r '.teamApiKey  // empty' "$CONFIG" 2>/dev/null || true)" "$CONFIG"
elif [[ -n "$(read_env_val "$ENV_FILE" E2B_ACCESS_TOKEN)" && -n "$(read_env_val "$ENV_FILE" E2B_API_KEY)" ]]; then
  # 常态：config.json 是 seed 时的一次性产物，.env 里已经有值就没必要再同步
  echo "提示: 读不到 $CONFIG，沿用 .env 里已有的 E2B_ACCESS_TOKEN / E2B_API_KEY"
else
  echo "警告: 读不到 $CONFIG，且 .env 里也没有可用的 E2B token。" >&2
  echo "      明文只存在于该文件（数据库里是哈希），丢了就得清库后重跑 build.sh -s 重新 seed；" >&2
  echo "      若 config.json 在别处，用 E2B_CONFIG_JSON=<路径> 指过来。" >&2
fi

# ---- Nomad ACL token ----
if [[ -r "$ACL_TOKEN_FILE" ]]; then
  sync_one NOMAD_TOKEN "$(tr -d '[:space:]' < "$ACL_TOKEN_FILE")" "$ACL_TOKEN_FILE"
else
  echo "警告: 读不到 $ACL_TOKEN_FILE（可能需要 root/sudo，或设 NOMAD_DATA_DIR=<路径>），跳过 NOMAD_TOKEN 同步" >&2
fi

# ---- E2B_API_URL ----
# .env.example 里是占位符 http://<server_ip>:3000。忘了替换的话 SDK 会拿 "<server_ip>"
# 当主机名去解析，报 httpx.ConnectError: [Errno -2] Name or service not known。
# 这里只在“为空 / 仍是占位符”时才填，已经改过的值不动。
api_url="$(read_env_val "$ENV_FILE" E2B_API_URL)"
if [[ -z "$api_url" || "$api_url" == *"<server_ip>"* ]]; then
  server_ip="${SERVER_IP:-}"
  ip_src="环境变量 SERVER_IP"
  if [[ -z "$server_ip" ]]; then
    server_ip="$(read_env_val "$DEPLOY_ENV" SERVER_IP)"
    ip_src="$DEPLOY_ENV 的 SERVER_IP"
  fi
  if [[ -n "$server_ip" ]]; then
    upsert E2B_API_URL "http://$server_ip:3000"
    echo "已同步 E2B_API_URL=http://$server_ip:3000（来自 $ip_src）"
  else
    echo "警告: .env 里的 E2B_API_URL 还是占位符/为空，且读不到 $DEPLOY_ENV 的 SERVER_IP。" >&2
    echo "      请手动改成 http://<本机IP>:3000（api job 的 REST 端口），或 SERVER_IP=<IP> bash sync-env.sh；" >&2
    echo "      留空也可以——SDK 会退回 http://api.\$E2B_DOMAIN，走 dnsmasq + iptables 80→3002 那条路。" >&2
  fi
fi

# 只回显被管理的键名，不打印 token 值
echo "完成。$ENV_FILE 中已管理的凭据行:"
grep -nE '^[[:space:]]*(export[[:space:]]+)?(E2B_ACCESS_TOKEN|E2B_API_KEY|NOMAD_TOKEN)=' "$ENV_FILE" \
  | sed -E 's/=.*/=<hidden>/'
echo "连接配置（非密，SDK 实际会用这些）:"
grep -nE '^[[:space:]]*(export[[:space:]]+)?(E2B_API_URL|E2B_DOMAIN|E2B_HTTP_SSL)=' "$ENV_FILE"
