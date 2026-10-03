import ipaddress
import secrets
import shlex

import httpx
from fastapi import Depends, HTTPException, Request

from ..core import app, require_csrf, user_permissions, setting_set
from .provisioning import _ssh, _run


@app.post("/api/bootstrap/portainer")
async def bootstrap_portainer(request: Request, session=Depends(require_csrf)):
    if "admin" not in user_permissions(session.get("user", "")):
        raise HTTPException(403, "Permission denied")
    p = await request.json()
    host = str(p.get("host") or "").strip()
    user = str(p.get("ssh_user") or "").strip()
    password = str(p.get("ssh_password") or "")
    site = str(p.get("site") or "MAIN").strip().upper()
    lan_ip = str(p.get("lan_ip") or host).strip()
    wg_endpoint = str(p.get("wg_endpoint") or (lan_ip + ":51820")).strip()
    ssh_port = int(p.get("ssh_port") or 22)
    if not host or not user or not password or not lan_ip:
        raise HTTPException(400, "Vyplň SSH adresu, uživatele, heslo a LAN IP Portaineru.")
    try:
        ipaddress.ip_address(host)
        ipaddress.ip_address(lan_ip)
    except ValueError as exc:
        raise HTTPException(400, "Neplatná IPv4 adresa: " + str(exc))

    c = None
    try:
        c = await __import__("asyncio").to_thread(_ssh, host, ssh_port, user, password)
        cmd = r"""set -e
source /etc/os-release
test "$ID" = ubuntu
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl wireguard
if ! command -v docker >/dev/null 2>&1; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $VERSION_CODENAME stable" >/etc/apt/sources.list.d/docker.list
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
systemctl enable --now docker
hostnamectl set-hostname __HOSTNAME__
install -d -m 700 /etc/wireguard
if [ ! -f /etc/wireguard/hub.key ]; then
  umask 077
  wg genkey | tee /etc/wireguard/hub.key | wg pubkey >/etc/wireguard/hub.pub
fi
PRIV=$(cat /etc/wireguard/hub.key)
cat >/etc/wireguard/wg-dsm.conf <<EOF
[Interface]
Address = 10.200.0.8/16
ListenPort = 51820
PrivateKey = $PRIV
EOF
chmod 600 /etc/wireguard/wg-dsm.conf
printf 'net.ipv4.ip_forward=1\n' >/etc/sysctl.d/99-dockerstackmover-wg-forward.conf
sysctl -w net.ipv4.ip_forward=1 >/dev/null
systemctl enable --now wg-quick@wg-dsm
docker volume create portainer_data >/dev/null
docker rm -f portainer >/dev/null 2>&1 || true
docker pull portainer/portainer-ce:2.33.6
docker run -d --name portainer --restart=always -p 9443:9443 -v /var/run/docker.sock:/var/run/docker.sock -v portainer_data:/data portainer/portainer-ce:2.33.6 >/dev/null
for i in $(seq 1 45); do
  curl -kfsS https://127.0.0.1:9443/api/status >/dev/null 2>&1 && exit 0
  sleep 2
done
exit 51
""".replace("__HOSTNAME__", shlex.quote(site + "-PORTAINER"))
        await __import__("asyncio").to_thread(_run, c, cmd, password, 1200)
        hub_pub = await __import__("asyncio").to_thread(_run, c, "cat /etc/wireguard/hub.pub", password)
    except Exception as exc:
        raise HTTPException(502, "Bootstrap Portaineru selhal: " + str(exc))
    finally:
        if c:
            c.close()

    url = "https://" + lan_ip + ":9443"
    admin_password = secrets.token_urlsafe(18)
    try:
        async with httpx.AsyncClient(base_url=url, verify=False, timeout=20) as pc:
            init = await pc.post("/api/users/admin/init", json={"Username": "admin", "Password": admin_password})
            if init.status_code not in (200, 201, 409):
                raise RuntimeError("admin init HTTP " + str(init.status_code) + ": " + init.text[:200])
            auth = await pc.post("/api/auth", json={"Username": "admin", "Password": admin_password})
            if auth.status_code != 200:
                if init.status_code == 409:
                    raise RuntimeError("Portainer už je inicializovaný. Pro bezpečné převzetí je potřeba jeho existující API token.")
                raise RuntimeError("auth HTTP " + str(auth.status_code))
            jwt = auth.json().get("jwt")
            if not jwt:
                raise RuntimeError("Portainer auth nevrátil JWT.")
            me = await pc.get("/api/users/me", headers={"Authorization": "Bearer " + jwt})
            if me.status_code != 200:
                raise RuntimeError("users/me HTTP " + str(me.status_code))
            uid = me.json().get("Id")
            tok = await pc.post("/api/users/" + str(uid) + "/tokens",
                                headers={"Authorization": "Bearer " + jwt},
                                json={"description": "DockerStackMover bootstrap", "password": admin_password})
            if tok.status_code not in (200, 201):
                raise RuntimeError("API token HTTP " + str(tok.status_code) + ": " + tok.text[:200])
            api_key = tok.json().get("rawAPIKey") or tok.json().get("apiKey")
            if not api_key:
                raise RuntimeError("Portainer nevrátil API key.")
            check = await pc.get("/api/endpoints", headers={"X-API-Key": api_key})
            if check.status_code != 200:
                raise RuntimeError("ověření API key selhalo HTTP " + str(check.status_code))
    except Exception as exc:
        raise HTTPException(502, "Portainer běží, ale automatická inicializace API selhala: " + str(exc))

    setting_set("portainer_url", url)
    setting_set("portainer_token", api_key, True)
    setting_set("main_site", site)
    setting_set("wg_hub_lan_ip", lan_ip)
    setting_set("wg_hub_endpoint", wg_endpoint)
    setting_set("wg_hub_public_key", hub_pub)
    return {
        "ok": True,
        "site": site,
        "portainer_url": url,
        "hub_management_ip": "10.200.0.8",
        "hub_public_key": hub_pub,
        "wg_endpoint": wg_endpoint,
        "portainer_admin_user": "admin",
        "portainer_admin_password": admin_password,
        "password_is_one_time": True,
    }
