#!/usr/bin/env bash
set -Eeuo pipefail
trap 'echo "[ERROR] line $LINENO: $BASH_COMMAND" >&2' ERR

[[ ${EUID} -eq 0 ]] || { echo "Run with sudo."; exit 1; }
source /etc/os-release
[[ "${ID:-}" == "ubuntu" ]] || { echo "DockerStackMover bootstrap supports Ubuntu Server."; exit 1; }

export DEBIAN_FRONTEND=noninteractive
IFACE=$(ip -4 route show default | awk 'NR==1{print $5}')
MGMT_IP=$(ip -o -4 addr show dev "$IFACE" scope global | awk 'NR==1{split($4,a,"/");print a[1]}')
[[ -n "$MGMT_IP" ]] || { echo "Unable to detect management IPv4."; exit 1; }

echo "DockerStackMover · first MGMT bootstrap"
echo "Detected address: $MGMT_IP"

if ! command -v docker >/dev/null 2>&1; then
  apt-get update
  apt-get install -y ca-certificates curl
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $VERSION_CODENAME stable" >/etc/apt/sources.list.d/docker.list
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
systemctl enable --now docker

install -d -m 0750 /opt/dockerstackmover
cat >/opt/dockerstackmover/compose.yaml <<'EOF'
services:
  dockerstackmover:
    image: ghcr.io/drbanek/dockerstackmover:latest
    pull_policy: always
    restart: unless-stopped
    ports:
      - "${MOVER_BIND_IP}:${MOVER_PORT}:8080"
    volumes:
      - data:/data
volumes:
  data:
EOF
cat >/opt/dockerstackmover/.env <<EOF
MOVER_BIND_IP=$MGMT_IP
MOVER_PORT=8082
EOF
chmod 600 /opt/dockerstackmover/.env
cd /opt/dockerstackmover
docker compose pull
docker compose up -d

for _ in $(seq 1 30); do
  if curl -fsS "http://$MGMT_IP:8082/api/setup/status" >/dev/null 2>&1; then
    echo
    echo "============================================================"
    echo "DockerStackMover is ready."
    echo "Open: http://$MGMT_IP:8082"
    echo "All further infrastructure setup continues in the web UI."
    echo "============================================================"
    exit 0
  fi
  sleep 2
done

echo "Container started, but the web health check did not become ready in time." >&2
docker compose ps
exit 1
