#!/bin/bash
# This script is used to configure and run Consul on a Google Compute Instance.

set -e

assert_not_empty(){
  if [[ -z "$2" ]]; then
    echo "ERROR: $1 must not be empty" >&2
    exit 1
  fi
}
log_info()  { echo "[INFO]  $*"; }
log_warn()  { echo "[WARN]  $*"; }
log_error() { echo "[ERROR] $*"; }
assert_is_installed(){ command -v "$1" >/dev/null || { log_error "$1 is not installed"; exit 1; }; }
get_owner_of_path()   { stat -c '%U' "$1" 2>/dev/null || stat -f '%Su' "$1" 2>/dev/null || echo "root"; }
get_owner_home_dir(){ local h; h=~$1; [[ $h == "/" ]] && { log_error "no HOME for $1"; exit 1; }; echo "$h"; }

# Import the appropriate bash commons libraries
readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

readonly CONSUL_CONFIG_FILE="default.json"
readonly SYSTEMD_CONFIG_PATH="/etc/systemd/system/consul.service"

readonly DEFAULT_AUTOPILOT_CLEANUP_DEAD_SERVERS="true"
readonly DEFAULT_AUTOPILOT_LAST_CONTACT_THRESHOLD="200ms"
readonly DEFAULT_AUTOPILOT_MAX_TRAILING_LOGS="250"
readonly DEFAULT_AUTOPILOT_SERVER_STABILIZATION_TIME="10s"
readonly DEFAULT_AUTOPILOT_REDUNDANCY_ZONE_TAG="az"
readonly DEFAULT_AUTOPILOT_DISABLE_UPGRADE_MIGRATION="false"

# The following two files are only used for one-time ACL switching, the script won't repeatedly depend on them
readonly ACL_SWITCH_FILE="/tmp/consul-acl-bootstrap-done"
readonly AGENT_TOKEN_FILE="/etc/consul.d/agent-token.json"

function print_usage {
  echo
  echo "Usage: run-consul [OPTIONS]"
  echo
  echo "This script is used to configure and run Consul on a Google Compute Instance."
  echo
  echo "Options:"
  echo
  echo -e "  --server\t\tIf set, run in server mode. Optional. Exactly one of --server or --client must be set."
  echo -e "  --client\t\tIf set, run in client mode. Optional. Exactly one of --server or --client must be set."
  echo -e "  --consul-token\t\tThe Consul ACL token to use."
  echo -e "  --cluster-tag-name\tAutomatically form a cluster with Instances that have the same value for this Compute Instance tag name. Optional."
  echo -e "  --datacenter\t\tThe name of the datacenter Consul is running in. Optional. If not specified, will default to GCP region name."
  echo -e "  --config-dir\t\tThe path to the Consul config folder. Optional. Default is the absolute path of '../config', relative to this script."
  echo -e "  --data-dir\t\tThe path to the Consul data folder. Optional. Default is the absolute path of '../data', relative to this script."
  echo -e "  --systemd-stdout\t\tThe StandardOutput option of the systemd unit.  Optional.  If not configured, uses systemd's default (journal)."
  echo -e "  --systemd-stderr\t\tThe StandardError option of the systemd unit.  Optional.  If not configured, uses systemd's default (inherit)."
  echo -e "  --bin-dir\t\tThe path to the folder with Consul binary. Optional. Default is the absolute path of the parent folder of this script."
  echo -e "  --user\t\tThe user to run Consul as. Optional. Default is to use the owner of --config-dir."
  echo -e "  --enable-gossip-encryption\t\tEnable encryption of gossip traffic between nodes. Optional. Must also specify --gossip-encryption-key."
  echo -e "  --gossip-encryption-key\t\tThe key to use for encrypting gossip traffic. Optional. Must be specified with --enable-gossip-encryption."
  echo -e "  --dns-request-token\t\tThe token to use for DNS requests."
  echo -e "  --enable-rpc-encryption\t\tEnable encryption of RPC traffic between nodes. Optional. Must also specify --ca-file-path, --cert-file-path and --key-file-path."
  echo -e "  --ca-path\t\tPath to the directory of CA files used to verify outgoing connections. Optional. Must be specified with --enable-rpc-encryption."
  echo -e "  --cert-file-path\tPath to the certificate file used to verify incoming connections. Optional. Must be specified with --enable-rpc-encryption and --key-file-path."
  echo -e "  --key-file-path\tPath to the certificate key used to verify incoming connections. Optional. Must be specified with --enable-rpc-encryption and --cert-file-path."
  echo -e "  --verify-server-hostname\tWhen passed in, enable server hostname verification as part of RPC encryption. Each server in Consul should get their own certificate that contains SERVERNAME.DATACENTERNAME.consul in the hostname or SAN. This prevents an authenticated agent from being converted into a server that streams all data, bypassing ACLs."
  echo -e "  --environment\t\tA single environment variable in the key/value pair form 'KEY=\"val\"' to pass to Consul as environment variable when starting it up. Repeat this option for additional variables. Optional."
  echo -e "  --skip-consul-config\tIf this flag is set, don't generate a Consul configuration file. Optional. Default is false."
  echo -e "  --recursor\tThis flag provides address of upstream DNS server that is used to recursively resolve queries if they are not inside the service domain for Consul. Repeat this option for additional variables. Optional."
  echo
  echo "Options for Consul Autopilot:"
  echo
  echo -e "  --autopilot-cleanup-dead-servers\tSet to true or false to control the automatic removal of dead server nodes periodically and whenever a new server is added to the cluster. Defaults to $DEFAULT_AUTOPILOT_CLEANUP_DEAD_SERVERS. Optional."
  echo -e "  --autopilot-last-contact-threshold\tControls the maximum amount of time a server can go without contact from the leader before being considered unhealthy. Must be a duration value such as 10s. Defaults to $DEFAULT_AUTOPILOT_LAST_CONTACT_THRESHOLD. Optional."
  echo -e "  --autopilot-max-trailing-logs\t\tControls the maximum number of log entries that a server can trail the leader by before being considered unhealthy. Defaults to $DEFAULT_AUTOPILOT_MAX_TRAILING_LOGS. Optional."
  echo -e "  --autopilot-server-stabilization-time\tControls the minimum amount of time a server must be stable in the 'healthy' state before being added to the cluster. Only takes effect if all servers are running Raft protocol version 3 or higher. Must be a duration value such as 30s. Defaults to $DEFAULT_AUTOPILOT_SERVER_STABILIZATION_TIME. Optional."
  echo -e "  --autopilot-redundancy-zone-tag\t\t(Enterprise-only) This controls the -node-meta key to use when Autopilot is separating servers into zones for redundancy. Only one server in each zone can be a voting member at one time. If left blank, this feature will be disabled. Defaults to $DEFAULT_AUTOPILOT_REDUNDANCY_ZONE_TAG. Optional."
  echo -e "  --autopilot-disable-upgrade-migration\t(Enterprise-only) If this flag is set, this will disable Autopilot's upgrade migration strategy in Consul Enterprise of waiting until enough newer-versioned servers have been added to the cluster before promoting any of them to voters. Defaults to $DEFAULT_AUTOPILOT_DISABLE_UPGRADE_MIGRATION. Optional."
  echo -e "  --autopilot-upgrade-version-tag\t\t(Enterprise-only) That tag to be used to override the version information used during a migration. Optional."
  echo
  echo
  echo "Example:"
  echo
  echo "  run-consul.sh --server --cluster-tag-name consul-xyz --config-dir /custom/path/to/consul/config"
}

function split_by_lines {
  local prefix="$1"
  shift

  for var in "$@"; do
    echo "${prefix}${var}"
  done
}

function generate_consul_config {
  local -r server="${1}"
  local -r consul_token="${2}"
  local -r config_dir="${3}"
  local -r user="${4}"
  local -r cluster_tag_name="${5}"
  local -r cluster_size_instance_metadata_key_name="${6}"
  local -r datacenter="${7}"
  local -r enable_gossip_encryption="${8}"
  local -r gossip_encryption_key="${9}"
  local -r enable_rpc_encryption="${10}"
  local -r verify_server_hostname="${11}"
  local -r ca_path="${12}"
  local -r cert_file_path="${13}"
  local -r key_file_path="${14}"
  local -r cleanup_dead_servers="${15}"
  local -r last_contact_threshold="${16}"
  local -r max_trailing_logs="${17}"
  local -r server_stabilization_time="${18}"
  local -r redundancy_zone_tag="${19}"
  local -r disable_upgrade_migration="${20}"
  local -r upgrade_version_tag=${21}
  local -r instance_ip_address=${22}
  local -r config_path="$config_dir/$CONSUL_CONFIG_FILE"

  shift 20
  local -ar recursors=("$@")

  local instance_id=""
  local instance_name=""
  local project_id=""
  local instance_region=""
  local ui="false"

  instance_name=$(hostname)

  # Configure Cloud Auto Join. See https://www.consul.io/docs/install/cloud-auto-join#google-compute-engine for more info.
  if [[ -z "$server_ips" ]]; then
    log_error "Missing --server-ips argument."
    exit 1
  fi
  retry_join_json=$(jq -n --arg ips "$server_ips" '$ips | split(" ")')

  local bootstrap_expect=""
  if [[ "$server" == "true" ]]; then
    local cluster_size=""
    cluster_size=$(echo "$server_ips" | tr -s ' ' '\n' | grep -c '.')

    bootstrap_expect="\"bootstrap_expect\": $cluster_size,"
    ui="true"
  fi

  local autopilot_configuration
  autopilot_configuration=$(
    cat <<EOF
"autopilot": {
  "cleanup_dead_servers": $cleanup_dead_servers,
  "last_contact_threshold": "$last_contact_threshold",
  "max_trailing_logs": $max_trailing_logs,
  "server_stabilization_time": "$server_stabilization_time",
  "redundancy_zone_tag": "$redundancy_zone_tag",
  "disable_upgrade_migration": $disable_upgrade_migration,
  "upgrade_version_tag": "$upgrade_version_tag"
},
EOF
  )

  local gossip_encryption_configuration=""
  if [[ "$enable_gossip_encryption" == "true" && -n "$gossip_encryption_key" ]]; then
    log_info "Creating gossip encryption configuration"
    gossip_encryption_configuration="\"encrypt\": \"$gossip_encryption_key\","
  fi

  local rpc_encryption_configuration=""
  if [[ "$enable_rpc_encryption" == "true" && -n "$ca_path" && -n "$cert_file_path" && -n "$key_file_path" ]]; then
    log_info "Creating RPC encryption configuration"
    rpc_encryption_configuration=$(
      cat <<EOF
"verify_outgoing": true,
"verify_incoming": true,
"verify_server_hostname": $verify_server_hostname,
"ca_path": "$ca_path",
"cert_file": "$cert_file_path",
"key_file": "$key_file_path",
EOF
    )
  fi

  log_info "Creating default Consul configuration"
  local acl_policy='allow'
  local agent_token=''
  if [[ -f "$ACL_SWITCH_FILE" && -f "$AGENT_TOKEN_FILE" ]]; then
    acl_policy='deny'
    agent_token=$(jq -r '.acl.tokens.default' "$AGENT_TOKEN_FILE")
  fi
  if [[ "${client:-}" == "true" || "${skip_bootstrap}" == "true" ]]; then
    agent_token="${CONSUL_ACL_TOKEN:-}"
  fi
  local default_config_json
  default_config_json=$(
    cat <<EOF
{
  "connect": {
    "enabled": true
  },
  "acl": {
    "enabled": true,
    "default_policy": "$acl_policy",
    "enable_token_persistence": true,
    "tokens": {
      "default": "$agent_token"
    }
  },
  "telemetry": {
    "prometheus_retention_time": "2h",
    "disable_hostname": true
  },
  "limits": {
    "http_max_conns_per_client": 80
  },
  "advertise_addr": "$instance_ip_address",
  "bind_addr": "$instance_ip_address",
  $bootstrap_expect
  "client_addr": "0.0.0.0",
  "datacenter": "$datacenter",
  "node_name": "$instance_id",
  "leave_on_terminate": true,
  "skip_leave_on_interrupt": true,
  "retry_join": $retry_join_json,
  "server": $server,
  $gossip_encryption_configuration
  $rpc_encryption_configuration
  $autopilot_configuration
  "ui": $ui
}
EOF
  )

  log_info "Installing Consul config file in $config_path"
  echo "$default_config_json" | jq '.' >"$config_path"
  chown "$user:$user" "$config_path"
}

function generate_systemd_config {
  local -r systemd_config_path="$1"
  local -r consul_config_dir="$2"
  local -r consul_data_dir="$3"
  local -r consul_systemd_stdout="$4"
  local -r consul_systemd_stderr="$5"
  local -r consul_bin_dir="$6"
  local -r consul_user="$7"
  shift 7
  local -ar environment=("$@")
  local -r config_path="$consul_config_dir/$CONSUL_CONFIG_FILE"

  log_info "Creating systemd config file to run Consul in $systemd_config_path"

  local -r unit_config=$(
    cat <<EOF
[Unit]
Description="HashiCorp Consul - A service mesh solution"
Documentation=https://www.consul.io/
Requires=network-online.target
After=network-online.target
ConditionFileNotEmpty=$config_path
EOF
  )

  local -r service_config=$(
    cat <<EOF
[Service]
Type=notify
User=$consul_user
Group=$consul_user
ExecStart=$consul_bin_dir/consul agent -config-dir $consul_config_dir -data-dir $consul_data_dir
ExecReload=$consul_bin_dir/consul reload
ExecStop=$consul_bin_dir/consul leave
KillMode=process
Restart=on-failure
TimeoutSec=300s
LimitNOFILE=65536
$(split_by_lines "Environment=" "${environment[@]}")
EOF
  )

  local log_config=""
  if [[ -n $consul_systemd_stdout ]]; then
    log_config+="StandardOutput=$consul_systemd_stdout\n"
  fi
  if [[ -n $consul_systemd_stderr ]]; then
    log_config+="StandardError=$consul_systemd_stderr\n"
  fi

  local -r install_config=$(
    cat <<EOF
[Install]
WantedBy=multi-user.target
EOF
  )

  echo -e "$unit_config" >"$systemd_config_path"
  echo -e "$service_config" >>"$systemd_config_path"
  echo -e "$log_config" >>"$systemd_config_path"
  echo -e "$install_config" >>"$systemd_config_path"
}

function start_consul {
  log_info "Reloading systemd config and starting Consul"

  sudo systemctl daemon-reload
  sudo systemctl enable consul.service
  sudo systemctl restart consul.service || true
}

# Based on: http://unix.stackexchange.com/a/7732/215969
function get_owner_of_path {
  local -r path="$1"
  ls -ld "$path" | awk '{print $3}'
}

function get_owner_home_dir {
  local -r user="$1"

  local home_dir=""
  home_dir=$(sudo su - $user -c 'echo $HOME')

  if [[ "$home_dir" == "/" ]]; then
    log_error "No \$HOME directory is set for user $user. This may cause unpredictable behavior with Consul in GCP. Exiting."
    exit 1
  fi

  echo "$home_dir"
}

function run {
  local server="false"
  local client="false"
  local config_dir=""
  local data_dir=""
  local systemd_stdout=""
  local systemd_stderr=""
  local bin_dir=""
  local server_ips=""
  local user=""
  local cluster_tag_name=""
  local datacenter=""
  local upgrade_version_tag=""
  local enable_gossip_encryption="false"
  local gossip_encryption_key=""
  local enable_rpc_encryption="false"
  local verify_server_hostname="false"
  local ca_path=""
  local cert_file_path=""
  local key_file_path=""
  local environment=()
  local skip_consul_config="false"
  local recursors=()
  local cleanup_dead_servers="$DEFAULT_AUTOPILOT_CLEANUP_DEAD_SERVERS"
  local last_contact_threshold="$DEFAULT_AUTOPILOT_LAST_CONTACT_THRESHOLD"
  local max_trailing_logs="$DEFAULT_AUTOPILOT_MAX_TRAILING_LOGS"
  local server_stabilization_time="$DEFAULT_AUTOPILOT_SERVER_STABILIZATION_TIME"
  local redundancy_zone_tag="$DEFAULT_AUTOPILOT_REDUNDANCY_ZONE_TAG"
  local disable_upgrade_migration="$DEFAULT_AUTOPILOT_DISABLE_UPGRADE_MIGRATION"

  while [[ $# -gt 0 ]]; do
    local key="$1"

    case "$key" in
    --server)
      server="true"
      ;;
    --client)
      client="true"
      ;;
    --server-ips)
      assert_not_empty "$key" "$2"
      server_ips="$2"
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
    --systemd-stdout)
      assert_not_empty "$key" "$2"
      systemd_stdout="$2"
      shift
      ;;
    --systemd-stderr)
      assert_not_empty "$key" "$2"
      systemd_stderr="$2"
      shift
      ;;
    --bin-dir)
      assert_not_empty "$key" "$2"
      bin_dir="$2"
      shift
      ;;
    --user)
      assert_not_empty "$key" "$2"
      user="$2"
      shift
      ;;
    --cluster-tag-name)
      assert_not_empty "$key" "$2"
      cluster_tag_name="$2"
      shift
      ;;
    --datacenter)
      assert_not_empty "$key" "$2"
      datacenter="$2"
      shift
      ;;
    --autopilot-cleanup-dead-servers)
      assert_not_empty "$key" "$2"
      cleanup_dead_servers="$2"
      shift
      ;;
    --autopilot-last-contact-threshold)
      assert_not_empty "$key" "$2"
      last_contact_threshold="$2"
      shift
      ;;
    --autopilot-max-trailing-logs)
      assert_not_empty "$key" "$2"
      max_trailing_logs="$2"
      shift
      ;;
    --autopilot-server-stabilization-time)
      assert_not_empty "$key" "$2"
      server_stabilization_time="$2"
      shift
      ;;
    --autopilot-redundancy-zone-tag)
      assert_not_empty "$key" "$2"
      redundancy_zone_tag="$2"
      shift
      ;;
    --autopilot-disable-upgrade-migration)
      disable_upgrade_migration="true"
      shift
      ;;
    --autopilot-upgrade-version-tag)
      assert_not_empty "$key" "$2"
      upgrade_version_tag="$2"
      shift
      ;;
    --enable-gossip-encryption)
      enable_gossip_encryption="true"
      ;;
    --gossip-encryption-key)
      assert_not_empty "$key" "$2"
      gossip_encryption_key="$2"
      shift
      ;;
    --dns-request-token)
      assert_not_empty "$key" "$2"
      dns_request_token="$2"
      shift
      ;;
    --enable-rpc-encryption)
      enable_rpc_encryption="true"
      ;;
    --verify-server-hostname)
      verify_server_hostname="true"
      ;;
    --ca-path)
      assert_not_empty "$key" "$2"
      ca_path="$2"
      shift
      ;;
    --cert-file-path)
      assert_not_empty "$key" "$2"
      cert_file_path="$2"
      shift
      ;;
    --key-file-path)
      assert_not_empty "$key" "$2"
      key_file_path="$2"
      shift
      ;;
    --environment)
      assert_not_empty "$key" "$2"
      environment+=("$2")
      shift
      ;;
    --skip-consul-config)
      skip_consul_config="true"
      ;;
    --recursor)
      assert_not_empty "$key" "$2"
      recursors+=("$2")
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

  if [[ ("$server" == "true" && "$client" == "true") || ("$server" == "false" && "$client" == "false") ]]; then
    log_error "Exactly one of --server or --client must be set."
    exit 1
  fi

  assert_is_installed "systemctl"
  assert_is_installed "curl"
  assert_is_installed "jq"

  if [[ -z "$config_dir" ]]; then
    config_dir="/etc/consul.d"
  fi
  mkdir -p "$config_dir"

  if [[ -z "$data_dir" ]]; then
    data_dir="/data/consul"
  fi
  mkdir -p "$data_dir"

  # If $systemd_stdout and/or $systemd_stderr are empty, we leave them empty so that generate_systemd_config will use systemd's defaults (journal and inherit, respectively)

  if [[ -z "$bin_dir" ]]; then
    bin_dir="/usr/local/bin"
  fi

  if [[ -z "$user" ]]; then
    user=$(get_owner_of_path "$config_dir")
  fi

  if [[ -z "$datacenter" ]]; then
    datacenter=dc1
  fi

  for ip in $server_ips; do
    if curl -sSf "http://$ip:8500/v1/status/leader" | grep -q '"'; then
      skip_bootstrap=true
      break
    fi
  done

  # When joining an already-bootstrapped cluster, recover the real ACL token that was
  # persisted at first bootstrap. build.sh -i / rpm reinstall reset .env to the placeholder
  # CONSUL_ACL_TOKEN, so without this the config below (and deploy.sh) would use a bad token.
  # The file lives under /opt/consul and is wiped together with the cluster on build.sh -d / -u.
  if [[ "${skip_bootstrap:-false}" == "true" && -f /opt/consul/acl.token ]]; then
    CONSUL_ACL_TOKEN="$(cat /opt/consul/acl.token)"
    export CONSUL_ACL_TOKEN
    local recover_env_file
    recover_env_file="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/.env"
    grep -q '^export CONSUL_ACL_TOKEN=' "$recover_env_file" 2>/dev/null \
      && sed -i "s|^export CONSUL_ACL_TOKEN=.*|export CONSUL_ACL_TOKEN=$CONSUL_ACL_TOKEN|" "$recover_env_file"
    log_info "Recovered persisted Consul ACL token from /opt/consul/acl.token"
  fi

  if [[ "$skip_consul_config" == "true" ]]; then
    log_info "The --skip-consul-config flag is set, so will not generate a default Consul config file."
  else
    if [[ "$enable_gossip_encryption" == "true" ]]; then
      assert_not_empty "--gossip-encryption-key" "$gossip_encryption_key"
    fi
    if [[ "$enable_rpc_encryption" == "true" ]]; then
      assert_not_empty "--ca-path" "$ca_path"
      assert_not_empty "--cert-file-path" "$cert_file_path"
      assert_not_empty "--key_file_path" "$key_file_path"
    fi

    generate_consul_config "$server" \
      "$consul_token" \
      "$config_dir" \
      "$user" \
      "$cluster_tag_name" \
      "$CLUSTER_SIZE_INSTANCE_METADATA_KEY_NAME" \
      "$datacenter" \
      "$enable_gossip_encryption" \
      "$gossip_encryption_key" \
      "$enable_rpc_encryption" \
      "$verify_server_hostname" \
      "$ca_path" \
      "$cert_file_path" \
      "$key_file_path" \
      "$cleanup_dead_servers" \
      "$last_contact_threshold" \
      "$max_trailing_logs" \
      "$server_stabilization_time" \
      "$redundancy_zone_tag" \
      "$disable_upgrade_migration" \
      "$upgrade_version_tag" \
      "$instance_ip_address" \
      "$server_ips" \
      "${recursors[@]}"
  fi

  generate_systemd_config "$SYSTEMD_CONFIG_PATH" "$config_dir" "$data_dir" "$systemd_stdout" "$systemd_stderr" "$bin_dir" "$user" "${environment[@]}"

  start_consul

  if [[ "$server" == "true" ]]; then
    if [[ "${skip_bootstrap:-false}" == "true" ]]; then
      :
    elif [[ ! -f "$ACL_SWITCH_FILE" ]]; then
      log_info "Waiting for first start to be ready"
      until curl -sSf http://localhost:8500/v1/status/leader >/dev/null 2>&1; do sleep 1; done
      sleep 5
      log_info "Bootstrapping ACL"
      local root_token
      root_token=$(consul acl bootstrap -format=json | jq -r '.SecretID')
      echo '{"acl":{"tokens":{"default":"'"$root_token"'"}}}' > "$AGENT_TOKEN_FILE"
      chmod 600 "$AGENT_TOKEN_FILE"
      touch "$ACL_SWITCH_FILE"

      # Persist the real ACL token so re-runs (join path above) can recover it.
      mkdir -p /opt/consul
      printf '%s' "$root_token" > /opt/consul/acl.token
      chmod 600 /opt/consul/acl.token

      log_info "Re-generate config with deny + token and restart"
      generate_consul_config "$server" "$root_token" "$config_dir" "$user" \
        "$cluster_tag_name" "$CLUSTER_SIZE_INSTANCE_METADATA_KEY_NAME" "$datacenter" \
        "$enable_gossip_encryption" "$gossip_encryption_key" \
        "$enable_rpc_encryption" "$verify_server_hostname" \
        "$ca_path" "$cert_file_path" "$key_file_path" \
        "$cleanup_dead_servers" "$last_contact_threshold" \
        "$max_trailing_logs" "$server_stabilization_time" \
        "$redundancy_zone_tag" "$disable_upgrade_migration" \
        "$upgrade_version_tag" "$instance_ip_address" "$server_ips" "${recursors[@]}"

      # Immediate second restart (within same script)
      systemctl daemon-reload
      systemctl restart consul.service || true
      rm -rf $AGENT_TOKEN_FILE
      rm -rf $ACL_SWITCH_FILE

      if [[ -n "${root_token:-}" ]]; then
          script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
          env_file="$script_dir/.env"
          # If CONSUL_ACL_TOKEN line exists in file, replace it; otherwise append it
          if grep -q '^export CONSUL_ACL_TOKEN=' "$env_file" 2>/dev/null; then
              sed -i "s|^export CONSUL_ACL_TOKEN=.*|export CONSUL_ACL_TOKEN=$root_token|" "$env_file"
          fi
      fi
    fi
  fi
}

run "$@"

