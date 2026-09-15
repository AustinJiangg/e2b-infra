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
      # 停服时进程要在退出前跑 networkPool.Close()，把网络槽位暖池里的
      # netns / veth / iptables 规则逐个拆掉。本仓库把池子从上游的
      # NewSlotsPoolSize 32 / ReusedSlotsPoolSize 100 调到了 300 / 1000：零并发时常驻
      # 300 个槽位，历史峰值并发越高、回收池里沉淀的越多（上限 1000）。每个槽位 9 条
      # iptables 规则，拆一轮是十几秒到几十秒。
      #
      # 30s 是 Nomad 客户端 max_kill_timeout 的默认上限（patch 把上游那行
      # max_kill_timeout = "24h" 删掉了，所以走默认），提不上去。槽位多时拆不完会被
      # SIGKILL，剩下的永久留在宿主机上：StorageLocal 下次启动把它们记成 foreignNs
      # 永久跳过，槽位号只增不减，攒到几千个网卡后 nomad client 的 fingerprint 要走
      # 几分钟，表现就是 build.sh -s 卡在"端口未启动"。
      #
      # 所以别指望这 30s 能清干净——停服后用 build.sh --recycle-netns 兜底。
      kill_timeout = "30s"

      # Nomad task cgroup 的 memory.max 硬上限。firecracker 进程也在这个 cgroup 里
      # （arm64 补丁注释掉了 CLONE_INTO_CGROUP，/sys/fs/cgroup/e2b 只是空目录），
      # 但客户机内存走 hugetlb、不记 memory 控制器，所以这里不是"沙箱数 × 内存"的预算；
      # 记账的是 orchestrator 堆 + 构建/快照产生的 page cache。太小会 memcg OOM
      # （Exit 137）并连锁到 api 恒 503、部署超时失败。256 GiB 只是"跑飞了别拖垮
      # 整机"的护栏。怎么量峰值、怎么改、三处路径分别在哪，见
      # deploy-docs/12-orchestrator资源配额调优.md
      resources {
        memory     = 262144
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
