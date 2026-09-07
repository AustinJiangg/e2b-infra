job "template-manager-system" {
  datacenters = ["${GCP_ZONE}"]
  type = "system"
  node_pool  = "${BUILD_NODE_POOL}"
  priority = 70

  group "template-manager" {

    network {
      port "template-manager" {
        static = "${TEMPLATE_MANAGER_PORT}"
      }
    }

    service {
      name = "template-manager"
      port = "${TEMPLATE_MANAGER_PORT}"

      check {
        type         = "grpc"
        name         = "health"
        interval     = "20s"
        timeout      = "5s"
        grpc_use_tls = false
        port         = "${TEMPLATE_MANAGER_PORT}"
      }
    }

    task "start" {
      driver = "raw_exec"
      kill_signal  = "SIGTERM"
      # 停服时 orchestrator 要在退出前跑 networkPool.Close()，把网络槽位暖池
      # （NewSlotsPoolSize 32 + ReusedSlotsPoolSize 100）里的 netns / veth / iptables
      # 规则逐个拆掉。默认 kill_timeout 只有 5s，132 个槽位常常拆不完就被 SIGKILL，
      # 剩下的会永久留在宿主机上（StorageLocal 下次启动会把它们记成 foreignNs 跳过，
      # 槽位号只增不减）。30s 是 Nomad 客户端 max_kill_timeout 的默认上限。
      kill_timeout = "30s"

      # 只约束 orchestrator 进程（Nomad task cgroup），即模板构建这条路径；
      # 沙箱在 /sys/fs/cgroup/e2b 那棵独立的树下，不受这里限制。
      # 太小会 memcg OOM（Exit 137）并连锁到 api 恒 503、部署超时失败。
      # 怎么量峰值、怎么改、三处路径分别在哪，见
      # deploy-docs/12-orchestrator资源配额调优.md
      resources {
        memory     = 32768
        cpu        = 2048
      }

      env {
        NODE_ID                       = "$${node.unique.name}"
        ENVD_TIMEOUT                  = "${ENVD_TIMEOUT}"
        CONSUL_TOKEN                  = "${CONSUL_ACL_TOKEN}"
        GCP_DOCKER_REPOSITORY_NAME    = "${HARBOR_HOST}"
        API_SECRET                    = "${EDGE_API_SECRET}"
        OTEL_TRACING_PRINT            = "${OTEL_TRACING_PRINT}"
        ENVIRONMENT                   = "${ENVIRONMENT}"
        TEMPLATE_BUCKET_NAME          = "${TEMPLATE_BUCKET_NAME}"
        BUILD_CACHE_BUCKET_NAME       = "${BUILD_CACHE_BUCKET_NAME}"
        OTEL_COLLECTOR_GRPC_ENDPOINT  = "${OTEL_COLLECTOR_GRPC_ENDPOINT}"
        LOGS_COLLECTOR_ADDRESS        = "${LOGS_COLLECTOR_ADDRESS}"
        ORCHESTRATOR_SERVICES         = "orchestrator,template-manager"
        E2B_FC_NETNS_EXEC_HELPER      = "/opt/e2b-infra/bin/fc-netns-exec"
        MAX_STARTING_INSTANCES_PER_NODE = "30"
        LOGS_COLLECTOR_PUBLIC_IP      = "${LOGS_COLLECTOR_PUBLIC_IP}"
        ALLOW_SANDBOX_INTERNET        = "${ALLOW_SANDBOX_INTERNET}"
        SHARED_CHUNK_CACHE_PATH       = "${SHARED_CHUNK_CACHE_PATH}"
        CLICKHOUSE_CONNECTION_STRING  = "clickhouse://${CLICKHOUSE_USERNAME}:${CLICKHOUSE_PASSWORD}@$localhost:${CLICKHOUSE_SERVER_PORT}/${CLICKHOUSE_DATABASE}"
        STORAGE_PROVIDER = "Local"
        ARTIFACTS_REGISTRY_PROVIDER = "${ARTIFACTS_REGISTRY_PROVIDER}"
        MINIO_ENDPOINT = "${MINIO_ENDPOINT}"
        MINIO_ACCESS_KEY = "${MINIO_ACCESS_KEY}"
        MINIO_SECRET_KEY = "${MINIO_SECRET_KEY}"
        SSL_CERT_FILE = "/etc/docker/certs.d/harbor:443/ca.crt" 
     }

      config {
        command = "/bin/bash"
        args    = ["-c", " chmod +x /usr/bin/template-manager && /usr/bin/template-manager --port ${TEMPLATE_MANAGER_PORT}"]
      }
    }
  }
}
