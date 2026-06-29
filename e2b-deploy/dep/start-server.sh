#!/bin/bash
# This script is meant to be run in the Startup Script of each Compute Instance while it's booting. The script uses the
# run-nomad and run-consul scripts to configure and start Consul and Nomad in server mode. Note that this script
# assumes it's running in a Google IMage built from the Packer template in examples/nomad-consul-image/nomad-consul.json.
set -a
[[ -f "$(dirname "$0")/.env" ]] && source "$(dirname "$0")/.env"
set +a

set -e

if [[ -z "$1" ]]; then
  echo "Usage: $0 <INSTANCE_IP_ADDRESS>"
  exit 1
fi

INSTANCE_IP_ADDRESS="$1"

ulimit -n 65536
export GOMAXPROCS='nproc'
chmod +x ./run-consul.sh ./run-nomad.sh ./install-consul.sh ./install-nomad.sh
./install-consul.sh --version ${CONSUL_VERSION}
./install-nomad.sh --version ${NOMAD_VERSION}

./run-consul.sh --server --server-ips "${SERVER_IPS}" --instance-ip-address "${INSTANCE_IP_ADDRESS}"

set -a
[[ -f "$(dirname "$0")/.env" ]] && source "$(dirname "$0")/.env"
set +a

./run-nomad.sh --server --num-servers "${NUM_SERVERS}" --consul-token "${CONSUL_ACL_TOKEN}" --instance-ip-address "${INSTANCE_IP_ADDRESS}"

