#!/usr/bin/env bash
# 从磁盘上的“真相源”把易变的凭据同步进 benchmark/.env（其它行原样保留）：
#   E2B_ACCESS_TOKEN / E2B_API_KEY <- /root/.e2b/config.json（e2b CLI 登录后写入）
#   NOMAD_TOKEN                     <- ${NOMAD_DATA_DIR:-/data/nomad}/acl.token（Nomad ACL bootstrap 持久化）
#
# 用法: bash sync-env.sh
# 覆盖默认路径: E2B_CONFIG_JSON=/path/config.json NOMAD_DATA_DIR=/path/nomad bash sync-env.sh
#
# 只改这几行的值，不动 .env 里的其它配置（E2B_DOMAIN / E2B_API_URL / E2B_HTTP_SSL 等）。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
CONFIG="${E2B_CONFIG_JSON:-/root/.e2b/config.json}"
ACL_TOKEN_FILE="${NOMAD_DATA_DIR:-/data/nomad}/acl.token"

# .env 不存在则从模板初始化
if [[ ! -f "$ENV_FILE" ]]; then
  cp "$SCRIPT_DIR/.env.example" "$ENV_FILE"
  echo "已从 .env.example 初始化 $ENV_FILE"
fi

command -v jq >/dev/null 2>&1 || { echo "错误: 需要 jq 来解析 $CONFIG" >&2; exit 1; }

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

# 取到有效值才写；空 / null 跳过并告警，绝不用空值覆盖 .env 里已有的
sync_one() {
  local key="$1" val="$2" src="$3"
  if [[ -z "$val" || "$val" == "null" ]]; then
    echo "跳过 $key：从 $src 没取到有效值" >&2
    return
  fi
  upsert "$key" "$val"
  echo "已同步 $key（来自 $src）"
}

if [[ -r "$CONFIG" ]]; then
  sync_one E2B_ACCESS_TOKEN "$(jq -r '.accessToken // empty' "$CONFIG" 2>/dev/null || true)" "$CONFIG"
  sync_one E2B_API_KEY      "$(jq -r '.teamApiKey  // empty' "$CONFIG" 2>/dev/null || true)" "$CONFIG"
else
  echo "警告: 读不到 $CONFIG，跳过 E2B token 同步（先用 e2b CLI 登录，或设 E2B_CONFIG_JSON=<路径>）" >&2
fi

if [[ -r "$ACL_TOKEN_FILE" ]]; then
  sync_one NOMAD_TOKEN "$(tr -d '[:space:]' < "$ACL_TOKEN_FILE")" "$ACL_TOKEN_FILE"
else
  echo "警告: 读不到 $ACL_TOKEN_FILE（可能需要 root/sudo，或设 NOMAD_DATA_DIR=<路径>），跳过 NOMAD_TOKEN 同步" >&2
fi

# 只回显被管理的键名，不打印 token 值
echo "完成。$ENV_FILE 中已管理的凭据行:"
grep -nE '^[[:space:]]*(export[[:space:]]+)?(E2B_ACCESS_TOKEN|E2B_API_KEY|NOMAD_TOKEN)=' "$ENV_FILE" \
  | sed -E 's/=.*/=<hidden>/'
