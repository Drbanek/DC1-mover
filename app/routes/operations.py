from ..core import *
from .general import build_detail
from .capacity import node_capacity, node_readiness, CAPACITY_AGENT_IMAGE, _agent_container

async def _stack_preflight(stack_id: int, target_id: int):
    detail = await build_detail(stack_id)
    endpoints = await get_endpoints()
    source_id = int(detail["stack"]["endpoint_id"])
    target = next((e for e in endpoints if int(e.get("Id")) == int(target_id)), None)
    if not target: raise HTTPException(404, "Target endpoint not found")
    checks=[]; blocking=False
    def add(name, ok, message, severity="error"):
        nonlocal blocking
        checks.append({"name":name,"ok":bool(ok),"message":message,"severity":severity})
        if not ok and severity=="error": blocking=True
    add("source_target", source_id != int(target_id), "Source and target differ")
    ready = await node_readiness(target)
    add("target_ready", ready.get("ready"), "Target READY" if ready.get("ready") else "Target requires setup")
    stacks=await get_stacks()
    collision=any(int(s.get("EndpointId") or 0)==int(target_id) and s.get("Name")==detail["stack"]["name"] for s in stacks)
    add("stack_name", not collision, "Stack name available" if not collision else "Stack already exists on target")
    target_containers=(await docker_get(target_id,"/containers/json",params={"all":"1"})).json()
    used={(int(p["PublicPort"]),p.get("Type","tcp")) for c in target_containers for p in c.get("Ports",[]) if p.get("PublicPort")}
    wanted={(int(p["public"]),p.get("type","tcp")) for c in detail["containers"] for p in c["ports"] if p.get("public")}
    ports=sorted(wanted.intersection(used)); add("ports",not ports,"No published-port collisions" if not ports else "Port collision: "+str(ports))
    existing=[]
    for v in detail["volumes"]:
        r=await docker_request(target_id,"GET","/volumes/"+v["name"])
        if r.status_code==200: existing.append(v["name"])
    add("volumes",not existing,"Volume names available" if not existing else "Existing volumes: "+", ".join(existing))
    binds=sorted({m.get("source") for c in detail["containers"] for m in c.get("mounts",[]) if m.get("type")=="bind" and m.get("source")})
    add("bind_mounts",not binds,"No host bind mounts" if not binds else "Host bind mounts require manual validation: "+", ".join(binds),"warning")
    cap=await node_capacity(target)
    add("data_capacity",bool(cap.get("data_disk")),"DATA /srv capacity available" if cap.get("data_disk") else "DATA /srv capacity unavailable")
    images=sorted({c.get("image") for c in detail["containers"] if c.get("image")})
    return {"stack_id":stack_id,"source_endpoint_id":source_id,"target_endpoint_id":target_id,"ready":not blocking,"checks":checks,"images":images,"bind_mounts":binds}

@app.get("/api/stacks/{stack_id}/preflight-v2/{target_id}")
async def preflight_v2(stack_id:int,target_id:int,session=Depends(require_permission("migrations"))):
    return await _stack_preflight(stack_id,target_id)

@app.get("/api/maintenance")
async def maintenance_overview(session=Depends(require_permission("admin"))):
    endpoints=await get_endpoints(); result=[]
    for e in endpoints:
        eid=int(e["Id"])
        try:
            agent=await _agent_container(eid)
            containers=(await docker_get(eid,"/containers/json",params={"all":"1"})).json()
            result.append({"id":eid,"name":e.get("Name"),"docker":True,
                "capacity_agent":{"installed":bool(agent),"image":(agent or {}).get("Config",{}).get("Image"),"state":(agent or {}).get("State",{}).get("Status")},
                "containers":len(containers)})
        except Exception as exc: result.append({"id":eid,"name":e.get("Name"),"docker":False,"error":str(exc)})
    return {"nodes":result,"capacity_agent_image":CAPACITY_AGENT_IMAGE}

@app.post("/api/endpoints/{endpoint_id}/capacity-agent/upgrade")
async def upgrade_capacity_agent(endpoint_id:int,session=Depends(require_csrf)):
    if "admin" not in user_permissions(session.get("user","")): raise HTTPException(403,"Permission denied")
    from .capacity import install_capacity_agent
    return await install_capacity_agent(endpoint_id,session)

@app.get("/api/backups")
async def backup_inventory(session=Depends(require_permission("admin"))):
    with db() as conn:
        rows=conn.execute("SELECT key,value,updated_at FROM app_settings WHERE key LIKE 'backup:%' ORDER BY updated_at DESC").fetchall()
    return {"backups":[{"key":r["key"],"value":json.loads(r["value"]),"updated_at":r["updated_at"]} for r in rows]}

@app.post("/api/stacks/{stack_id}/backup")
async def create_stack_backup(stack_id:int,session=Depends(require_csrf)):
    if "admin" not in user_permissions(session.get("user","")) and "migrations" not in user_permissions(session.get("user","")): raise HTTPException(403,"Permission denied")
    detail=await build_detail(stack_id); stack_file=await get_stack_file(stack_id); endpoint_id=int(detail["stack"]["endpoint_id"])
    backup_id=uuid.uuid4().hex[:12]; copies=[]; stopped=False
    try:
        await stop_stack(stack_id,endpoint_id); stopped=True; await asyncio.sleep(2)
        for volume in detail["volumes"]:
            source=volume["name"]; target="dsm-backup-"+backup_id+"-"+source
            await create_volume(endpoint_id,target,volume.get("driver") or "local")
            await copy_volume(endpoint_id,endpoint_id,source,target); copies.append({"source":source,"backup":target})
    finally:
        if stopped: await start_stack(stack_id,endpoint_id)
    manifest={"id":backup_id,"created_at":utcnow(),"stack":detail["stack"],"volumes":copies,
              "domains":detail["domains"],"stack_file":stack_file,"type":"local-volume-snapshot"}
    setting_set("backup:"+backup_id,json.dumps(manifest))
    return manifest

