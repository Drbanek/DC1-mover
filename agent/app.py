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
    full=subprocess.run(["nft","-j","list","ruleset"],capture_output=True,text=True,timeout=10)
    if full.returncode != 0: raise HTTPException(500,(full.stderr or full.stdout)[:500])
    try: full_ruleset=json.loads(full.stdout)
    except Exception: full_ruleset={"raw":full.stdout}
    p=subprocess.run(["nft","-j","list","table","inet","dockerstackmover"],capture_output=True,text=True,timeout=10)
    if p.returncode != 0:
        # A freshly installed agent has no managed table yet. This is a valid,
        # unconfigured state; the UI can then offer to apply the policy.
        if "No such file or directory" in (p.stderr or ""):
            return {"managed":False,"configured":False,"message":"Firewall policy not configured yet","full_ruleset":full_ruleset}
        raise HTTPException(500,(p.stderr or p.stdout)[:500])
    try: return {"managed":True,"configured":True,"ruleset":json.loads(p.stdout),"full_ruleset":full_ruleset}
    except Exception: return {"managed":True,"configured":True,"raw":p.stdout,"full_ruleset":full_ruleset}

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
