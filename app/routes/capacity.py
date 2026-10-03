from ..core import *
from .general import stack_detail



def endpoint_host_ip(endpoint):
    """Resolve a usable node IP. Explicit Host IP is an override; otherwise derive it from Portainer."""
    eid = int(endpoint.get("Id"))
    configured = (get_endpoint_settings().get(eid, {}).get("host_ip") or "").strip()
    if configured:
        return configured, "configured"
    candidates = [
        endpoint.get("URL"), endpoint.get("Url"),
        endpoint.get("PublicURL"), endpoint.get("PublicUrl"),
        endpoint.get("EdgeCheckinInterval")
    ]
    for value in candidates:
        if not isinstance(value, str) or not value.strip():
            continue
        value = value.strip()
        try:
            from urllib.parse import urlparse
            parsed = urlparse(value if "://" in value else "//" + value)
            host = parsed.hostname
            if host:
                import ipaddress
                ipaddress.ip_address(host)
                return host, "portainer"
        except Exception:
            pass
    return "", ""

CAPACITY_AGENT_IMAGE = os.getenv("CAPACITY_AGENT_IMAGE", "ghcr.io/drbanek/dockerstackmover-capacity-agent:latest")
CAPACITY_AGENT_CONTAINER = "dockerstackmover-capacity-agent"

async def _agent_container(endpoint_id):
    r = await docker_request(endpoint_id, "GET", "/containers/" + CAPACITY_AGENT_CONTAINER + "/json")
    return r.json() if r.status_code == 200 else None

@app.post("/api/endpoints/{endpoint_id}/capacity-agent/install")
async def install_capacity_agent(endpoint_id: int, session=Depends(require_csrf)):
    if "admin" not in user_permissions(session.get("user", "")):
        raise HTTPException(403, "Permission denied")
    endpoints = await get_endpoints()
    endpoint = next((e for e in endpoints if int(e.get("Id")) == endpoint_id), None)
    if not endpoint:
        raise HTTPException(404, "Endpoint not found")
    settings = get_endpoint_settings().get(endpoint_id, {})
    host_ip, host_ip_source = endpoint_host_ip(endpoint)
    if not host_ip:
        raise HTTPException(400, "Host IP se nepodařilo zjistit z Portainer endpointu. Nastav ji ručně v Endpoint settings.")
    token = secrets.token_hex(32)
    # Pull the centrally published agent image through Portainer.
    await ensure_image(endpoint_id, CAPACITY_AGENT_IMAGE)
    existing = await _agent_container(endpoint_id)
    if existing:
        await remove_container(endpoint_id, CAPACITY_AGENT_CONTAINER)
    create = await docker_request(endpoint_id, "POST", "/containers/create",
        params={"name": CAPACITY_AGENT_CONTAINER},
        json={
            "Image": CAPACITY_AGENT_IMAGE,
            "Env": ["AGENT_TOKEN=" + token],
            "ExposedPorts": {"9100/tcp": {}},
            "HostConfig": {
                "Binds": ["/srv:/host/srv:ro", "/var/lib/docker:/host/docker:ro"],
                "PortBindings": {"9100/tcp": [{"HostIp": host_ip, "HostPort": "9100"}]},
                "ReadonlyRootfs": True,
                "Tmpfs": {"/tmp": "rw,noexec,nosuid,size=16m"},
                "SecurityOpt": ["no-new-privileges:true"],
                "CapDrop": ["ALL"],
                "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0}
            }
        })
    if create.status_code != 201:
        raise HTTPException(create.status_code, "Capacity Agent create failed: " + create.text)
    container_id = create.json()["Id"]
    start = await docker_request(endpoint_id, "POST", "/containers/" + container_id + "/start", json={})
    if start.status_code not in (204, 304):
        await remove_container(endpoint_id, container_id)
        raise HTTPException(start.status_code, "Capacity Agent start failed: " + start.text)
    agent_url = "http://" + host_ip + ":9100"
    last_error = ""
    for _ in range(15):
        try:
            async with httpx.AsyncClient(timeout=3) as hc:
                health = await hc.get(agent_url + "/health")
                capacity = await hc.get(agent_url + "/capacity", headers={"X-Agent-Token": token})
            if health.status_code == 200 and capacity.status_code == 200:
                payload = capacity.json()
                if int((payload.get("data") or {}).get("total") or 0) > 0 and int((payload.get("system") or {}).get("total") or 0) > 0:
                    save_endpoint_setting(endpoint_id, bool(settings.get("migration_enabled")), host_ip,
                        settings.get("site", ""), settings.get("public_ip", ""), agent_url, token)
                    return {"ok": True, "agent_url": agent_url, "image": CAPACITY_AGENT_IMAGE, "host_ip": host_ip, "host_ip_source": host_ip_source}
            last_error = "health=" + str(health.status_code) + ", capacity=" + str(capacity.status_code)
        except Exception as exc:
            last_error = str(exc)
        await asyncio.sleep(1)
    await remove_container(endpoint_id, container_id)
    raise HTTPException(502, "Capacity Agent se po instalaci nepodařilo ověřit: " + last_error)



async def node_readiness(endpoint):
    """Return an actionable readiness report for any Portainer endpoint."""
    endpoint_id = int(endpoint["Id"])
    settings = get_endpoint_settings().get(endpoint_id, {})
    host_ip, host_ip_source = endpoint_host_ip(endpoint)
    checks = {
        "docker": {"ok": False, "message": "Docker API unavailable"},
        "host_ip": {"ok": bool(host_ip), "message": host_ip or "Host IP could not be detected"},
        "capacity_agent": {"ok": False, "message": "Not configured"},
        "data_disk": {"ok": False, "message": "/srv capacity unavailable"},
        "migration": {"ok": bool(settings.get("migration_enabled")), "message": "Enabled" if settings.get("migration_enabled") else "Disabled"},
    }
    try:
        info = await docker_request(endpoint_id, "GET", "/info")
        checks["docker"] = {"ok": info.status_code == 200, "message": "Online" if info.status_code == 200 else "HTTP " + str(info.status_code)}
    except Exception as exc:
        checks["docker"]["message"] = str(exc)
    if settings.get("agent_url") and settings.get("agent_token"):
        try:
            data_disk, system_disk = await agent_disk_usage(endpoint_id)
            checks["capacity_agent"] = {"ok": True, "message": "Online"}
            checks["data_disk"] = {"ok": True, "message": fmt_bytes(data_disk["free"]) + " free", "free": data_disk["free"], "total": data_disk["total"]}
        except Exception as exc:
            checks["capacity_agent"] = {"ok": False, "message": str(exc)}
    ready = all(checks[k]["ok"] for k in ("docker", "host_ip", "capacity_agent", "data_disk", "migration"))
    return {"id": endpoint_id, "name": endpoint.get("Name") or ("Endpoint " + str(endpoint_id)), "ready": ready,
            "status": "ready" if ready else "setup_required", "host_ip": host_ip, "host_ip_source": host_ip_source,
            "site": settings.get("site", ""), "public_ip": settings.get("public_ip", ""), "checks": checks}

@app.get("/api/nodes/readiness")
async def nodes_readiness(session=Depends(require_permission("dashboard_read"))):
    endpoints = await get_endpoints()
    result = []
    for endpoint in endpoints:
        try:
            result.append(await node_readiness(endpoint))
        except Exception as exc:
            result.append({"id": endpoint.get("Id"), "name": endpoint.get("Name"), "ready": False, "status": "error", "error": str(exc)})
    return {"nodes": result}

@app.post("/api/endpoints/{endpoint_id}/prepare")
async def prepare_node(endpoint_id: int, session=Depends(require_csrf)):
    """Enable migrations and install/repair the Capacity Agent in one action."""
    if "admin" not in user_permissions(session.get("user", "")):
        raise HTTPException(403, "Permission denied")
    endpoints = await get_endpoints()
    endpoint = next((e for e in endpoints if int(e.get("Id")) == endpoint_id), None)
    if not endpoint:
        raise HTTPException(404, "Endpoint not found")
    settings = get_endpoint_settings().get(endpoint_id, {})
    host_ip, _ = endpoint_host_ip(endpoint)
    if not host_ip:
        raise HTTPException(400, "Host IP se nepodařilo automaticky zjistit. Nastav ji ručně jako override.")
    save_endpoint_setting(endpoint_id, True, settings.get("host_ip", ""), settings.get("site", ""),
                          settings.get("public_ip", ""), settings.get("agent_url", ""), None)
    # Reuse the hardened installer; it persists generated credentials only after verification.
    await install_capacity_agent(endpoint_id, session)
    return await node_readiness(endpoint)

def fmt_bytes(value):
    value = float(value or 0); units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if value < 1024 or unit == units[-1]: return f"{value:.1f} {unit}"
        value /= 1024

async def agent_disk_usage(endpoint_id):
    settings = get_endpoint_settings().get(int(endpoint_id), {})
    agent_url = (settings.get("agent_url") or "").strip().rstrip("/")
    token = settings.get("agent_token") or ""
    if not agent_url:
        raise RuntimeError("Capacity Agent není pro endpoint nastaven")
    if not token:
        raise RuntimeError("Capacity Agent token není pro endpoint nastaven")
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(agent_url + "/capacity", headers={"X-Agent-Token": token})
    except Exception as exc:
        raise RuntimeError("Capacity Agent nedostupný: " + str(exc))
    if r.status_code != 200:
        raise RuntimeError("Capacity Agent HTTP " + str(r.status_code) + ": " + r.text[:200])
    payload = r.json()
    def normalize(name, display_path):
        d = payload.get(name) or {}
        total, used, free = int(d.get("total") or 0), int(d.get("used") or 0), int(d.get("free") or 0)
        if total <= 0:
            raise RuntimeError("Capacity Agent nevrátil platnou kapacitu " + display_path)
        return {"path": display_path, "total": total, "used": used, "free": free, "percent": round(used / total * 100, 1)}
    return normalize("data", "/srv"), normalize("system", "/var/lib/docker")

async def node_capacity(endpoint):
    endpoint_id = endpoint["Id"]; info_r = await docker_request(endpoint_id, "GET", "/info")
    if info_r.status_code != 200: raise RuntimeError("Docker info failed for " + endpoint["Name"] + ": " + info_r.text)
    info = info_r.json(); df_r = await docker_request(endpoint_id, "GET", "/system/df"); df = df_r.json() if df_r.status_code == 200 else {}; total_ram = int(info.get("MemTotal") or 0); cpus = int(info.get("NCPU") or 0)
    containers_r = await docker_request(endpoint_id, "GET", "/containers/json?all=0"); containers = containers_r.json() if containers_r.status_code == 200 else []
    used_ram = 0; cpu_percent_total = 0.0; running = 0
    for container in containers:
        cid = container.get("Id")
        if not cid: continue
        stats_r = await docker_request(endpoint_id, "GET", f"/containers/{cid}/stats?stream=false")
        if stats_r.status_code != 200: continue
        stats = stats_r.json(); mem = stats.get("memory_stats", {}); usage = int(mem.get("usage") or 0); cache = int((mem.get("stats") or {}).get("cache") or 0); used_ram += max(0, usage - cache)
        cpu = stats.get("cpu_stats", {}); precpu = stats.get("precpu_stats", {}); cpu_delta = int((cpu.get("cpu_usage") or {}).get("total_usage") or 0) - int((precpu.get("cpu_usage") or {}).get("total_usage") or 0); system_delta = int(cpu.get("system_cpu_usage") or 0) - int(precpu.get("system_cpu_usage") or 0); online = int(cpu.get("online_cpus") or cpus or 1)
        if cpu_delta > 0 and system_delta > 0: cpu_percent_total += cpu_delta / system_delta * online * 100.0
        running += 1
    available_ram = max(0, total_ram - used_ram); images_size = sum(int(i.get("Size") or 0) for i in (df.get("Images") or [])); volumes_size = 0
    for volume in df.get("Volumes") or []:
        usage = volume.get("UsageData") or {}; size = usage.get("Size")
        if isinstance(size, int) and size > 0: volumes_size += size
    docker_used = images_size + volumes_size
    docker_root = str(info.get("DockerRootDir") or "/var/lib/docker")
    system_disk = data_disk = None
    system_disk_error = data_disk_error = None
    try:
        data_disk, system_disk = await agent_disk_usage(endpoint_id)
    except Exception as exc:
        system_disk_error = data_disk_error = str(exc)
    return {"id": endpoint_id, "name": endpoint["Name"], "cpu_count": cpus, "ram_total": total_ram, "ram_used_containers": used_ram, "ram_available_estimate": available_ram, "running_containers": running, "cpu_percent_containers": round(cpu_percent_total, 1), "docker_images_size": images_size, "docker_volumes_size": volumes_size, "docker_used_estimate": docker_used, "ram_total_human": fmt_bytes(total_ram), "ram_used_human": fmt_bytes(used_ram), "ram_available_human": fmt_bytes(available_ram), "docker_used_human": fmt_bytes(docker_used), "docker_root": docker_root,
        "system_disk": ({**system_disk, "total_human":fmt_bytes(system_disk["total"]), "used_human":fmt_bytes(system_disk["used"]), "free_human":fmt_bytes(system_disk["free"])} if system_disk else None), "system_disk_error":system_disk_error,
        "data_path":"/srv", "data_disk": ({**data_disk, "total_human":fmt_bytes(data_disk["total"]), "used_human":fmt_bytes(data_disk["used"]), "free_human":fmt_bytes(data_disk["free"])} if data_disk else None), "data_disk_error":data_disk_error,
        "disk_total": data_disk["total"] if data_disk else None, "disk_used": data_disk["used"] if data_disk else None, "disk_free": data_disk["free"] if data_disk else None, "disk_percent": data_disk["percent"] if data_disk else None, "disk_total_human":fmt_bytes(data_disk["total"]) if data_disk else None, "disk_used_human":fmt_bytes(data_disk["used"]) if data_disk else None, "disk_free_human":fmt_bytes(data_disk["free"]) if data_disk else None, "disk_error":data_disk_error}

@app.get("/api/cluster")
async def cluster_dashboard(session=Depends(require_permission("dashboard_read"))):
    endpoints = await get_endpoints(); nodes = migration_endpoints(endpoints)
    async with client() as c:
        stacks_r = await c.get("/api/stacks")
        if stacks_r.status_code != 200: raise HTTPException(stacks_r.status_code, "Portainer stacks: " + stacks_r.text)
        stacks = stacks_r.json()
    result = []
    for endpoint in nodes:
        endpoint_id = int(endpoint["Id"])
        try: cap = await node_capacity(endpoint)
        except Exception as exc: cap = {"id": endpoint_id, "name": endpoint.get("Name"), "error": str(exc)}
        node_stacks = [{"id": stack.get("Id"), "name": stack.get("Name"), "status": stack.get("Status"), "endpoint_id": endpoint_id} for stack in stacks if int(stack.get("EndpointId") or 0) == endpoint_id]
        cap["stacks"] = sorted(node_stacks, key=lambda s: str(s.get("name", "")).lower()); result.append(cap)
    healthy = [n for n in result if not n.get("error")]; recommended = None
    if healthy: recommended = sorted(healthy, key=lambda n: (-n["ram_available_estimate"], n["cpu_percent_containers"], len(n["stacks"]), n["id"]))[0]["id"]
    return {"nodes": result, "recommended_endpoint_id": recommended}

@app.get("/api/nodes/capacity")
async def nodes_capacity(session=Depends(require_permission("dashboard_read"))):
    endpoints = await get_endpoints(); nodes = migration_endpoints(endpoints); result = []
    for endpoint in nodes:
        try: result.append(await node_capacity(endpoint))
        except Exception as exc: result.append({"id": endpoint.get("Id"), "name": endpoint.get("Name"), "error": str(exc)})
    healthy = [n for n in result if not n.get("error")]; recommended = None
    if healthy: recommended = sorted(healthy, key=lambda n: (-n["ram_available_estimate"], n["cpu_percent_containers"], n["running_containers"], n["id"]))[0]["id"]
    return {"nodes": result, "recommended_endpoint_id": recommended, "method": "Recommendation uses estimated free RAM from total host RAM minus current container memory usage."}

@app.get("/api/stacks/{stack_id}/advisor")
async def migration_advisor(stack_id: int, session=Depends(require_permission("migrations"))):
    detail = await stack_detail(stack_id); source_id = int(detail["stack"]["endpoint_id"]); endpoints = await get_endpoints(); nodes = [e for e in migration_endpoints(endpoints) if int(e.get("Id")) != source_id]
    source_volume_bytes = 0
    for volume in detail.get("volumes", []):
        size = volume.get("size")
        if isinstance(size, int) and size > 0: source_volume_bytes += size
    candidates = []
    for endpoint in nodes:
        try:
            cap = await node_capacity(endpoint); warnings = []; ram_ratio = cap["ram_available_estimate"] / cap["ram_total"] if cap["ram_total"] else 0
            if ram_ratio < 0.20: warnings.append("Nízká RAM rezerva (<20 %)")
            if cap["cpu_percent_containers"] > 80: warnings.append("Vysoké aktuální CPU zatížení kontejnerů")
            if source_volume_bytes:
                warnings.append("Volume data k přenosu: " + fmt_bytes(source_volume_bytes))
                if cap.get("data_disk") and source_volume_bytes > cap["data_disk"]["free"]: warnings.append("Nedostatek místa na DATA /srv")
            cap["warnings"] = warnings; cap["source_volume_bytes"] = source_volume_bytes; candidates.append(cap)
        except Exception as exc: candidates.append({"id": endpoint.get("Id"), "name": endpoint.get("Name"), "error": str(exc), "warnings": ["Kapacitu cíle se nepodařilo ověřit"]})
    healthy = [c for c in candidates if not c.get("error")]; recommended = None
    if healthy:
        recommended = sorted(healthy, key=lambda n: (1 if "Nedostatek místa na DATA /srv" in n["warnings"] else 0, len([w for w in n["warnings"] if "Nízká RAM" in w or "Vysoké" in w]), -(n.get("data_disk") or {}).get("free",0), -n["ram_available_estimate"], n["cpu_percent_containers"], n["id"]))[0]["id"]
    return {"stack_id": stack_id, "source_endpoint_id": source_id, "source_volume_bytes": source_volume_bytes, "source_volume_human": fmt_bytes(source_volume_bytes), "recommended_endpoint_id": recommended, "candidates": candidates}

@app.get("/api/migrations")
async def migration_history(session=Depends(require_permission("migrations"))): return load_recent_jobs(100)

@app.get("/api/migrations/{job_id}")
async def migration_status(job_id: str, session=Depends(require_permission("migrations"))):
    job = load_job(job_id)
    if not job: raise HTTPException(404, "Migration job not found")
    return job

async def delete_stack(stack_id, endpoint_id):
    async with client() as c:
        r = await c.delete("/api/stacks/" + str(stack_id), params={"endpointId": endpoint_id})
        if r.status_code not in (200, 204): raise RuntimeError("Stack delete failed: " + r.text)

async def delete_volume(endpoint_id, volume_name):
    r = await docker_request(endpoint_id, "DELETE", "/volumes/" + volume_name)
    if r.status_code not in (204, 404): raise RuntimeError("Volume delete failed " + volume_name + ": " + r.text)

@app.post("/api/migrations/{job_id}/confirm")
async def confirm_migration(job_id: str, session=Depends(require_csrf)):
    if "migrations" not in user_permissions(session.get("user","")): raise HTTPException(403,"Permission denied")
    job = load_job(job_id)
    if not job or job.get("status") != "success" or not job.get("result"): raise HTTPException(409, "Migration is not ready for confirmation")
    result = job["result"]
    if result.get("finalized"): return result
    await delete_stack(result["source_stack_id"], result["source_endpoint_id"])
    for volume_name in result.get("source_volumes", []): await delete_volume(result["source_endpoint_id"], volume_name)
    for change in result.get("dns_changes", []):
        if change.get("provider") == "vas-hosting":
            await vas_update_a_record(change["zone"], change["record_id"], change["host"], change["new_content"], change.get("ttl") or 60)
    result["source_state"] = "deleted"; result["finalized"] = "confirmed"; persist_job(job); release_stack_lock(job["stack_id"], job["id"]); return result

@app.post("/api/migrations/{job_id}/rollback")
async def rollback_migration(job_id: str, session=Depends(require_csrf)):
    if "migrations" not in user_permissions(session.get("user","")): raise HTTPException(403,"Permission denied")
    job = load_job(job_id)
    if not job or job.get("status") != "success" or not job.get("result"): raise HTTPException(409, "Migration is not ready for rollback")
    result = job["result"]
    if result.get("finalized"): return result
    await start_stack(result["source_stack_id"], result["source_endpoint_id"])
    for change in reversed(result.get("dns_changes", [])):
        if change.get("provider") == "vas-hosting":
            await vas_update_a_record(change["zone"], change["record_id"], change["host"], change["old_content"], change.get("ttl") or 60)
    await delete_stack(result["target_stack_id"], result["target_endpoint_id"])
    for volume_name in result.get("volumes", []): await delete_volume(result["target_endpoint_id"], volume_name)
    result["source_state"] = "running"; result["finalized"] = "rolled-back"; persist_job(job); release_stack_lock(job["stack_id"], job["id"]); return result
