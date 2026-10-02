import os
import re
import httpx
import asyncio
import json
import uuid
import sqlite3
import secrets
import hashlib
import hmac
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import HTMLResponse, Response

app = FastAPI(title="DC1 Mover")

PORTAINER_URL = os.environ["PORTAINER_URL"].rstrip("/")
PORTAINER_TOKEN = os.environ["PORTAINER_TOKEN"]
headers = {"X-API-Key": PORTAINER_TOKEN}

DB_PATH = "/data/mover.db"
MOVER_USER = os.getenv("MOVER_USER", "admin")
MOVER_PASSWORD = os.getenv("MOVER_PASSWORD", "")
SESSION_SECRET = os.getenv("MOVER_SESSION_SECRET", "")
SESSION_COOKIE = "dc1_mover_session"
sessions = {}

if not MOVER_PASSWORD:
    raise RuntimeError("MOVER_PASSWORD is required")
if len(SESSION_SECRET) < 32:
    raise RuntimeError("MOVER_SESSION_SECRET must contain at least 32 characters")

def password_ok(value):
    return hmac.compare_digest(value or "", MOVER_PASSWORD)

def new_session():
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    sessions[token] = {"user": MOVER_USER, "csrf": csrf}
    return token, csrf

def current_session(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    session = sessions.get(token)
    if not session:
        raise HTTPException(401, "Authentication required")
    return session

def require_csrf(request: Request, session=Depends(current_session)):
    supplied = request.headers.get("X-CSRF-Token", "")
    if not hmac.compare_digest(supplied, session["csrf"]):
        raise HTTPException(403, "Invalid CSRF token")
    return session

migration_jobs = {}

def utcnow():
    return datetime.now(timezone.utc).isoformat()

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS migrations (
                id TEXT PRIMARY KEY,
                stack_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                steps_json TEXT NOT NULL,
                result_json TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS migration_locks (
                stack_id INTEGER PRIMARY KEY,
                job_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS endpoint_settings (
                endpoint_id INTEGER PRIMARY KEY,
                migration_enabled INTEGER NOT NULL DEFAULT 0,
                host_ip TEXT,
                site TEXT,
                updated_at TEXT NOT NULL
            )
        """)

def get_endpoint_settings():
    with db() as conn:
        rows = conn.execute("SELECT * FROM endpoint_settings").fetchall()
    return {int(r["endpoint_id"]): {"migration_enabled": bool(r["migration_enabled"]), "host_ip": r["host_ip"] or "", "site": r["site"] or ""} for r in rows}

def save_endpoint_setting(endpoint_id, migration_enabled, host_ip="", site=""):
    with db() as conn:
        conn.execute("""
            INSERT INTO endpoint_settings(endpoint_id, migration_enabled, host_ip, site, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(endpoint_id) DO UPDATE SET migration_enabled=excluded.migration_enabled,
            host_ip=excluded.host_ip, site=excluded.site, updated_at=excluded.updated_at
        """, (int(endpoint_id), 1 if migration_enabled else 0, (host_ip or "").strip(), (site or "").strip(), utcnow()))

def migration_endpoints(endpoints):
    settings = get_endpoint_settings()
    return [e for e in endpoints if settings.get(int(e["Id"]), {}).get("migration_enabled", False)]

def acquire_stack_lock(stack_id, job_id):
    try:
        with db() as conn:
            conn.execute("INSERT INTO migration_locks(stack_id, job_id, created_at) VALUES (?, ?, ?)", (stack_id, job_id, utcnow()))
        return True
    except sqlite3.IntegrityError:
        return False

def release_stack_lock(stack_id, job_id):
    with db() as conn:
        conn.execute("DELETE FROM migration_locks WHERE stack_id = ? AND job_id = ?", (stack_id, job_id))

def persist_job(job):
    now = utcnow(); job["updated_at"] = now; job.setdefault("created_at", now)
    with db() as conn:
        conn.execute("""
            INSERT INTO migrations (id, stack_id, target_id, status, steps_json, result_json, error, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET status=excluded.status, steps_json=excluded.steps_json,
            result_json=excluded.result_json, error=excluded.error, updated_at=excluded.updated_at
        """, (job["id"], job["stack_id"], job["target_id"], job["status"], json.dumps(job.get("steps", [])), json.dumps(job.get("result")) if job.get("result") is not None else None, job.get("error"), job["created_at"], job["updated_at"]))

def load_job(job_id):
    if job_id in migration_jobs: return migration_jobs[job_id]
    with db() as conn:
        row = conn.execute("SELECT * FROM migrations WHERE id = ?", (job_id,)).fetchone()
    if not row: return None
    job = {"id": row["id"], "stack_id": row["stack_id"], "target_id": row["target_id"], "status": row["status"], "steps": json.loads(row["steps_json"] or "[]"), "result": json.loads(row["result_json"]) if row["result_json"] else None, "error": row["error"], "created_at": row["created_at"], "updated_at": row["updated_at"]}
    migration_jobs[job_id] = job
    return job

def load_recent_jobs(limit=50):
    with db() as conn:
        rows = conn.execute("SELECT id FROM migrations ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [load_job(row["id"]) for row in rows]

def new_job(stack_id, target_id):
    job_id = uuid.uuid4().hex
    job = {"id": job_id, "stack_id": stack_id, "target_id": target_id, "status": "queued", "steps": [], "result": None, "error": None, "created_at": utcnow(), "updated_at": utcnow()}
    if not acquire_stack_lock(stack_id, job_id): raise HTTPException(409, "Tento stack už má aktivní migraci")
    migration_jobs[job_id] = job; persist_job(job); return job

def job_step(job, name, state, message=""):
    existing = next((s for s in job["steps"] if s["name"] == name), None)
    payload = {"name": name, "state": state, "message": message}
    if existing: existing.update(payload)
    else: job["steps"].append(payload)
    persist_job(job)

init_db()

def client():
    return httpx.AsyncClient(base_url=PORTAINER_URL, headers=headers, verify=False, timeout=30)

async def pget(path, params=None):
    async with client() as c:
        r = await c.get(path, params=params)
        if r.status_code != 200: raise HTTPException(r.status_code, r.text)
        return r.json()

async def get_endpoints(): return await pget("/api/endpoints")
async def get_stacks(): return await pget("/api/stacks")

async def docker_get(endpoint_id, path, params=None, allowed=(200,)):
    async with client() as c:
        r = await c.get("/api/endpoints/" + str(endpoint_id) + "/docker" + path, params=params)
        if r.status_code not in allowed: raise HTTPException(r.status_code, "Docker API error: " + r.text)
        return r

async def docker_request(endpoint_id, method, path, **kwargs):
    async with client() as c:
        return await c.request(method, "/api/endpoints/" + str(endpoint_id) + "/docker" + path, **kwargs)

async def wait_container(endpoint_id, container_id, timeout=300):
    async with client() as c:
        r = await c.post("/api/endpoints/" + str(endpoint_id) + "/docker/containers/" + container_id + "/wait", json={"condition": "not-running"}, timeout=timeout)
        if r.status_code != 200: raise HTTPException(r.status_code, r.text)
        return int(r.json().get("StatusCode", 1))

async def remove_container(endpoint_id, container_id):
    async with client() as c:
        await c.delete("/api/endpoints/" + str(endpoint_id) + "/docker/containers/" + container_id, params={"force": "1", "v": "0"})

async def ensure_image(endpoint_id, image):
    async with client() as c:
        r = await c.post("/api/endpoints/" + str(endpoint_id) + "/docker/images/create", params={"fromImage": image}, timeout=300)
        if r.status_code not in (200, 201): raise HTTPException(r.status_code, "Image pull failed: " + r.text)

async def create_volume(endpoint_id, name, driver="local"):
    async with client() as c:
        r = await c.post("/api/endpoints/" + str(endpoint_id) + "/docker/volumes/create", json={"Name": name, "Driver": driver or "local"})
        if r.status_code not in (200, 201): raise HTTPException(r.status_code, "Volume create failed: " + r.text)
        return r.json()

async def copy_volume(source_id, target_id, volume_name):
    helper_image = "alpine:3.22"; await ensure_image(source_id, helper_image); await ensure_image(target_id, helper_image)
    src_name = "dc1-mover-src-" + uuid.uuid4().hex[:10]; dst_name = "dc1-mover-dst-" + uuid.uuid4().hex[:10]; src_id = None; dst_id = None
    try:
        r = await docker_request(source_id, "POST", "/containers/create", params={"name": src_name}, json={"Image": helper_image, "Cmd": ["sh", "-c", "true"], "HostConfig": {"Mounts": [{"Type": "volume", "Source": volume_name, "Target": "/volume", "ReadOnly": True}]}})
        if r.status_code != 201: raise HTTPException(r.status_code, "Source helper create failed: " + r.text)
        src_id = r.json()["Id"]
        r = await docker_request(target_id, "POST", "/containers/create", params={"name": dst_name}, json={"Image": helper_image, "Cmd": ["sh", "-c", "true"], "HostConfig": {"Mounts": [{"Type": "volume", "Source": volume_name, "Target": "/volume"}]}})
        if r.status_code != 201: raise HTTPException(r.status_code, "Target helper create failed: " + r.text)
        dst_id = r.json()["Id"]
        async with client() as c:
            async with c.stream("GET", "/api/endpoints/" + str(source_id) + "/docker/containers/" + src_id + "/archive", params={"path": "/volume/."}, timeout=None) as source:
                if source.status_code != 200:
                    body = await source.aread(); raise HTTPException(source.status_code, "Volume archive read failed: " + body.decode("utf-8", errors="replace"))
                async def archive_stream():
                    async for chunk in source.aiter_bytes(): yield chunk
                r = await c.put("/api/endpoints/" + str(target_id) + "/docker/containers/" + dst_id + "/archive", params={"path": "/volume"}, content=archive_stream(), headers={**headers, "Content-Type": "application/x-tar"}, timeout=None)
                if r.status_code != 200: raise HTTPException(r.status_code, "Volume archive restore failed: " + r.text)
    finally:
        if src_id: await remove_container(source_id, src_id)
        if dst_id: await remove_container(target_id, dst_id)

async def get_stack_file(stack_id):
    async with client() as c:
        r = await c.get("/api/stacks/" + str(stack_id) + "/file")
        if r.status_code != 200: raise HTTPException(r.status_code, "Cannot read stack file: " + r.text)
        return r.json().get("StackFileContent", "")

async def stop_stack(stack_id, endpoint_id):
    async with client() as c:
        r = await c.post("/api/stacks/" + str(stack_id) + "/stop", params={"endpointId": endpoint_id})
        if r.status_code not in (200, 204): raise HTTPException(r.status_code, "Cannot stop source stack: " + r.text)

async def start_stack(stack_id, endpoint_id):
    async with client() as c:
        r = await c.post("/api/stacks/" + str(stack_id) + "/start", params={"endpointId": endpoint_id})
        if r.status_code not in (200, 204): raise HTTPException(r.status_code, "Cannot start source stack: " + r.text)

async def create_target_stack(target_id, name, stack_file, env):
    async with client() as c:
        r = await c.post("/api/stacks/create/standalone/string", params={"endpointId": target_id}, json={"Name": name, "StackFileContent": stack_file, "Env": env or []}, timeout=120)
        if r.status_code not in (200, 201): raise HTTPException(r.status_code, "Target stack create failed: " + r.text)
        return r.json()

async def build_detail(stack_id):
    stacks = await get_stacks(); endpoints = await get_endpoints(); stack = next((s for s in stacks if s["Id"] == stack_id), None)
    if not stack: raise HTTPException(404, "Stack not found")
    endpoint_id = stack["EndpointId"]; endpoint = next((e for e in endpoints if e["Id"] == endpoint_id), None)
    all_containers = (await docker_get(endpoint_id, "/containers/json", params={"all": "1"})).json(); containers = []
    for container in all_containers:
        labels = container.get("Labels") or {}
        if labels.get("com.docker.compose.project") != stack["Name"]: continue
        mounts = [{"type": m.get("Type"), "name": m.get("Name"), "source": m.get("Source"), "destination": m.get("Destination"), "rw": m.get("RW")} for m in container.get("Mounts", [])]
        ports = [{"ip": p.get("IP"), "private": p.get("PrivatePort"), "public": p.get("PublicPort"), "type": p.get("Type")} for p in container.get("Ports", [])]
        containers.append({"id": container["Id"][:12], "name": container.get("Names", ["unknown"])[0].lstrip("/"), "image": container.get("Image"), "state": container.get("State"), "status": container.get("Status"), "mounts": mounts, "ports": ports, "labels": labels})
    volume_names = sorted({m["name"] for c in containers for m in c["mounts"] if m["type"] == "volume" and m["name"]}); volumes = []
    for volume_name in volume_names:
        r = await docker_get(endpoint_id, "/volumes/" + volume_name, allowed=(200, 404))
        if r.status_code == 200:
            v = r.json(); volumes.append({"name": v.get("Name"), "driver": v.get("Driver"), "mountpoint": v.get("Mountpoint")})
        else: volumes.append({"name": volume_name, "driver": "unknown", "mountpoint": None})
    domains = []
    for container in containers:
        labels = container["labels"]
        if labels.get("dc1.proxy.enable") == "true": domains.append({"host": labels.get("dc1.proxy.host"), "port": labels.get("dc1.proxy.port"), "scheme": labels.get("dc1.proxy.scheme", "http")})
    endpoint_name = endpoint["Name"] if endpoint else "Endpoint " + str(endpoint_id)
    return {"stack": {"id": stack["Id"], "name": stack["Name"], "endpoint_id": endpoint_id, "endpoint": endpoint_name, "status": stack["Status"]}, "containers": containers, "volumes": volumes, "domains": domains}
