#!/usr/bin/env bash
# Uninstall / tear down the Consul agent installed by run-consul.sh on this host.
#
# This is the Consul counterpart of uninstall-nomad.sh. HashiCorp/Gruntwork do NOT
# ship an uninstall script (their teardown model is `consul leave` + destroy the VM),
# so this is hand-written to mirror uninstall-nomad.sh and to clean up exactly the
# paths that THIS repo's run-consul.sh creates:
#
#   /etc/systemd/system/consul.service   systemd unit (ExecStop already runs `consul leave`)
#   /etc/consul.d                        config dir (default.json + agent-token.json)
#   /data/consul                         data dir
#   /usr/local/bin/consul                binary
#   /opt/consul/acl.token                persisted ACL token (run-consul.sh recovers from this)
#   /tmp/consul-acl-bootstrap-done       one-time ACL-bootstrap switch flag
#
# Removing the last two is the whole reason this script must exist: run-consul.sh
# explicitly assumes they are "wiped together with the cluster on build.sh -d / -u".
# If they survive a teardown, the next install bootstraps a NEW token while the stale
# /opt/consul/acl.token is still on disk, and the join/recovery path can hand deploy.sh
# a wrong token -> auth failures that only show up on reinstall.

#set -euo pipefail
source /opt/e2b-infra/.env 2>/dev/null || true

DRY_RUN=true                          # Dry-run by default; set --force to delete
CONSUL_CONFIG_DIR="/etc/consul.d"     # Corresponds to run-consul.sh --config-dir default
CONSUL_DATA_DIR="/data/consul"        # Corresponds to run-consul.sh --data-dir default
CONSUL_BIN_DIR="/usr/local/bin"       # Corresponds to run-consul.sh --bin-dir default

# Fixed ACL/state files created by run-consul.sh (not overridable via flags)
CONSUL_ACL_TOKEN_FILE="/opt/consul/acl.token"
CONSUL_AGENT_TOKEN_FILE="/etc/consul.d/agent-token.json"
CONSUL_ACL_SWITCH_FILE="/tmp/consul-acl-bootstrap-done"
SYSTEMD_PATH="/etc/systemd/system/consul.service"

function log_info  { echo -e "\033[1;32m[INFO]\033[0m $*"; }
function log_warn  { echo -e "\033[1;33m[WARN]\033[0m $*"; }
function log_error { echo -e "\033[1;31m[ERROR]\033[0m $*"; }

function safe_rm {
  local path="$1"
  if [[ ! -e "$path" ]]; then
    log_warn "$path does not exist, skipping"
    return
  fi
  if [[ "$DRY_RUN" == "true" ]]; then
    log_info "[Dry-run] Will remove: $path"
  else
    log_info "Removing: $path"
    rm -rf "$path"
  fi
}

# Gracefully leave the cluster before stopping, so peers deregister this node
# instead of marking it "failed". systemctl stop also triggers ExecStop=consul leave,
# but we do it explicitly (with token) in case the unit is already gone.
function consul_leave {
  if ! command -v consul >/dev/null 2>&1; then
    log_warn "consul binary not found, skipping graceful leave"
    return
  fi
  if [[ "$DRY_RUN" == "true" ]]; then
    log_info "[Dry-run] Will run: consul leave"
    return
  fi
  if curl -sSf http://localhost:8500/v1/status/leader >/dev/null 2>&1; then
    log_info "Gracefully leaving the Consul cluster"
    CONSUL_HTTP_TOKEN="${CONSUL_ACL_TOKEN:-}" consul leave 2>/dev/null \
      || log_warn "consul leave failed (non-fatal)"
  else
    log_warn "Consul HTTP API not reachable on :8500, skipping graceful leave"
  fi
}

function main {
  # 1. Leave the cluster, then stop & disable the service
  consul_leave
  if [[ "$DRY_RUN" != "true" ]]; then
    sudo systemctl stop    consul || true
    sudo systemctl disable consul || true
    # Backstop in case the process is not managed by systemd for any reason
    pkill -x consul 2>/dev/null || true
  fi

  # 2. Remove systemd unit
  safe_rm "$SYSTEMD_PATH"

  # 3. Remove config & data directories (config dir also contains agent-token.json)
  safe_rm "$CONSUL_CONFIG_DIR"
  safe_rm "$CONSUL_DATA_DIR"

  # 4. Remove consul binary (comment out if shared with other tooling)
  if [[ -f "$CONSUL_BIN_DIR/consul" ]]; then
    safe_rm "$CONSUL_BIN_DIR/consul"
  fi

  # 5. Remove persisted ACL token + one-time bootstrap flag.
  #    THIS is the part official/AWS scripts cannot cover: run-consul.sh invented
  #    these files, and leaving them behind breaks token recovery on reinstall.
  safe_rm "$CONSUL_ACL_TOKEN_FILE"
  safe_rm "$CONSUL_AGENT_TOKEN_FILE"     # usually already deleted post-bootstrap; safe to re-remove
  safe_rm "$CONSUL_ACL_SWITCH_FILE"
  # /opt/consul may be left empty after acl.token removal; drop it if nothing else lives there
  if [[ -d /opt/consul ]] && [[ -z "$(ls -A /opt/consul 2>/dev/null)" ]]; then
    safe_rm "/opt/consul"
  fi

  # 6. Reload systemd
  if [[ "$DRY_RUN" != "true" ]]; then
    log_info "Reloading systemd daemon"
    sudo systemctl daemon-reload
    sudo systemctl reset-failed 2>/dev/null || true
  fi

  log_info "Uninstall finished"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --force|-f)
      DRY_RUN=false
      shift
      ;;
    --config-dir)
      CONSUL_CONFIG_DIR="$2"
      shift 2
      ;;
    --data-dir)
      CONSUL_DATA_DIR="$2"
      shift 2
      ;;
    --bin-dir)
      CONSUL_BIN_DIR="$2"
      shift 2
      ;;
    --help|-h)
      cat <<EOF
Usage: $0 [OPTIONS]

OPTIONS:
  -f, --force        Perform actual deletion without prompting (dry-run by default)
  --config-dir PATH  Config directory path (default /etc/consul.d)
  --data-dir PATH    Data directory path (default /data/consul)
  --bin-dir PATH     Binary directory path (default /usr/local/bin)
  -h, --help         Show this help message
EOF
      exit 0
      ;;
    *)
      log_error "Unknown argument: $1"
      exit 1
      ;;
  esac
done

if [[ "$DRY_RUN" == "true" ]]; then
  log_info "Currently in **Dry-run** mode; only paths to be removed will be printed."
  log_info "To execute removal, run again with: $0 --force"
fi

main
