import os, shutil, json, subprocess, ipaddress, threading, time, uuid
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
    confirm_timeout: int = 90

_pending = {}
_pending_lock = threading.Lock()

def managed_snapshot():
    p=subprocess.run(["nft","-j","list","table","inet","dockerstackmover"],capture_output=True,text=True,timeout=10)
    if p.returncode != 0: return None
    try: return json.loads(p.stdout)
    except Exception: return None

def restore_snapshot(snapshot):
    subprocess.run(["nft","delete","table","inet","dockerstackmover"],capture_output=True,text=True)
    if snapshot:
        nft(["-j","-f","-"],json.dumps(snapshot))

def schedule_rollback(txid, seconds):
    def worker():
        time.sleep(seconds)
        with _pending_lock: tx=_pending.pop(txid,None)
        if tx: restore_snapshot(tx["before"])
    threading.Thread(target=worker,daemon=True).start()

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
    if any(ipaddress.ip_address(x).version != 4 for x in sources): raise HTTPException(400,"IPv4 management sources only")
    before=managed_snapshot(); timeout=max(30,min(int(policy.confirm_timeout or 90),300))
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
    check=subprocess.run(["nft","-c","-f","-"],input=rules,text=True,capture_output=True,timeout=10)
    if check.returncode: raise HTTPException(400,(check.stderr or check.stdout)[:500])
    try: nft(["-f","-"],rules)
    except Exception:
        restore_snapshot(before); raise
    txid=uuid.uuid4().hex
    with _pending_lock: _pending[txid]={"before":before}
    schedule_rollback(txid,timeout)
    return {"ok":True,"pending_confirmation":True,"transaction_id":txid,"confirm_timeout":timeout,"management_sources":sources,"management_ports":ports}

@app.post("/firewall/confirm/{txid}")
def firewall_confirm(txid: str, x_agent_token: str | None = Header(default=None)):
    auth(x_agent_token)
    with _pending_lock: tx=_pending.pop(txid,None)
    if not tx: raise HTTPException(409,"Firewall change is no longer pending")
    return {"ok":True,"confirmed":True}

@app.post("/firewall/rollback/{txid}")
def firewall_rollback(txid: str, x_agent_token: str | None = Header(default=None)):
    auth(x_agent_token)
    with _pending_lock: tx=_pending.pop(txid,None)
    if not tx: raise HTTPException(409,"Firewall change is no longer pending")
    restore_snapshot(tx["before"])
    return {"ok":True,"rolled_back":True}
