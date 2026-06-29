#datacenter = ""
name       = "hostname-sc7jb"
#region     = ""
bind_addr  = "0.0.0.0"

advertise {
  http = "173.118.9.2"
  rpc  = "173.118.9.2"
  serf = "173.118.9.2"
}

leave_on_interrupt = true
leave_on_terminate = true



server {
  enabled = true
  bootstrap_expect = 1
}

plugin_dir = "/opt/nomad/plugins"

plugin "docker" {
  config {
    volumes {
      enabled = true
    }
    auth {
      config = "/root/docker/config.json"
    }
  }
}

log_level = "DEBUG"
log_json = true

telemetry {
  collection_interval = "5s"
  disable_hostname = true
  prometheus_metrics = true
  publish_allocation_metrics = true
  publish_node_metrics = true
}

acl {
  enabled = true
}

limits {
  http_max_conns_per_client = 80
  rpc_max_conns_per_client = 80
}

consul {
  address = "127.0.0.1:8500"
  allow_unauthenticated = false
  server_auto_join = true
  auto_advertise = true
  token = "c61c9ffa-c337-13eb-825d-36f6d1aac75b"
}
