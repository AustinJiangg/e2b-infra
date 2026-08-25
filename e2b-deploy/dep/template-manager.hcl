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

      resources {
        memory     = 8192
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

        # 这一套的 checkpoint 靠 reflink 克隆内存基线，所以数据必须落在开了
        # reflink 的 XFS 卷上。不设这一行就走默认的 /orchestrator —— 在 950 上
        # 那是根盘 ext4，reflink 用不了，每次 checkpoint 会退化成整份内存拷贝，
        # 功能照常、代价几十倍，且只有 orchestrator 启动那行日志会说。
        #
        # 卷用 test-950/02-prepare-loop-volume.sh --fs xfs --mnt /mnt/xfsdev 造。
        # 换到别的挂载点就改这里 —— 写的是字面路径不是 ${...}，因为
        # deploy.sh 的 envsubst 用的是白名单，没列进去的变量会原样留下。
        ORCHESTRATOR_BASE_PATH = "/mnt/xfsdev/orchestrator"
     }

      config {
        command = "/bin/bash"
        args    = ["-c", " chmod +x /usr/bin/template-manager && /usr/bin/template-manager --port ${TEMPLATE_MANAGER_PORT}"]
      }
    }
  }
}
