import os, shutil
from fastapi import FastAPI, Header, HTTPException

app = FastAPI(title="DockerStackMover Capacity Agent")
TOKEN = os.getenv("AGENT_TOKEN", "")

def auth(x_agent_token: str | None):
    if not TOKEN or x_agent_token != TOKEN:
        raise HTTPException(status_code=401, detail="unauthorized")

def usage(path: str):
    u = shutil.disk_usage(path)
    return {"path": path, "total": u.total, "used": u.used, "free": u.free}

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/capacity")
def capacity(x_agent_token: str | None = Header(default=None)):
    auth(x_agent_token)
    return {
        "data": usage("/host/srv"),
        "system": usage("/host/docker")
    }
