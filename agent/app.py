import os, shutil, json, subprocess, ipaddress
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

app = FastAPI(title="DockerStackMover Node Agent")
TOKEN = os.getenv("AGENT_TOKEN", "")

def auth(x_agent_token: str | None):
    if not TOKEN or x_agent_token != TOKEN: raise HTTPException(401, "unauthorized")

def usage(path):
    u=shutil.disk_usage(path); return {"path":path,"total":u.total,"used":u.used,"free":u.free}

def nft(args, stdin=None):
    try:
        p=subprocess.run(["nft"]+args,input=stdin,text=True,capture_output=True,timeout=10)
    except Exception as exc: raise HTTPException(500,"nft failed: "+str(exc))
    if p.returncode: raise HTTPException(500,(p.stderr or p.stdout)[:500])
    return p.stdout

class FirewallPolicy(BaseModel):
    management_sources: list[str]
    management_ports: list[int] = [9001, 9100]

def normalize_policy(p):
    sources=[]
    for value in p.management_sources:
        try: sources.append(str(ipaddress.ip_address(value)))
        except ValueError: raise HTTPException(400,"Invalid management IP: "+value)
    ports=sorted({int(x) for x in p.management_ports if 1 <= int(x) <= 65535})
    if not sources or not ports: raise HTTPException(400,"Management sources and ports are required")
    return sources,ports

@app.get("/health")
def health(): return {"ok":True}

@app.get("/capacity")
def capacity(x_agent_token: str | None = Header(default=None)):
    auth(x_agent_token); return {"data":usage("/host/srv"),"system":usage("/host/docker")}

@app.get("/firewall")
def firewall(x_agent_token: str | None = Header(default=None)):
    auth(x_agent_token)
    out=nft(["-j","list","table","inet","dockerstackmover"])
    try: return {"managed":True,"ruleset":json.loads(out)}
    except Exception: return {"managed":True,"raw":out}

@app.put("/firewall")
def firewall_apply(policy: FirewallPolicy, x_agent_token: str | None = Header(default=None)):
    auth(x_agent_token); sources,ports=normalize_policy(policy)
    src=", ".join(sources); pts=", ".join(str(x) for x in ports)
    rules=f"""table inet dockerstackmover {{
 chain input {{
  type filter hook input priority -5; policy accept;
  ct state established,related accept
  iifname "lo" accept
  ip saddr {{ {src} }} tcp dport {{ {pts} }} accept
  tcp dport {{ {pts} }} drop
 }}
}}"""
    subprocess.run(["nft","delete","table","inet","dockerstackmover"],capture_output=True,text=True)
    nft(["-f","-"],rules)
    return {"ok":True,"management_sources":sources,"management_ports":ports}
