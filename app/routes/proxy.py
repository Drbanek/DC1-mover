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
    image = "alpine:3.22"
    await ensure_image(proxy_id, image)
    name = "dsm-proxy-" + uuid.uuid4().hex[:10]
    payload = {
        "Image": image,
        "Cmd": ["sh", "-ec", cmd],
        "Env": env or [],
        "HostConfig": {"AutoRemove": False}
    }
    if host_network:
        payload["HostConfig"]["NetworkMode"] = "host"
    if binds:
        payload["HostConfig"]["Binds"] = binds
    created = await docker_request(proxy_id, "POST", "/containers/create", params={"name": name}, json=payload)
    if created.status_code != 201:
        raise RuntimeError("PROXY helper create failed: " + created.text)
    cid = created.json()["Id"]
    try:
        started = await docker_request(proxy_id, "POST", "/containers/" + cid + "/start")
        if started.status_code not in (204, 304):
            raise RuntimeError("PROXY helper start failed: " + started.text)
        code = await wait_container(proxy_id, cid, timeout=30)
        if code != 0:
            logs = await docker_request(proxy_id, "GET", "/containers/" + cid + "/logs", params={"stdout": "1", "stderr": "1"})
            raise RuntimeError("PROXY helper failed (" + str(code) + "): " + logs.text[-1000:])
    finally:
        await remove_container(proxy_id, cid)


async def _write_dynamic_file(proxy_id, filename, content):
    if not re.fullmatch(r"[a-z0-9._-]+\.ya?ml", filename):
        raise RuntimeError("Unsafe Traefik filename")
    await _run_proxy_helper(
        proxy_id,
        'umask 022; printf "%s" "$DSM_CONFIG" >"/dynamic/$DSM_FILE.tmp"; mv "/dynamic/$DSM_FILE.tmp" "/dynamic/$DSM_FILE"',
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
