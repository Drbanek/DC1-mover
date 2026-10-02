from ..core import *
from .general import stack_detail

def fmt_bytes(value):
    value = float(value or 0); units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if value < 1024 or unit == units[-1]: return f"{value:.1f} {unit}"
        value /= 1024

async def host_disk_usage(endpoint_id, docker_root):
    helper = None
    try:
        await ensure_image(endpoint_id, "busybox:1.37")
        create = await docker_request(endpoint_id, "POST", "/containers/create", json={
            "Image": "busybox:1.37",
            "Cmd": ["sh", "-c", "df -P -k /dockerroot | tail -1"],
            "HostConfig": {"Binds": [docker_root + ":/dockerroot:ro"], "NetworkMode": "none"}
        })
        if create.status_code != 201:
            raise RuntimeError("disk helper create failed: " + create.text)
        helper = create.json()["Id"]
        start = await docker_request(endpoint_id, "POST", f"/containers/{helper}/start")
        if start.status_code != 204:
            raise RuntimeError("disk helper start failed: " + start.text)
        await wait_container(endpoint_id, helper, timeout=30)
        logs = await docker_request(endpoint_id, "GET", f"/containers/{helper}/logs?stdout=1&stderr=1")
        line = logs.text.strip().splitlines()[-1].replace("\x01", "").replace("\x00", "").strip()
        parts = line.split()
        total_kb, used_kb, free_kb = int(parts[-5]), int(parts[-4]), int(parts[-3])
        total, used, free = total_kb * 1024, used_kb * 1024, free_kb * 1024
        pct = round((used / total * 100.0) if total else 0, 1)
        return {"total": total, "used": used, "free": free, "percent": pct}
    finally:
        if helper:
            await remove_container(endpoint_id, helper)

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
    disk = None
    try:
        disk = await host_disk_usage(endpoint_id, docker_root)
    except Exception:
        disk = None
    return {"id": endpoint_id, "name": endpoint["Name"], "cpu_count": cpus, "ram_total": total_ram, "ram_used_containers": used_ram, "ram_available_estimate": available_ram, "running_containers": running, "cpu_percent_containers": round(cpu_percent_total, 1), "docker_images_size": images_size, "docker_volumes_size": volumes_size, "docker_used_estimate": docker_used, "ram_total_human": fmt_bytes(total_ram), "ram_used_human": fmt_bytes(used_ram), "ram_available_human": fmt_bytes(available_ram), "docker_used_human": fmt_bytes(docker_used), "docker_root": docker_root, "disk_total": disk["total"] if disk else None, "disk_used": disk["used"] if disk else None, "disk_free": disk["free"] if disk else None, "disk_percent": disk["percent"] if disk else None, "disk_total_human": fmt_bytes(disk["total"]) if disk else None, "disk_used_human": fmt_bytes(disk["used"]) if disk else None, "disk_free_human": fmt_bytes(disk["free"]) if disk else None}

@app.get("/api/cluster")
async def cluster_dashboard(session=Depends(current_session)):
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
async def nodes_capacity(session=Depends(current_session)):
    endpoints = await get_endpoints(); nodes = migration_endpoints(endpoints); result = []
    for endpoint in nodes:
        try: result.append(await node_capacity(endpoint))
        except Exception as exc: result.append({"id": endpoint.get("Id"), "name": endpoint.get("Name"), "error": str(exc)})
    healthy = [n for n in result if not n.get("error")]; recommended = None
    if healthy: recommended = sorted(healthy, key=lambda n: (-n["ram_available_estimate"], n["cpu_percent_containers"], n["running_containers"], n["id"]))[0]["id"]
    return {"nodes": result, "recommended_endpoint_id": recommended, "method": "Recommendation uses estimated free RAM from total host RAM minus current container memory usage."}

@app.get("/api/stacks/{stack_id}/advisor")
async def migration_advisor(stack_id: int, session=Depends(current_session)):
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
            if source_volume_bytes: warnings.append("Volume data k přenosu: " + fmt_bytes(source_volume_bytes))
            cap["warnings"] = warnings; cap["source_volume_bytes"] = source_volume_bytes; candidates.append(cap)
        except Exception as exc: candidates.append({"id": endpoint.get("Id"), "name": endpoint.get("Name"), "error": str(exc), "warnings": ["Kapacitu cíle se nepodařilo ověřit"]})
    healthy = [c for c in candidates if not c.get("error")]; recommended = None
    if healthy: recommended = sorted(healthy, key=lambda n: (len([w for w in n["warnings"] if "Nízká RAM" in w or "Vysoké" in w]), -n["ram_available_estimate"], n["cpu_percent_containers"], n["running_containers"], n["id"]))[0]["id"]
    return {"stack_id": stack_id, "source_endpoint_id": source_id, "source_volume_bytes": source_volume_bytes, "source_volume_human": fmt_bytes(source_volume_bytes), "recommended_endpoint_id": recommended, "candidates": candidates}

@app.get("/api/migrations")
async def migration_history(session=Depends(current_session)): return load_recent_jobs(100)

@app.get("/api/migrations/{job_id}")
async def migration_status(job_id: str, session=Depends(current_session)):
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
    job = load_job(job_id)
    if not job or job.get("status") != "success" or not job.get("result"): raise HTTPException(409, "Migration is not ready for confirmation")
    result = job["result"]
    if result.get("finalized"): return result
    await delete_stack(result["source_stack_id"], result["source_endpoint_id"])
    for volume_name in result.get("source_volumes", []): await delete_volume(result["source_endpoint_id"], volume_name)
    result["source_state"] = "deleted"; result["finalized"] = "confirmed"; persist_job(job); release_stack_lock(job["stack_id"], job["id"]); return result

@app.post("/api/migrations/{job_id}/rollback")
async def rollback_migration(job_id: str, session=Depends(require_csrf)):
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
