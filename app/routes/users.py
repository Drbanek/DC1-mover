from ..core import *

VALID_PERMISSIONS = {"dashboard_read", "migrations", "dns_read", "dns_write", "admin"}

@app.get("/api/users")
async def users_list(session=Depends(require_permission("admin"))):
    with db() as conn:
        rows = conn.execute("SELECT username,permissions,enabled,updated_at FROM app_users ORDER BY username").fetchall()
    return [{"username":r["username"],"permissions":[p for p in (r["permissions"] or "").split(",") if p],"enabled":bool(r["enabled"]),"updated_at":r["updated_at"]} for r in rows]

@app.post("/api/users")
async def users_create(request: Request, session=Depends(require_csrf)):
    if "admin" not in user_permissions(session.get("user","")): raise HTTPException(403,"Permission denied")
    p=await request.json(); username=str(p.get("username") or "").strip(); password=str(p.get("password") or "")
    perms=set(p.get("permissions") or []) & VALID_PERMISSIONS
    if not username or len(password)<10: raise HTTPException(400,"Username is required and password must have at least 10 characters")
    try:
        with db() as conn: conn.execute("INSERT INTO app_users(username,password_hash,updated_at,permissions,enabled) VALUES(?,?,?,?,1)",(username,password_hash(password),utcnow(),",".join(sorted(perms))))
    except sqlite3.IntegrityError: raise HTTPException(409,"User already exists")
    return {"ok":True}

@app.put("/api/users/{username}")
async def users_update(username: str, request: Request, session=Depends(require_csrf)):
    if "admin" not in user_permissions(session.get("user","")): raise HTTPException(403,"Permission denied")
    p=await request.json(); perms=set(p.get("permissions") or []) & VALID_PERMISSIONS; enabled=1 if p.get("enabled",True) else 0
    if username==session.get("user") and ("admin" not in perms or not enabled): raise HTTPException(400,"You cannot remove your own administrator access")
    with db() as conn:
        row=conn.execute("SELECT 1 FROM app_users WHERE username=?",(username,)).fetchone()
        if not row: raise HTTPException(404,"User not found")
        if p.get("password"):
            if len(str(p["password"]))<10: raise HTTPException(400,"Password must have at least 10 characters")
            conn.execute("UPDATE app_users SET password_hash=?,permissions=?,enabled=?,updated_at=? WHERE username=?",(password_hash(str(p["password"])),",".join(sorted(perms)),enabled,utcnow(),username))
        else: conn.execute("UPDATE app_users SET permissions=?,enabled=?,updated_at=? WHERE username=?",(",".join(sorted(perms)),enabled,utcnow(),username))
    return {"ok":True}

@app.delete("/api/users/{username}")
async def users_delete(username: str, session=Depends(require_csrf)):
    if "admin" not in user_permissions(session.get("user","")): raise HTTPException(403,"Permission denied")
    if username==session.get("user"): raise HTTPException(400,"You cannot delete your own account")
    with db() as conn: conn.execute("DELETE FROM app_users WHERE username=?",(username,))
    return {"ok":True}
