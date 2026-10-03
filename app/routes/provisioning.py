import asyncio
import ipaddress
import shlex
import socket
import time

import paramiko
from fastapi import Depends, HTTPException, Request

from ..core import app, require_csrf, user_permissions, client


def _ssh(host, port, username, password):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(hostname=host, port=int(port or 22), username=username, password=password,
              timeout=10, banner_timeout=10, auth_timeout=10)
    return c


def _run(c, command, password=None, timeout=300):
    if password is not None:
        command = "sudo -S -p '' bash -lc " + shlex.quote(command)
    stdin, stdout, stderr = c.exec_command(command, timeout=timeout, get_pty=password is not None)
    if password is not None:
        stdin.write(password + "\n"); stdin.flush()
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    rc = stdout.channel.recv_exit_status()
    if rc:
        raise RuntimeError((err or out or ("command failed: " + str(rc))).strip())
    return out.strip()


def _provision(payload):
    host = str(payload.get("host") or "").strip()
    user = str(payload.get("ssh_user") or "").strip()
    password = str(payload.get("ssh_password") or "")
    hub_host = str(payload.get("hub_host") or "").strip()
    hub_user = str(payload.get("hub_ssh_user") or "").strip()
    hub_password = str(payload.get("hub_ssh_password") or "")
    site = str(payload.get("site") or "").strip().upper()
    role = str(payload.get("role") or "NODE").strip().upper()
    lan_ip = str(payload.get("lan_ip") or "").strip()
    mgmt_ip = str(payload.get("management_ip") or "").strip()
    hub_mgmt_ip = str(payload.get("hub_management_ip") or "10.200.0.8").strip()
    hub_endpoint = str(payload.get("hub_endpoint") or "").strip()
    name = str(payload.get("name") or (site + "-" + role)).strip().upper()
    ssh_port = int(payload.get("ssh_port") or 22)
    hub_ssh_port = int(payload.get("hub_ssh_port") or 22)
    if not all((host,user,password,hub_host,hub_user,hub_password,site,lan_ip,mgmt_ip,hub_endpoint)):
        raise ValueError("Chybí povinné provisioning údaje.")
    ipaddress.ip_address(host); ipaddress.ip_address(lan_ip); ipaddress.ip_address(mgmt_ip); ipaddress.ip_address(hub_mgmt_ip)
    if not mgmt_ip.startswith("10.200."):
        raise ValueError("Management IP musí být z overlay 10.200.0.0/16.")
    steps=[]
    target=_ssh(host,ssh_port,user,password)
    hub=None
    try:
        steps.append("SSH target OK")
        pre=_run(target,"source /etc/os-release; test \"$ID\" = ubuntu; ip -4 route show default | head -1; command -v sudo >/dev/null")
        steps.append("Pre-flight OK: "+pre.splitlines()[-1])
        if host != lan_ip:
            netcmd=f"""IF=$(ip -4 route show default | awk 'NR==1{{print $5}}'); GW=$(ip -4 route show default | awk 'NR==1{{print $3}}'); CIDR=$(ip -o -4 addr show dev "$IF" scope global | awk 'NR==1{{print $4}}'); PREFIX="${{CIDR#*/}}"; test "$PREFIX" = 24; cat >/etc/netplan/99-dockerstackmover.yaml <<EOF
network:
  version: 2
  ethernets:
    $IF:
      dhcp4: false
      addresses: [{lan_ip}/24]
      routes:
        - to: default
          via: $GW
      nameservers:
        addresses: [1.1.1.1,8.8.8.8]
EOF
netplan generate
nohup sh -c 'sleep 2; netplan apply' >/tmp/dsm-netplan.log 2>&1 &"""
            _run(target,netcmd,password)
            target.close(); target=None
            last=None
            for _ in range(20):
                time.sleep(2)
                try:
                    target=_ssh(lan_ip,ssh_port,user,password); break
                except Exception as exc: last=exc
            if target is None:
                raise RuntimeError("LAN IP byla změněna, ale SSH na nové adrese "+lan_ip+" není dostupné: "+str(last))
            host=lan_ip
            steps.append("LAN IP changed to "+lan_ip)
        _run(target,"apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y wireguard ca-certificates curl nftables",password,600)
        _run(target,"install -d -m 700 /etc/wireguard; if [ ! -f /etc/wireguard/dsm.key ]; then umask 077; wg genkey | tee /etc/wireguard/dsm.key | wg pubkey > /etc/wireguard/dsm.pub; fi",password)
        peer_pub=_run(target,"cat /etc/wireguard/dsm.pub",password)
        steps.append("WireGuard keypair OK")
        hub=_ssh(hub_host,hub_ssh_port,hub_user,hub_password)
        hub_pub=_run(hub,"cat /etc/wireguard/hub.pub",hub_password)
        hub_conf="/etc/wireguard/wg-dsm.conf"
        add=f"""grep -qF {shlex.quote(peer_pub)} {hub_conf} || cat >>{hub_conf} <<'EOF'

# {name}
[Peer]
PublicKey = {peer_pub}
AllowedIPs = {mgmt_ip}/32
EOF
wg set wg-dsm peer {peer_pub} allowed-ips {mgmt_ip}/32"""
        _run(hub,add,hub_password)
        steps.append("Peer registered on MAIN")
        cfg=f"""cat >/etc/wireguard/wg-dsm.conf <<'EOF'
[Interface]
Address = {mgmt_ip}/32
PrivateKey = $(cat /etc/wireguard/dsm.key)
EOF
sed -i "s|PrivateKey = .*|PrivateKey = $(cat /etc/wireguard/dsm.key)|" /etc/wireguard/wg-dsm.conf
cat >>/etc/wireguard/wg-dsm.conf <<'EOF'

[Peer]
PublicKey = {hub_pub}
Endpoint = {hub_endpoint}
AllowedIPs = 10.200.0.0/16
PersistentKeepalive = 25
EOF
chmod 600 /etc/wireguard/wg-dsm.conf
systemctl enable --now wg-quick@wg-dsm"""
        _run(target,cfg,password)
        steps.append("WireGuard started")
        time.sleep(2)
        hs=_run(hub,f"wg show wg-dsm latest-handshakes | grep -F {shlex.quote(peer_pub)} || true",hub_password)
        if not hs or hs.split()[-1]=="0":
            raise RuntimeError("WireGuard handshake se nepotvrdil.")
        steps.append("WireGuard handshake OK")
        docker="""if ! command -v docker >/dev/null; then install -m 0755 -d /etc/apt/keyrings; curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc; chmod a+r /etc/apt/keyrings/docker.asc; . /etc/os-release; echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $VERSION_CODENAME stable" >/etc/apt/sources.list.d/docker.list; apt-get update; DEBIAN_FRONTEND=noninteractive apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin; fi
systemctl enable --now docker
docker rm -f portainer_agent >/dev/null 2>&1 || true
docker pull portainer/agent:2.45.1
docker run -d --name portainer_agent --restart=always -p 9001:9001 -v /var/run/docker.sock:/var/run/docker.sock -v /var/lib/docker/volumes:/var/lib/docker/volumes -v /:/host portainer/agent:2.45.1 >/dev/null"""
        _run(target,docker,password,900)
        steps.append("Docker + Portainer Agent OK")
        fw=f"""systemctl enable --now nftables
nft delete table inet dockerstackmover-bootstrap >/dev/null 2>&1 || true
nft add table inet dockerstackmover-bootstrap
nft 'add chain inet dockerstackmover-bootstrap input {{ type filter hook input priority -10; policy accept; }}'
nft add rule inet dockerstackmover-bootstrap input ct state established,related accept
nft add rule inet dockerstackmover-bootstrap input iifname lo accept
nft add rule inet dockerstackmover-bootstrap input iifname wg-dsm ip saddr {hub_mgmt_ip} tcp dport '{{ 9001, 9100 }}' accept
nft add rule inet dockerstackmover-bootstrap input tcp dport '{{ 9001, 9100 }}' drop"""
        _run(target,fw,password)
        steps.append("Management firewall OK")
        # Test the exact central management path before returning success.
        _run(hub,f"timeout 4 bash -lc '</dev/tcp/{mgmt_ip}/9001'")
        steps.append("MAIN -> "+mgmt_ip+":9001 OK")
        return {"ok":True,"name":name,"site":site,"role":role,"lan_ip":lan_ip,"management_ip":mgmt_ip,
                "wireguard_public_key":peer_pub,"steps":steps}
    finally:
        try:
            if target: target.close()
        except Exception: pass
        if hub:
            try: hub.close()
            except Exception: pass


@app.post("/api/provisioning/server")
async def provision_server(request: Request, session=Depends(require_csrf)):
    if "admin" not in user_permissions(session.get("user","")):
        raise HTTPException(403,"Permission denied")
    payload=await request.json()
    try:
        result=await asyncio.to_thread(_provision,payload)
    except Exception as exc:
        raise HTTPException(502,"Provisioning selhal: "+str(exc))
    # Register Portainer Agent only after WG and firewall have been verified.
    try:
        async with client() as c:
            r=await c.post("/api/endpoints",data={"Name":result["name"],"EndpointCreationType":"2","URL":"tcp://"+result["management_ip"]+":9001"})
        if r.status_code not in (200,201,409):
            raise HTTPException(r.status_code,"Portainer registration failed: "+r.text)
        if r.status_code in (200,201):
            result["portainer_endpoint_id"]=(r.json() or {}).get("Id")
            result["steps"].append("Portainer environment registered")
        else:
            result["steps"].append("Portainer environment already exists")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502,"WG je funkční, ale registrace do Portaineru selhala: "+str(exc))
    return result
