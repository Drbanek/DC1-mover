import json
import re
import uuid

from fastapi import Depends, HTTPException

from ..core import *


def _safe_name(value):
    value = re.sub(r"[^a-z0-9-]+", "-", str(value or "").lower()).strip("-")
    return value[:50] or "stack"


def _site_proxy(site):
    site = str(site or "").strip().upper()
    settings = get_endpoint_settings()
    matches = [(eid, s) for eid, s in settings.items()
               if (s.get("role") or "").upper() == "PROXY" and (s.get("site") or "").strip().upper() == site]
    if len(matches) != 1:
        raise RuntimeError("Site " + site + " musí mít právě jeden endpoint s rolí PROXY; nalezeno: " + str(len(matches)))
    return matches[0][0]


async def _run_proxy_helper(proxy_id, cmd, env=None, host_network=False, binds=None):
    """Run a short-lived helper through Portainer's stack API.

    Portainer performs the Docker create/start locally on the endpoint, avoiding
    Docker POST /containers/{id}/start through the reverse proxy.
    """
    name = "dsm-proxy-" + uuid.uuid4().hex[:10]
    environment = {}
    for item in (env or []):
        key, _, value = item.partition("=")
        environment[key] = value
    service = [
        "services:",
        "  helper:",
        "    image: alpine:3.22",
        "    command: [\"sh\", \"-ec\", " + json.dumps(cmd) + "]",
        "    restart: \"no\"",
    ]
    if host_network:
        service.append("    network_mode: host")
    if environment:
        service.append("    environment:")
        for key, value in environment.items():
            service.append("      " + key + ": " + json.dumps(value))
    if binds:
        service.append("    volumes:")
        for bind in binds:
            service.append("      - " + json.dumps(bind))
    stack_file = "\n".join(service) + "\n"
    stack = await create_target_stack(proxy_id, name, stack_file, [])
    stack_id = int(stack.get("Id") or stack.get("id") or 0)
    if not stack_id:
        raise RuntimeError("PROXY helper stack did not return an ID")
    try:
        code = None
        logs_text = ""
        for _ in range(60):
            containers = (await docker_get(proxy_id, "/containers/json", params={"all": "1", "filters": json.dumps({"label": ["com.docker.compose.project=" + name]})})).json()
            if containers:
                state = str(containers[0].get("State") or "").lower()
                status = str(containers[0].get("Status") or "")
                if state == "exited":
                    match = re.search(r"Exited \((\d+)\)", status)
                    code = int(match.group(1)) if match else 1
                    if code != 0:
                        cid = containers[0].get("Id")
                        if cid:
                            logs = await docker_request(proxy_id, "GET", "/containers/" + cid + "/logs", params={"stdout": "1", "stderr": "1"})
                            logs_text = logs.text[-1000:]
                    break
            await asyncio.sleep(0.5)
        if code is None:
            raise RuntimeError("PROXY helper timeout")
        if code != 0:
            raise RuntimeError("PROXY helper failed (" + str(code) + "): " + logs_text)
    finally:
        async with client() as hc:
            await hc.delete("/api/stacks/" + str(stack_id), params={"endpointId": proxy_id})


async def _write_dynamic_file(proxy_id, filename, content):
    if not re.fullmatch(r"[a-z0-9._-]+\.ya?ml", filename):
        raise RuntimeError("Unsafe Traefik filename")
    await _run_proxy_helper(
        proxy_id,
        'umask 022; tmp="/dynamic/.$DSM_FILE.tmp.$"; printf "%s" "$DSM_CONFIG" >"$tmp"; mv -f "$tmp" "/dynamic/$DSM_FILE"; test -s "/dynamic/$DSM_FILE"',
        env=["DSM_FILE=" + filename, "DSM_CONFIG=" + content],
        binds=["/opt/traefik/dynamic:/dynamic"],
    )


async def _remove_dynamic_file(proxy_id, filename):
    if not re.fullmatch(r"[a-z0-9._-]+\.ya?ml", filename):
        raise RuntimeError("Unsafe Traefik filename")
    await _run_proxy_helper(
        proxy_id,
        'rm -f "/dynamic/$DSM_FILE"',
        env=["DSM_FILE=" + filename],
        binds=["/opt/traefik/dynamic:/dynamic"],
    )


async def sync_stack_proxy(detail, endpoint_id):
    domains = detail.get("domains") or []
    if not domains:
        return {"configured": False, "reason": "no proxy metadata"}
    settings = get_endpoint_settings()
    node = settings.get(int(endpoint_id), {})
    site = (node.get("site") or "").strip().upper()
    lan_ip = (node.get("lan_ip") or "").strip()
    if not site:
        raise RuntimeError("Endpoint nemá nastavenou Site / lokalitu")
    if not lan_ip:
        raise RuntimeError("Endpoint nemá nastavenou LAN IP. Management IP 10.200.x.x se pro aplikační proxy záměrně nepoužívá.")
    proxy_id = _site_proxy(site)
    stack_name = detail["stack"]["name"]
    base = _safe_name(stack_name)
    routers = []
    services = []
    tested = set()
    for index, domain in enumerate(domains, start=1):
        host = str(domain.get("host") or "").strip().lower().rstrip(".")
        scheme = str(domain.get("scheme") or "http").strip().lower()
        try:
            port = int(domain.get("port"))
        except Exception:
            raise RuntimeError("Neplatný dc1.proxy.port pro " + (host or stack_name))
        if not re.fullmatch(r"[a-z0-9.-]+", host) or "." not in host:
            raise RuntimeError("Neplatný dc1.proxy.host: " + host)
        if scheme not in ("http", "https") or not (1 <= port <= 65535):
            raise RuntimeError("Neplatný proxy backend pro " + host)
        key = (lan_ip, port)
        if key not in tested:
            await _run_proxy_helper(proxy_id, "nc -z -w 5 " + lan_ip + " " + str(port), host_network=True)
            tested.add(key)
        svc = base + "-" + str(index)
        web = svc + "-web"
        secure = svc + "-secure"
        url = scheme + "://" + lan_ip + ":" + str(port)
        routers.extend([
            "    " + web + ":",
            "      rule: " + json.dumps("Host(`" + host + "`)"),
            "      entryPoints: [web]",
            "      service: " + svc,
            "    " + secure + ":",
            "      rule: " + json.dumps("Host(`" + host + "`)"),
            "      entryPoints: [websecure]",
            "      service: " + svc,
            "      tls:",
            "        certResolver: " + setting_get("traefik_cert_resolver", "letsencrypt"),
        ])
        services.extend([
            "    " + svc + ":",
            "      loadBalancer:",
            "        servers:",
            "          - url: " + json.dumps(url),
        ])
    config = "\n".join(["http:", "  routers:"] + routers + ["  services:"] + services) + "\n"
    filename = "dsm-" + base + ".yml"
    await _write_dynamic_file(proxy_id, filename, config)
    return {"configured": True, "site": site, "proxy_endpoint_id": proxy_id, "lan_ip": lan_ip, "file": filename, "domains": len(domains)}


async def remove_stack_proxy(stack_name, site):
    if not site:
        return
    proxy_id = _site_proxy(site)
    await _remove_dynamic_file(proxy_id, "dsm-" + _safe_name(stack_name) + ".yml")


@app.post("/api/stacks/{stack_id}/proxy/sync")
async def proxy_sync(stack_id: int, session=Depends(require_csrf)):
    if "migrations" not in user_permissions(session.get("user", "")) and "admin" not in user_permissions(session.get("user", "")):
        raise HTTPException(403, "Permission denied")
    from .general import build_detail
    detail = await build_detail(stack_id)
    try:
        return await sync_stack_proxy(detail, int(detail["stack"]["endpoint_id"]))
    except Exception as exc:
        raise HTTPException(502, "Traefik sync selhal: " + str(exc))
