from ..core import *

@app.get("/health")
async def health():
    return {"status": "ok", "version": "1.0.0"}

@app.get("/api/inventory")
async def inventory():
    endpoints = await get_endpoints(); stacks = await get_stacks(); endpoint_names = {e["Id"]: e["Name"] for e in endpoints}
    return [{"id": s["Id"], "name": s["Name"], "endpoint_id": s["EndpointId"], "endpoint": endpoint_names.get(s["EndpointId"], "Endpoint " + str(s["EndpointId"])), "status": s["Status"]} for s in stacks]

@app.get("/api/stacks/{stack_id}/detail")
async def stack_detail(stack_id: int): return await build_detail(stack_id)

@app.get("/api/endpoints/settings")
async def endpoint_settings_list(session=Depends(current_session)):
    endpoints = await get_endpoints(); settings = get_endpoint_settings(); result = []
    for endpoint in endpoints:
        eid = int(endpoint["Id"]); setting = settings.get(eid, {})
        result.append({"id": eid, "name": endpoint.get("Name") or ("Endpoint " + str(eid)), "url": endpoint.get("URL") or "", "migration_enabled": bool(setting.get("migration_enabled", False)), "host_ip": setting.get("host_ip", ""), "site": setting.get("site", "")})
    return result

@app.put("/api/endpoints/{endpoint_id}/settings")
async def endpoint_settings_save(endpoint_id: int, request: Request, session=Depends(require_csrf)):
    endpoints = await get_endpoints()
    if not any(int(e["Id"]) == endpoint_id for e in endpoints): raise HTTPException(404, "Endpoint not found")
    payload = await request.json(); save_endpoint_setting(endpoint_id, bool(payload.get("migration_enabled")), str(payload.get("host_ip") or ""), str(payload.get("site") or ""))
    return {"ok": True}

@app.get("/api/stacks/{stack_id}/targets")
async def migration_targets(stack_id: int):
    detail = await build_detail(stack_id); endpoints = await get_endpoints(); targets = []
    for endpoint in endpoints:
        if endpoint["Id"] == detail["stack"]["endpoint_id"]: continue
        if endpoint not in migration_endpoints(endpoints): continue
        name = endpoint.get("Name", "") or ("Endpoint " + str(endpoint["Id"]))
        targets.append({"id": endpoint["Id"], "name": name})
    return {"source": {"id": detail["stack"]["endpoint_id"], "name": detail["stack"]["endpoint"]}, "targets": targets}

@app.get("/api/stacks/{stack_id}/preflight/{target_id}")
async def preflight(stack_id: int, target_id: int):
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
    if detail["domains"]: add("Proxy metadata", True, ", ".join((d.get("host") or "?") + " → " + str(d.get("port") or "?") for d in detail["domains"]))
    else: add("Proxy metadata", True, "Stack nemá dc1.proxy doménu", "warning")
    blocking = [c for c in checks if c["level"] == "error" and not c["ok"]]
    return {"version": "1.0.0", "read_only": True, "ready": len(blocking) == 0, "stack": detail["stack"], "target": {"id": target["Id"], "name": target["Name"]}, "containers": len(detail["containers"]), "volumes": [v["name"] for v in detail["volumes"]], "checks": checks}

def endpoint_host_ip(endpoint):
    endpoint_id = int((endpoint or {}).get("Id") or 0); setting = get_endpoint_settings().get(endpoint_id, {})
    if setting.get("host_ip"): return setting["host_ip"]
    url = (endpoint or {}).get("URL") or ""; m = re.match(r"^tcp://(\[[^\]]+\]|[^:]+)(?::\d+)?$", url)
    if not m: raise RuntimeError("Cannot determine host IP automatically. Set Host IP for this endpoint in Endpoint settings.")
    return m.group(1).strip("[]")

def rewrite_host_bind_ip(stack_file, source_endpoint, target_endpoint):
    source_ip = endpoint_host_ip(source_endpoint); target_ip = endpoint_host_ip(target_endpoint)
    pattern = re.compile(r'(?P<prefix>["\\\'\s-])' + re.escape(source_ip) + r'(?P<suffix>:\d+(?::\d+)?(?:/(?:tcp|udp|sctp))?)')
    rewritten, count = pattern.subn(lambda m: m.group("prefix") + target_ip + m.group("suffix"), stack_file)
    return rewritten, source_ip, target_ip, count
