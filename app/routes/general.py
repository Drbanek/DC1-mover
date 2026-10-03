from ..core import *

@app.get("/health")
async def health():
    return {"status": "ok", "version": "1.14.2"}

@app.get("/api/inventory")
async def inventory(session=Depends(require_permission("migrations"))):
    endpoints = await get_endpoints(); stacks = await get_stacks(); enabled_ids = {int(e["Id"]) for e in migration_endpoints(endpoints)}; endpoint_names = {e["Id"]: e["Name"] for e in endpoints}
    return [{"id": s["Id"], "name": s["Name"], "endpoint_id": s["EndpointId"], "endpoint": endpoint_names.get(s["EndpointId"], "Endpoint " + str(s["EndpointId"])), "status": s["Status"]} for s in stacks if int(s.get("EndpointId") or 0) in enabled_ids]

@app.get("/api/stacks/{stack_id}/detail")
async def stack_detail(stack_id: int, session=Depends(require_permission("migrations"))): return await build_detail(stack_id)

@app.get("/api/endpoints/settings")
async def endpoint_settings_list(session=Depends(require_permission("admin"))):
    endpoints = await get_endpoints(); settings = get_endpoint_settings(); result = []
    for endpoint in endpoints:
        eid = int(endpoint["Id"]); setting = settings.get(eid, {})
        result.append({"id": eid, "name": endpoint.get("Name") or ("Endpoint " + str(eid)), "url": endpoint.get("URL") or "", "migration_enabled": bool(setting.get("migration_enabled", False)), "host_ip": setting.get("host_ip", ""), "lan_ip": setting.get("lan_ip", ""), "site": setting.get("site", ""), "public_ip": setting.get("public_ip", ""), "agent_url": setting.get("agent_url", ""), "agent_token_set": bool(setting.get("agent_token")), "role": (setting.get("role") or "NONE").upper()})
    return result

@app.put("/api/endpoints/{endpoint_id}/settings")
async def endpoint_settings_save(endpoint_id: int, request: Request, session=Depends(require_csrf)):
    if "admin" not in user_permissions(session.get("user","")): raise HTTPException(403,"Permission denied")
    endpoints = await get_endpoints()
    if not any(int(e["Id"]) == endpoint_id for e in endpoints): raise HTTPException(404, "Endpoint not found")
    payload = await request.json(); save_endpoint_setting(endpoint_id, bool(payload.get("migration_enabled")), str(payload.get("host_ip") or ""), str(payload.get("site") or ""), str(payload.get("public_ip") or ""), str(payload.get("agent_url") or ""), str(payload["agent_token"]) if payload.get("agent_token") else None, str(payload.get("role") or "NONE").upper(), str(payload.get("lan_ip") or ""))
    return {"ok": True}


@app.get("/api/endpoints/{endpoint_id}/remove-check")
async def endpoint_remove_check(endpoint_id: int, session=Depends(require_permission("admin"))):
    endpoints = await get_endpoints()
    endpoint = next((e for e in endpoints if int(e["Id"]) == endpoint_id), None)
    if not endpoint: raise HTTPException(404, "Endpoint not found")
    setting = get_endpoint_settings().get(endpoint_id, {})
    role = (setting.get("role") or "NONE").upper()
    if role not in ("NODE", "PROXY"):
        return {"allowed": False, "role": role, "reason": "Odebrat lze pouze NODE nebo PROXY."}
    stacks = [s for s in await get_stacks() if int(s.get("EndpointId") or 0) == endpoint_id]
    containers = (await docker_get(endpoint_id, "/containers/json", params={"all": "1"})).json()
    system_names = {"portainer_agent", "dockerstackmover-capacity-agent", "traefik"}
    workload_containers = []
    for item in containers:
        names = [str(x).lstrip("/") for x in (item.get("Names") or [])]
        name = names[0] if names else str(item.get("Id") or "")[:12]
        if name in system_names or name.startswith("dsm-proxy-"):
            continue
        workload_containers.append(name)
    if role == "NODE":
        blockers = []
        if stacks: blockers.append(str(len(stacks)) + " stacků")
        if workload_containers: blockers.append(str(len(workload_containers)) + " aplikačních kontejnerů")
        return {"allowed": not blockers, "role": role, "stacks": len(stacks), "workload_containers": workload_containers,
                "reason": "NODE je prázdný a lze jej bezpečně odebrat." if not blockers else "NODE nelze odebrat: " + ", ".join(blockers) + "."}
    # PROXY: only system containers may remain and there must be no application
    # dynamic Traefik files. The helper mounts the host directory read-only.
    proxy_files = []
    try:
        # We cannot return stdout from the helper, so use the Traefik container's
        # mounted dynamic directory through Docker exec for a read-only listing.
        traefik = next((x for x in containers if "traefik" in [n.lstrip("/") for n in (x.get("Names") or [])]), None)
        if not traefik:
            return {"allowed": False, "role": role, "reason": "PROXY nemá spuštěný Traefik."}
        ex = await docker_request(endpoint_id, "POST", "/containers/" + traefik["Id"] + "/exec",
                                  json={"AttachStdout": True, "AttachStderr": True, "Cmd": ["sh", "-c", "find /etc/traefik/dynamic -maxdepth 1 -type f -name '*.yml' -o -name '*.yaml' 2>/dev/null | sed 's#.*/##'"]})
        if ex.status_code != 201: raise RuntimeError("Docker exec create HTTP " + str(ex.status_code))
        start = await docker_request(endpoint_id, "POST", "/exec/" + ex.json()["Id"] + "/start", json={"Detach": False, "Tty": False})
        if start.status_code != 200: raise RuntimeError("Docker exec start HTTP " + str(start.status_code))
        raw = start.content.replace(b"\x01\x00\x00\x00", b"").replace(b"\x02\x00\x00\x00", b"").decode("utf-8", "ignore")
        proxy_files = [x.strip() for x in raw.splitlines() if x.strip().endswith((".yml", ".yaml"))]
    except Exception as exc:
        return {"allowed": False, "role": role, "reason": "Nelze bezpečně ověřit Traefik konfiguraci: " + str(exc)}
    blockers = []
    if stacks: blockers.append(str(len(stacks)) + " stacků")
    if workload_containers: blockers.append(str(len(workload_containers)) + " aplikačních kontejnerů")
    if proxy_files: blockers.append(str(len(proxy_files)) + " aktivních proxy konfigurací")
    return {"allowed": not blockers, "role": role, "stacks": len(stacks), "workload_containers": workload_containers, "proxy_files": proxy_files,
            "reason": "PROXY nemá aplikační routy a lze jej bezpečně odebrat." if not blockers else "PROXY nelze odebrat: " + ", ".join(blockers) + "."}

@app.post("/api/endpoints/{endpoint_id}/remove")
async def endpoint_remove(endpoint_id: int, request: Request, session=Depends(require_csrf)):
    if "admin" not in user_permissions(session.get("user","")): raise HTTPException(403, "Permission denied")
    check = await endpoint_remove_check(endpoint_id, session)
    if not check.get("allowed"): raise HTTPException(409, check.get("reason") or "Endpoint nelze bezpečně odebrat.")
    payload = await request.json()
    hub_host = str(payload.get("hub_host") or "").strip()
    hub_user = str(payload.get("hub_ssh_user") or "").strip()
    hub_password = str(payload.get("hub_ssh_password") or "")
    hub_port = int(payload.get("hub_ssh_port") or 22)
    setting = get_endpoint_settings().get(endpoint_id, {})
    mgmt_ip = str(setting.get("host_ip") or "").strip()
    if not all((hub_host, hub_user, hub_password, mgmt_ip)):
        raise HTTPException(400, "Pro bezpečné odebrání je potřeba SSH přístup na MAIN WireGuard HUB.")
    # Remove managed containers that do not carry Portainer's connection first.
    # portainer_agent itself must NOT be deleted through the Portainer Docker
    # proxy: doing so cuts the request transport and Portainer returns
    # "Proxy failure / Unexpected EOF". It is removed directly over SSH below.
    role = (check.get("role") or "").upper()
    removed_containers = []
    proxy_removals = ["dockerstackmover-capacity-agent"] if role == "NODE" else (["traefik"] if role == "PROXY" else [])
    for container_name in proxy_removals:
        r = await docker_request(endpoint_id, "DELETE", "/containers/" + container_name,
                                 params={"force": "1", "v": "0"})
        if r.status_code in (204, 404):
            if r.status_code == 204:
                removed_containers.append(container_name)
            continue
        raise HTTPException(502, "Nelze odstranit systémový kontejner " + container_name + ": " + r.text)

    # The target SSH connection is intentionally required for the final
    # self-removal of portainer_agent. Use the management address while the WG
    # peer still exists; LAN IP is a fallback for repaired/legacy endpoints.
    target_host = mgmt_ip or str(setting.get("lan_ip") or "").strip()
    target_user = str(payload.get("target_ssh_user") or hub_user).strip()
    target_password = str(payload.get("target_ssh_password") or hub_password)
    target_port = int(payload.get("target_ssh_port") or 22)
    if not all((target_host, target_user, target_password)):
        raise HTTPException(400, "Pro odebrání Portainer Agentu je potřeba SSH přístup na odebíraný server.")
    try:
        from .provisioning import _ssh, _run
        target = _ssh(target_host, target_port, target_user, target_password)
        try:
            _run(target, "docker rm -f portainer_agent >/dev/null 2>&1 || true", target_password)
            removed_containers.append("portainer_agent")
        finally:
            target.close()
    except Exception as exc:
        raise HTTPException(502, "Portainer Agent se nepodařilo odebrat přes SSH: " + str(exc))

    try:
        from .provisioning import _ssh, _run
        hub = _ssh(hub_host, hub_port, hub_user, hub_password)
        try:
            conf = "/etc/wireguard/wg-dsm.conf"
            # Find the peer by its unique AllowedIPs management address, remove
            # it from the live interface and rewrite only that [Peer] block.
            script = r"""set -e
IP=__IP__
CONF=/etc/wireguard/wg-dsm.conf
PUB=$(wg show wg-dsm allowed-ips | awk -v ip="$IP/32" '$2==ip{print $1; exit}')
[ -n "$PUB" ] || exit 44
wg set wg-dsm peer "$PUB" remove
python3 - "$CONF" "$IP/32" <<'PY'
import sys
path, target = sys.argv[1], sys.argv[2]
text=open(path).read()
blocks=text.split("\n[Peer]\n")
out=[blocks[0]]
for block in blocks[1:]:
    if ("AllowedIPs = "+target) in block:
        continue
    out.append("[Peer]\n"+block)
open(path,"w").write("\n".join(out))
PY
"""
            _run(hub, script.replace("__IP__", shlex.quote(mgmt_ip)), hub_password)
        finally:
            hub.close()
    except Exception as exc:
        raise HTTPException(502, "WireGuard peer se nepodařilo bezpečně odebrat: " + str(exc))
    async with client() as pc:
        r = await pc.delete("/api/endpoints/" + str(endpoint_id))
    if r.status_code not in (200, 204):
        raise HTTPException(502, "WireGuard peer byl odebrán, ale Portainer endpoint ne: " + r.text)
    with db() as conn:
        conn.execute("DELETE FROM endpoint_settings WHERE endpoint_id = ?", (endpoint_id,))
    return {"ok": True, "role": check.get("role"), "endpoint_id": endpoint_id, "removed_containers": removed_containers}

@app.get("/api/stacks/{stack_id}/targets")
async def migration_targets(stack_id: int, session=Depends(require_permission("migrations"))):
    detail = await build_detail(stack_id); endpoints = await get_endpoints(); targets = []
    for endpoint in endpoints:
        if endpoint["Id"] == detail["stack"]["endpoint_id"]: continue
        if endpoint not in migration_endpoints(endpoints): continue
        name = endpoint.get("Name", "") or ("Endpoint " + str(endpoint["Id"]))
        targets.append({"id": endpoint["Id"], "name": name})
    return {"source": {"id": detail["stack"]["endpoint_id"], "name": detail["stack"]["endpoint"]}, "targets": targets}

@app.get("/api/stacks/{stack_id}/preflight/{target_id}")
async def preflight(stack_id: int, target_id: int, session=Depends(require_permission("migrations"))):
    detail = await build_detail(stack_id); endpoints = await get_endpoints(); stacks = await get_stacks(); source_id = detail["stack"]["endpoint_id"]
    target = next((e for e in endpoints if e["Id"] == target_id), None)
    if not target: raise HTTPException(404, "Target endpoint not found")
    if target_id == source_id: raise HTTPException(400, "Source and target are identical")
    checks = []
    def add(name, ok, message, level=None): checks.append({"name": name, "ok": bool(ok), "level": level or ("ok" if ok else "error"), "message": message})
    try:
        info = (await docker_get(target_id, "/info")).json(); add("Cílový Docker", True, "Docker odpovídá · " + str(info.get("NCPU", "?")) + " CPU · " + str(info.get("Containers", "?")) + " kontejnerů")
    except Exception as exc: add("Cílový Docker", False, str(exc))
    same_stack = [s for s in stacks if s.get("EndpointId") == target_id and s.get("Name") == detail["stack"]["name"]]
    add("Kolize stacku", len(same_stack) == 0, "Na cíli není stack se jménem " + detail["stack"]["name"] if not same_stack else "Na cíli už existuje stack " + detail["stack"]["name"])
    existing_volumes = []
    for volume in detail["volumes"]:
        r = await docker_get(target_id, "/volumes/" + volume["name"], allowed=(200, 404))
        if r.status_code == 200: existing_volumes.append(volume["name"])
    add("Kolize volumes", len(existing_volumes) == 0, "Cílové názvy volumes jsou volné" if not existing_volumes else "Na cíli už existují: " + ", ".join(existing_volumes))
    target_containers = (await docker_get(target_id, "/containers/json", params={"all": "1"})).json(); used_ports = set()
    for c in target_containers:
        for p in c.get("Ports", []):
            if p.get("PublicPort"): used_ports.add((int(p["PublicPort"]), p.get("Type", "tcp")))
    requested_ports = set()
    for c in detail["containers"]:
        for p in c["ports"]:
            if p.get("public"): requested_ports.add((int(p["public"]), p.get("type", "tcp")))
    collisions = sorted(requested_ports.intersection(used_ports))
    add("Kolize portů", len(collisions) == 0, "Publikované porty jsou na cíli volné" if not collisions else "Kolize: " + ", ".join(str(p[0]) + "/" + p[1] for p in collisions))
    unknown = [v["name"] for v in detail["volumes"] if v.get("driver") in (None, "unknown")]
    add("Persistentní data", len(unknown) == 0, str(len(detail["volumes"])) + " named volume(s) připraveno k migraci" if not unknown else "Nelze ověřit: " + ", ".join(unknown))
    not_running = [c["name"] for c in detail["containers"] if c.get("state") != "running"]
    add("Stav zdroje", len(not_running) == 0, "Všechny kontejnery zdrojového stacku běží" if not not_running else "Neběží: " + ", ".join(not_running), "ok" if not not_running else "warning")
    if detail["domains"]:
        add("Proxy metadata", True, ", ".join((d.get("host") or "?") + " → " + str(d.get("port") or "?") for d in detail["domains"]))
        settings = get_endpoint_settings(); source_public_ip = settings.get(int(source_id), {}).get("public_ip", ""); target_public_ip = settings.get(int(target_id), {}).get("public_ip", "")
        if vas_hosting_enabled() and source_public_ip and target_public_ip and source_public_ip != target_public_ip:
            dns_errors = []
            for domain in detail["domains"]:
                host = domain.get("host")
                if not host: continue
                try:
                    zone, record = await vas_find_a_record(host)
                    if record.get("content") != source_public_ip:
                        dns_errors.append(host + " ukazuje na " + str(record.get("content")) + ", očekáváno " + source_public_ip)
                except Exception as exc:
                    dns_errors.append(host + ": " + str(exc))
            add("DNS cutover", len(dns_errors) == 0, "Váš Hosting připraven · " + source_public_ip + " → " + target_public_ip if not dns_errors else " · ".join(dns_errors))
        elif source_public_ip == target_public_ip and source_public_ip:
            add("DNS cutover", True, "Zdroj i cíl používají stejnou veřejnou IP " + source_public_ip + " · změna DNS není potřeba", "warning")
        elif not vas_hosting_enabled():
            add("DNS cutover", True, "Váš Hosting není nakonfigurován · DNS se při migraci nezmění", "warning")
        else:
            add("DNS cutover", True, "Chybí Public IP u zdrojového nebo cílového endpointu · DNS se při migraci nezmění", "warning")
    else: add("Proxy metadata", True, "Stack nemá dc1.proxy doménu", "warning")
    blocking = [c for c in checks if c["level"] == "error" and not c["ok"]]
    return {"version": "1.2.0", "read_only": True, "ready": len(blocking) == 0, "stack": detail["stack"], "target": {"id": target["Id"], "name": target["Name"]}, "containers": len(detail["containers"]), "volumes": [v["name"] for v in detail["volumes"]], "checks": checks}

def endpoint_host_ip(endpoint):
    endpoint_id = int((endpoint or {}).get("Id") or 0); setting = get_endpoint_settings().get(endpoint_id, {})
    if setting.get("lan_ip"): return setting["lan_ip"]
    if setting.get("host_ip"): return setting["host_ip"]
    url = (endpoint or {}).get("URL") or ""; m = re.match(r"^tcp://(\[[^\]]+\]|[^:]+)(?::\d+)?$", url)
    if not m: raise RuntimeError("Cannot determine host IP automatically. Set Host IP for this endpoint in Endpoint settings.")
    return m.group(1).strip("[]")

def rewrite_host_bind_ip(stack_file, source_endpoint, target_endpoint):
    source_ip = endpoint_host_ip(source_endpoint); target_ip = endpoint_host_ip(target_endpoint)
    pattern = re.compile(r'(?P<prefix>["\\\'\s-])' + re.escape(source_ip) + r'(?P<suffix>:\d+(?::\d+)?(?:/(?:tcp|udp|sctp))?)')
    rewritten, count = pattern.subn(lambda m: m.group("prefix") + target_ip + m.group("suffix"), stack_file)
    return rewritten, source_ip, target_ip, count
