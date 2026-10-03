#!/usr/bin/env bash
set -Eeuo pipefail
trap 'echo "[ERROR] line $LINENO: $BASH_COMMAND" >&2' ERR

[[ ${EUID} -eq 0 ]] || { echo "Run with sudo."; exit 1; }
source /etc/os-release
[[ "${ID:-}" == "ubuntu" ]] || { echo "DockerStackMover bootstrap supports Ubuntu Server."; exit 1; }

export DEBIAN_FRONTEND=noninteractive
IFACE=$(ip -4 route show default | awk 'NR==1{print $5}')
CURRENT_CIDR=$(ip -o -4 addr show dev "$IFACE" scope global | awk 'NR==1{print $4}')
MGMT_IP=${CURRENT_CIDR%/*}
PREFIX=${CURRENT_CIDR#*/}
[[ -n "$MGMT_IP" && -n "$PREFIX" ]] || { echo "Unable to detect management IPv4."; exit 1; }

# DSM address convention: MGMT always uses host address .10 in the detected IPv4 subnet.
IFS=. read -r OCT1 OCT2 OCT3 _ <<<"$MGMT_IP"
TARGET_IP="$OCT1.$OCT2.$OCT3.10"

echo "DockerStackMover · first MGMT bootstrap"
echo "Detected address: $MGMT_IP/$PREFIX"
echo "Target MGMT address: $TARGET_IP/$PREFIX"

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

# Prepare the application first on the current address. The permanent IP
# switch is intentionally the final step because an SSH session can be lost.
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
    # Final step: switch MGMT to the DSM-standard .10 address.
    if [[ "$MGMT_IP" != "$TARGET_IP" ]]; then
      if ping -c 1 -W 1 "$TARGET_IP" >/dev/null 2>&1; then
        echo "ERROR: Target MGMT address $TARGET_IP is already in use; IP was not changed." >&2
        exit 1
      fi

      NETPLAN=$(find /etc/netplan -maxdepth 1 -type f \( -name '*.yaml' -o -name '*.yml' \) | head -n1)
      [[ -n "$NETPLAN" ]] || { echo "ERROR: No Netplan configuration found; IP was not changed." >&2; exit 1; }
      GATEWAY=$(ip -4 route show default | awk 'NR==1{print $3}')
      DNS=$(resolvectl dns "$IFACE" 2>/dev/null | awk -F': ' 'NR==1{print $2}' | xargs | tr ' ' ',')
      [[ -n "$DNS" ]] || DNS="$GATEWAY"

      cp -a "$NETPLAN" "$NETPLAN.dsm-backup"
      cat >"$NETPLAN" <<EOF
network:
  version: 2
  ethernets:
    $IFACE:
      dhcp4: false
      addresses:
        - $TARGET_IP/$PREFIX
      routes:
        - to: default
          via: $GATEWAY
      nameservers:
        addresses: [$DNS]
EOF
      chmod 600 "$NETPLAN"

      # Bind DSM to the new address before applying Netplan.
      sed -i "s/^MOVER_BIND_IP=.*/MOVER_BIND_IP=$TARGET_IP/" /opt/dockerstackmover/.env

      echo
      echo "============================================================"
      echo "DockerStackMover is installed."
      echo "MGMT address is now changing: $MGMT_IP -> $TARGET_IP"
      echo "Your SSH session may disconnect now. This is expected."
      echo "Reconnect to: $TARGET_IP"
      echo "Web UI: http://$TARGET_IP:8082"
      echo "============================================================"

      # Apply asynchronously so the final instructions reach the terminal
      # before the old address disappears. Restart DSM after the new IP exists.
      nohup bash -c "sleep 3; netplan apply; docker compose --env-file /opt/dockerstackmover/.env -f /opt/dockerstackmover/compose.yaml up -d --force-recreate" >/var/log/dockerstackmover-ip-switch.log 2>&1 &
      exit 0
    fi

    echo
    echo "============================================================"
    echo "DockerStackMover is ready."
    echo "Open: http://$TARGET_IP:8082"
    echo "All further infrastructure setup continues in the web UI."
    echo "============================================================"
    exit 0
  fi
  sleep 2
done

echo "Container started, but the web health check did not become ready in time." >&2
docker compose ps
exit 1
