#!/bin/bash
# This script is used to configure and run Nomad on a Google Compute Instance.

set -e

readonly NOMAD_CONFIG_FILE="default.hcl"
readonly SYSTEMD_CONFIG_PATH="/etc/systemd/system/nomad.service"

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_NAME="$(basename "$0")"

function print_usage {
  echo
  echo "Usage: run-nomad [OPTIONS]"
  echo
  echo "This script is used to configure and run Nomad on a Google Compute Instance."
  echo
  echo "Options:"
  echo
  echo -e "  --server\t\tIf set, run in server mode. Optional. At least one of --server or --client must be set."
  echo -e "  --client\t\tIf set, run in client mode. Optional. At least one of --server or --client must be set."
  echo -e "  --num-servers\t\tThe minimum number of servers to expect in the Nomad cluster. Required if --server is true."
  echo -e "  --consul-token\t\tThe ACL token that Consul uses."
  echo -e "  --nomad-token\t\tThe Nomad ACL token to use."
  echo -e "  --config-dir\t\tThe path to the Nomad config folder. Optional. Default is the absolute path of '../config', relative to this script."
  echo -e "  --data-dir\t\tThe path to the Nomad data folder. Optional. Default is the absolute path of '../data', relative to this script."
  echo -e "  --bin-dir\t\tThe path to the folder with Nomad binary. Optional. Default is the absolute path of the parent folder of this script."
  echo -e "  --log-dir\t\tThe path to the Nomad log folder. Optional. Default is the absolute path of '../log', relative to this script."
  echo -e "  --user\t\tThe user to run Nomad as. Optional. Default is to use the owner of --config-dir."
  echo -e "  --use-sudo\t\tIf set, run the Nomad agent with sudo. By default, sudo is only used if --client is set."
  echo -e "  --skip-nomad-config\tIf this flag is set, don't generate a Nomad configuration file. Optional. Default is false."
  echo -e "  --api\t\tIf set, run the Nomad agent dedicated to API. Optional. Default is false."
  echo
  echo "Example:"
  echo
  echo "  run-nomad.sh --server --config-dir /custom/path/to/nomad/config"
}

function log {
  local -r level="$1"
  local -r message="$2"
  local -r timestamp=$(date +"%Y-%m-%d %H:%M:%S")
  echo >&2 -e "${timestamp} [${level}] [$SCRIPT_NAME] ${message}"
}

function log_info {
  local -r message="$1"
  log "INFO" "$message"
}

function log_warn {
  local -r message="$1"
  log "WARN" "$message"
}

function log_error {
  local -r message="$1"
  log "ERROR" "$message"
}

# Based on code from: http://stackoverflow.com/a/16623897/483528
function strip_prefix {
  local -r str="$1"
  local -r prefix="$2"
  echo "${str#$prefix}"
}

function assert_not_empty {
  local -r arg_name="$1"
  local -r arg_value="$2"

  if [[ -z "$arg_value" ]]; then
    log_error "The value for '$arg_name' cannot be empty"
    print_usage
    exit 1
  fi
}

function assert_is_installed {
  local -r name="$1"

  if [[ ! $(command -v ${name}) ]]; then
    log_error "The binary '$name' is required by this script but is not installed or in the system's PATH."
    exit 1
  fi
}

function generate_nomad_config {
  local -r server="$1"
  local -r client="$2"
  local -r num_servers="$3"
  local -r config_dir="$4"
  local -r user="$5"
  local -r consul_token="$6"
  local -r node_pool_name="$7"
  local -r instance_ip_address="$8"
  local -r config_path="$config_dir/$NOMAD_CONFIG_FILE"

  local instance_name=""
  local instance_region=""
  local instance_zone=""

  instance_name=$(hostname -s)

  local server_config=""
  if [[ "$server" == "true" ]]; then
    server_config=$(
      cat <<EOF
server {
  enabled = true
  bootstrap_expect = $num_servers
}

EOF
    )
  fi

  local client_config=""
  if [[ "$client" == "true" ]]; then
    client_config=$(
      cat <<EOF
client {
  enabled = true
  node_pool = "$node_pool_name"
  meta {
    node_pool = "$node_pool_name"
  }
}

plugin "raw_exec" {
  config {
    enabled = true
  }
}

EOF
    )
  fi

  log_info "Creating default Nomad config file in $config_path"
  cat >"$config_path" <<EOF
#datacenter = "$zone"
name       = "$instance_name"
#region     = "$instance_region"
bind_addr  = "0.0.0.0"

advertise {
  http = "$instance_ip_address"
  rpc  = "$instance_ip_address"
  serf = "$instance_ip_address"
}

leave_on_interrupt = true
leave_on_terminate = true

$client_config

$server_config

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
  token = "$consul_token"
}
EOF
  chown "$user:$user" "$config_path"
}

function generate_systemd_config {
  local -r config_dir="$1"
  local -r data_dir="$2"
  local -r bin_dir="$3"
  local -r log_dir="$4"
  local -r user="$5"
  local -r use_sudo="$6"

  [[ "$use_sudo" == "true" ]] && exec_user="root" || exec_user="$user"

  log_info "Creating systemd unit file in $SYSTEMD_CONFIG_PATH"
  sudo tee "$SYSTEMD_CONFIG_PATH" >/dev/null <<EOF
[Unit]
Description=Nomad
Documentation=https://nomadproject.io/docs/
Wants=network-online.target
After=network-online.target

# When using Nomad with Consul it is not necessary to start Consul first. These
# lines start Consul before Nomad as an optimization to avoid Nomad logging
# that Consul is unavailable at startup.
#Wants=consul.service
#After=consul.service

[Service]
ExecReload=/bin/kill -HUP $MAINPID
ExecStart=$bin_dir/nomad agent -config $config_dir -data-dir $data_dir
KillMode=process
KillSignal=SIGINT
LimitNOFILE=infinity
LimitNPROC=infinity
Restart=on-failure
RestartSec=2
StartLimitBurst=3
StartLimitIntervalSec=10
TasksMax=infinity

[Install]
WantedBy=multi-user.target
EOF
  sudo systemctl daemon-reload
}

function start_nomad {
  log_info "Starting Nomad"
  sudo systemctl enable nomad.service
  sudo systemctl start  nomad.service
}

function bootstrap {
  log_info "Waiting for Nomad to start"
  while test -z "$(curl -s http://127.0.0.1:4646/v1/agent/health)"; do
    log_info "Nomad not yet started. Waiting for 1 second."
    sleep 1
  done
  log_info "Nomad server started."

  log_info "Bootstrapping Nomad ACL"
  # Capture nomad acl bootstrap raw output
  local bootstrap_output
  bootstrap_output=$(nomad acl bootstrap 2>&1) || {
    log_error "ACL bootstrap failed: $bootstrap_output"
    exit 1
  }

  local secret_id
  secret_id=$(echo "$bootstrap_output" | grep -E '^Secret ID' | awk '{print $4}')
  if [[ -z "$secret_id" ]]; then
    log_error "Could not parse Secret ID from bootstrap output"
    exit 1
  fi

  log_info "Successfully bootstrapped Nomad ACL"
  echo "$secret_id"
}

function create_node_pools {
  local -r nomad_token="$1"
  log_info "Creating node pools"
  cat > "$config_dir/api_node_pool.hcl"  <<EOF
node_pool "api" {
  description = "Nodes for api."
}
EOF
  nomad node pool apply -token "$nomad_token" "$config_dir/api_node_pool.hcl"
  cat > "$config_dir/build_node_pool.hcl"  <<EOF
node_pool "build" {
  description = "Nodes for template builds."
}
EOF
  nomad node pool apply -token "$nomad_token" "$config_dir/build_node_pool.hcl"
  rm -rf $config_dir/*_pool.hcl
}

# Based on: http://unix.stackexchange.com/a/7732/215969
function get_owner_of_path {
  local -r path="$1"
  ls -ld "$path" | awk '{print $3}'
}

function run {
  local server="false"
  local client="false"
  local num_servers=""
  local config_dir=""
  local data_dir=""
  local bin_dir=""
  local log_dir=""
  local user=""
  local skip_nomad_config="false"
  local use_sudo=""
  local all_args=()
  local node_pool_name="default"

  while [[ $# > 0 ]]; do
    local key="$1"

    case "$key" in
    --server)
      server="true"
      ;;
    --client)
      client="true"
      ;;
    --num-servers)
      num_servers="$2"
      shift
      ;;
    --nomad-token)
      assert_not_empty "$key" "$2"
      nomad_token="$2"
      shift
      ;;
    --consul-token)
      assert_not_empty "$key" "$2"
      consul_token="$2"
      shift
      ;;
    --config-dir)
      assert_not_empty "$key" "$2"
      config_dir="$2"
      shift
      ;;
    --data-dir)
      assert_not_empty "$key" "$2"
      data_dir="$2"
      shift
      ;;
    --bin-dir)
      assert_not_empty "$key" "$2"
      bin_dir="$2"
      shift
      ;;
    --log-dir)
      assert_not_empty "$key" "$2"
      log_dir="$2"
      shift
      ;;
    --user)
      assert_not_empty "$key" "$2"
      user="$2"
      shift
      ;;
    --cluster-tag-key)
      assert_not_empty "$key" "$2"
      cluster_tag_key="$2"
      shift
      ;;
    --cluster-tag-value)
      assert_not_empty "$key" "$2"
      cluster_tag_value="$2"
      shift
      ;;
    --skip-nomad-config)
      skip_nomad_config="true"
      ;;
    --use-sudo)
      use_sudo="true"
      ;;
    --node-pool-name)
      assert_not_empty "$key" "$2"
      node_pool_name="$2"
      shift
      ;;
    --instance-ip-address)
      assert_not_empty "$key" "$2"
      instance_ip_address="$2"
      shift
      ;;
    --help)
      print_usage
      exit
      ;;
    *)
      log_error "Unrecognized argument: $key"
      print_usage
      exit 1
      ;;
    esac

    shift
  done

  if [[ "$server" == "true" ]]; then
    assert_not_empty "--num-servers" "$num_servers"
  fi

  if [[ "$server" == "false" && "$client" == "false" ]]; then
    log_error "At least one of --server or --client must be set"
    exit 1
  fi

  if [[ -z "$use_sudo" ]]; then
    if [[ "$client" == "true" ]]; then
      use_sudo="true"
    else
      use_sudo="false"
    fi
  fi

  assert_is_installed "systemctl"
  assert_is_installed "curl"

  if [[ -z "$config_dir" ]]; then
    config_dir="/etc/nomad.d"
  fi
  mkdir -p "$config_dir"

  if [[ -z "$data_dir" ]]; then
    data_dir="/data/nomad"
  fi
  mkdir -p "$data_dir"

  if [[ -z "$bin_dir" ]]; then
    bin_dir="/usr/local/bin"
  fi

  if [[ -z "$log_dir" ]]; then
    log_dir="/log/nomad"
  fi
  mkdir -p $log_dir

  if [[ -z "$user" ]]; then
    user=$(get_owner_of_path "$config_dir")
  fi

  if [[ "$skip_nomad_config" == "true" ]]; then
    log_info "The --skip-nomad-config flag is set, so will not generate a default Nomad config file."
  else
    generate_nomad_config "$server" "$client" "$num_servers" "$config_dir" "$user" "$consul_token" "$node_pool_name" "$instance_ip_address"
  fi

  generate_systemd_config "$config_dir" "$data_dir" "$bin_dir" "$log_dir" "$user" "$use_sudo"

  # TODO: Let client wait for Nomad servers to start
  #  if [[ "$client" == "true" ]]; then
  #     log_info "Waiting for Nomad servers to start"
  #     while test -z "$(curl -s http://127.0.0.1:4646/v1/agent/leader)"
  #     do
  #       log_info "Nomad servers not yet started. Waiting for 1 second."
  #       sleep 1
  #     done
  #  fi

  start_nomad
  
  if [[ "$server" == "true" ]]; then
    # --- 远程探测是否已有 leader，避免重复 bootstrap ---
    skip_bootstrap=false
    if [[ -n "${SERVER_IPS:-}" ]]; then
      for ip in $SERVER_IPS; do
        if curl -m 2 -sf "http://${ip}:4646/v1/status/leader" | grep -q '[^"]'; then
          log_info "Detected running Nomad server at $ip, skipping bootstrap."
          skip_bootstrap=true
          break
        fi
      done
    fi

    if [[ "$skip_bootstrap" == "true" ]]; then
      log_info "Joining existing Nomad cluster."
    else
      bootstrap_token=$(bootstrap)
      create_node_pools "$bootstrap_token"
      if [[ -n "${bootstrap_token:-}" ]]; then
        env_file="$SCRIPT_DIR/.env"
        grep -q '^export NOMAD_ACL_TOKEN=' "$env_file" 2>/dev/null \
          && sed -i "s|^export NOMAD_ACL_TOKEN=.*|export NOMAD_ACL_TOKEN=$bootstrap_token|" "$env_file" \
          || echo "export NOMAD_ACL_TOKEN=$bootstrap_token" >> "$env_file"
      fi
    fi
  fi
}

run "$@"
