#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

DEPLOY_TYPE="nomad"  # 默认使用nomad
if [[ "$#" -gt 0 ]]; then
  while [[ "$#" -gt 0 ]]; do
    case $1 in
      --type)
        DEPLOY_TYPE="$2"
        shift 2
        ;;
      *)
        echo "Unknown parameter: $1"
        exit 1
        ;;
    esac
  done
fi

if [ ! -f .env ]; then
  echo "==> please cp .env.example .env and fill it with your current variable"
  exit 1
fi

set -a
source .env
set +a

BIN_DIR="./bin"
cd "$BIN_DIR"

for dockerfile in *.Dockerfile; do
    name=${dockerfile%.Dockerfile}
    tag="${REGISTRY_URL}/${name,,}"
    echo "------> building $tag"
    docker build -t "$tag" -f "$dockerfile" .
    docker push "$tag"
done

declare -A IMGS=(
  [redis]="redis:${REDIS_VERSION}"
  # [vector]="timberio/vector:${LOGS_COLLECTOR_VERSION}"
  # [loki]="grafana/loki:${LOKI_VERSION}"
  # [otel]="otel/opentelemetry-collector-contrib:${OTEL_COLLECTOR_VERSION}"
  # [clickhouse]="clickhouse/clickhouse-server:${CLICKHOUSE_VERSION}"
)

set -eu

for name in "${!IMGS[@]}"; do
  src=${IMGS[$name]}
  tag="${REGISTRY_URL}/${src}"

  echo "======  $src  ->  $tag  ======"
 # docker pull "$src"
  docker tag  "$src" "$tag"
  docker push "$tag"

done

cd "$SCRIPT_DIR"
if [[ "$DEPLOY_TYPE" == "k8s" ]]; then
  echo "==> rendering helm values.yaml ..."
  envsubst < helm/values-template.yaml > helm/values.yaml

  echo "==> installing with helm ..."
  helm install e2b-api ./helm --create-namespace -n e2b
  echo "==> waiting for all pods to be running (timeout: 5min)..."
  TIMEOUT=300
  START_TIME=$(date +%s)
  
  while true; do
    # 检查是否超时
    CURRENT_TIME=$(date +%s)
    ELAPSED=$((CURRENT_TIME - START_TIME))
    if [ $ELAPSED -gt $TIMEOUT ]; then
      echo "==> ERROR: timeout waiting for pods to be running"
      echo "==> current pod status:"
      kubectl get pods -n e2b || true
      exit 1
    fi
    
    # 获取 pod 状态，如果没有 pod 则继续等待
    POD_STATUS=$(kubectl get pods -n e2b --no-headers 2>/dev/null || true)
    if [ -z "$POD_STATUS" ]; then
      echo "----> no pods found yet, waiting..."
      sleep 5
      continue
    fi
    
    NOT_READY=$(echo "$POD_STATUS" | awk '
      NF {
        status = $3
        split($2, ready, "/")
        ready_count = ready[1]
        total_count = ready[2]
        
        if (ready_count != total_count) {
          count++
        }
      }
      END {print count+0}
    ')
    
    if [ "$NOT_READY" -eq 0 ]; then
      echo "==> all pods are running!"
      kubectl get pods -n e2b || true
      break
    fi
    
    echo "----> waiting for pods... ($NOT_READY not ready, elapsed: ${ELAPSED}s)"
    sleep 5
  done
elif [[ "$DEPLOY_TYPE" == "nomad" ]]; then
  mkdir -p rendered
  echo "==> rendering hcl ..."
  for hcl in nomad/*.hcl; do
    outfile=rendered/$(basename "$hcl")
    envsubst '$ENVIRONMENT $DOMAIN_NAME $GCP_ZONE $CONSUL_ACL_TOKEN $NOMAD_ACL_TOKEN $EDGE_SECRET $LOKI_URL $CLIENT_PROXY_DOCKER_IMAGE $LOKI_SERVICE_PORT_NAME $LOKI_PROXY_MAX_RESOURCES_MEMORY_MB $LOKI_PROXY_RESOURCES_MEMORY_MB $LOKI_RESOURCES_CPU_COUNT
              $POSTGRES_CONNECTION_STRING $REDIS_URL $REDIS_PORT $CLIENT_PROXY_COUNT $EDGE_PROXY_PORT_NAME $EDGE_API_PORT_NAME $CLIENT_PROXY_MAX_RESOURCES_MEMORY_MB $CLIENT_PROXY_RESOURCES_MEMORY_MB $CLIENT_PROXY_RESOURCES_CPU_COUNT
              $SUPABASE_JWT_SECRETS $POSTHOG_API_KEY $ANALYTICS_COLLECTOR_HOST $ANALYTICS_COLLECTOR_API_TOKEN $HARBOR_HOST $HARBOR_PROJECT $HARBOR_USERNAME $HARBOR_PASSWORD $DOCKER_REVERSE_PROXY_DOCKER_IMAGE $DOCKER_REVERSE_PROXY_PORT_NAME
              $LAUNCH_DARKLY_API_KEY $API_ADMIN_TOKEN $EDGE_API_SECRET $SANDBOX_ACCESS_TOKEN_HASH_SEED $CLICKHOUSE_RESOURCES_CPU_COUNT $CLICKHOUSE_RESOURCES_MEMORY_MB $CLICKHOUSE_SERVER_SECRET $REDIS_PORT_NAME $BUILD_CACHE_BUCKET_NAME
              $CLICKHOUSE_SERVER_COUNT $CLICKHOUSE_BACKUPS_BUCKET_NAME $CLICKHOUSE_USERNAME $CLICKHOUSE_DATABASE $DNS_PORT $LOCAL_CLUSTER_ENDPOINT $API_DOCKER_IMAGE $API_PORT_NAME $DB_MIGRATOR_DOCKER_IMAGE $CLICKHOUSE_NODE_POOL $CLICKHOUSE_VERSION $MINIO_ENDPOINT $MINIO_ACCESS_KEY $MINIO_SECRET_KEY
              $LOKI_BUCKET_NAME $LOGS_COLLECTOR_PUBLIC_IP $TEMPLATE_MANAGER_HOST $CLICKHOUSE_PASSWORD $OTEL_TRACING_PRINT $LOGS_COLLECTOR_ADDRESS $OTEL_COLLECTOR_GRPC_ENDPOINT $REDIS_CLUSTER_URL $OTEL_COLLECTOR_GRPC_PORT $REDIS_VERSION
              $API_PORT $EDGE_API_PORT $EDGE_PROXY_PORT $ORCHESTRATOR_PORT $ORCHESTRATOR_PROXY_PORT $ENVD_TIMEOUT $TEMPLATE_BUCKET_NAME $ALLOW_SANDBOX_INTERNET $SHARED_CHUNK_CACHE_PATH $GRAFANA_OTLP_URL $CLICKHOUSE_HOST $REGISTRY_URL
              $TEMPLATE_MANAGER_PORT $DOCKER_REVERSE_PROXY_PORT $LOKI_SERVICE_PORT $OTEL_COLLECTOR_PROXY_MAX_RESOURCES_MEMORY_MB $OTEL_COLLECTOR_PROXY_RESOURCES_MEMORY_MB $OTEL_COLLECTOR_RESOURCES_CPU_COUNT $GRAFANA_USERNAME $GRAFANA_OTEL_COLLECTOR_TOKEN
              $LOGS_PROXY_PORT $LOGS_HEALTH_PROXY_PORT $STORAGE_PROVIDER $ARTIFACTS_REGISTRY_PROVIDER $API_NODE_POOL $BUILD_NODE_POOL $LOGS_COLLECTOR_VERSION $LOKI_VERSION $OTEL_COLLECTOR_VERSION $CLICKHOUSE_SERVER_PORT $CLICKHOUSE_METRICS_PORT' \
      < "$hcl" > "$outfile"
  done

  JOBS=(
    redis
    template-manager
    edge
    api
  )

  echo "==> submitting nomad job..."
  for j in "${JOBS[@]}"; do
    if [ -f "rendered/${j}.hcl" ]; then
      echo "-----> $j"
      nomad job run --token $NOMAD_ACL_TOKEN rendered/${j}.hcl
    fi
  done

else
  echo "Unknown deploy type: $DEPLOY_TYPE"
  exit 1
fi


PG_CONTAINER="postgres"
PG_USER="postgres"
PG_DB="mydatabase"
PG_QUERY="SELECT EXISTS (SELECT 1 FROM teams WHERE name = 'E2B');"

# 核心优化：
# 1. --tuples-only：只输出数据行，不输出表头/分隔线
# 2. -q：静默模式，减少冗余输出
# 3. xargs：清理首尾空格
# 4. 增加错误码检查
RESULT=$(docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -q --tuples-only -c "$PG_QUERY" 2>/dev/null | xargs)

if [ "$RESULT" != "t" ]; then
  echo "==> importing default E2B user..."
  mkdir -p /root/.e2b
  cat >/root/.e2b/config.json <<'EOF'
{
        "email": "admin@e2b.dev",
        "accessToken": "sk_e2b_17bd3933af21f80dc10bba686691c4fcd7057123",
        "teamName": "E2B",
        "teamId": "834777bd-9956-45ca-b088-9bac9290e2ac",
        "teamApiKey": "e2b_5ec17bd3933af21f80dc10bba686691c4fcd7057"
}
EOF

  echo "==> initializing user into postgres..."
  chmod +x ./bin/seed-db
  POSTGRES_CONNECTION_STRING=$POSTGRES_CONNECTION_STRING ./bin/seed-db
fi
echo "==> E2B Local deploy complete!"
