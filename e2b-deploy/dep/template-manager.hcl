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

      # 这个配额约束的是 orchestrator 进程所在的 Nomad task cgroup
      # (/nomad.slice/share.slice/<allocID>.start.scope)，也就是**模板构建**这条路径：
      # 解压基础镜像的层、拼 rootfs、往 /mnt/snapshot-cache(tmpfs) 写快照产生的
      # page cache 与 shmem 页全部记在这里。
      #
      # 沙箱不在这个 cgroup 里——orchestrator 自己建了 /sys/fs/cgroup/e2b 那棵树
      # (sandbox/cgroup/manager.go)，而且沙箱 VM 内存走 hugepages，连常规 memcg 的账
      # 都不走。所以"并发几十个沙箱毫无压力"完全不能说明这个值够用。
      #
      # 8192 太小：构建一个 200 MB 的基础镜像（skip_cache=True 全量重建）就会
      # memcg OOM，内核挑 cgroup 里最大的进程杀，表现为
      #   Terminated Exit Code: 137, Signal: 9
      # 而 dmesg 里 anon-rss 只有几十 MB —— 吃掉配额的是 page cache 和 tmpfs 页，
      # 不是进程堆，很容易误判成"进程没吃内存为什么被杀"。
      # 再往上的连锁反应：orchestrator 反复重启 → api 的 orch.NodeCount() 为 0 →
      # /health 恒 503 → 部署卡在 api 的 deployment 上超时失败，根因离现象很远。
      #
      # 复核实际峰值再按需调整（构建跑完后读）：
      #   cat /sys/fs/cgroup/nomad.slice/share.slice/*.start.scope/memory.peak
      # 建议取峰值的 3~4 倍。别无脑调到几百 G：这个上限的意义就是"跑飞了别把
      # 整台机器拖垮"，共享机器上尤其如此。
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
