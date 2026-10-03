import asyncio
import json
import os
from pathlib import Path

import httpx
from fastapi import Depends, HTTPException

from ..core import app, require_csrf, current_session, user_permissions

IMAGE = "ghcr.io/drbanek/dockerstackmover:latest"
VERSION_URL = "https://raw.githubusercontent.com/Drbanek/DockerStackMover/main/VERSION"
SOCKET = "/var/run/docker.sock"
COMPOSE_DIR = "/opt/dockerstackmover"


def _version():
    try:
        return Path("/app/VERSION").read_text().strip()
    except Exception:
        return os.getenv("DSM_VERSION", "unknown")


def _admin(session):
    if "admin" not in user_permissions(session.get("user", "")):
        raise HTTPException(403, "Permission denied")


async def _latest_version():
    async with httpx.AsyncClient(timeout=8, follow_redirects=True) as c:
        r = await c.get(VERSION_URL, headers={"Cache-Control": "no-cache"})
        r.raise_for_status()
        return r.text.strip()


@app.get("/api/system/update")
async def update_status(session=Depends(current_session)):
    _admin(session)
    current = _version()
    try:
        latest = await _latest_version()
        error = ""
    except Exception as exc:
        latest = ""
        error = str(exc)
    ready = os.path.exists(SOCKET) and os.path.exists(COMPOSE_DIR)
    return {
        "current": current,
        "latest": latest,
        "update_available": bool(latest and current != latest),
        "updater_ready": ready,
        "last_check": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "error": error,
    }


@app.post("/api/system/update")
async def run_update(session=Depends(require_csrf)):
    _admin(session)
    if not os.path.exists(SOCKET):
        raise HTTPException(409, "Docker socket není připojen k DockerStackMoveru.")
    if not os.path.exists(COMPOSE_DIR):
        raise HTTPException(409, "Hostitelská /opt/dockerstackmover není připojena do kontejneru.")
    try:
        latest = await _latest_version()
    except Exception as exc:
        raise HTTPException(502, "Nelze ověřit cílovou verzi: " + str(exc))

    # A short-lived Docker CLI helper survives recreation of this web container.
    # It remembers the current image ID and restores it if the new container
    # does not answer its health endpoint.
    script = r"""set -eu
cd /work
OLD=$(docker image inspect ghcr.io/drbanek/dockerstackmover:latest --format '{{.Id}}')
docker compose --env-file .env -f compose.yaml pull dockerstackmover
docker compose --env-file .env -f compose.yaml up -d --force-recreate dockerstackmover
IP=$(awk -F= '$1=="MOVER_BIND_IP"{print $2}' .env)
PORT=$(awk -F= '$1=="MOVER_PORT"{print $2}' .env)
OK=0
for i in $(seq 1 45); do
  if wget -q -T 2 -O /dev/null "http://$IP:$PORT/api/setup/status"; then OK=1; break; fi
  sleep 2
done
if [ "$OK" != 1 ]; then
  docker tag "$OLD" ghcr.io/drbanek/dockerstackmover:latest
  docker compose --env-file .env -f compose.yaml up -d --force-recreate --pull never dockerstackmover
  exit 42
fi
"""
    transport = httpx.AsyncHTTPTransport(uds=SOCKET)
    async with httpx.AsyncClient(transport=transport, base_url="http://docker", timeout=30) as d:
        create = await d.post("/containers/create", params={"name": "dockerstackmover-updater"}, json={
            "Image": "docker:cli",
            "Cmd": ["sh", "-c", "sleep 2; " + script],
            "HostConfig": {
                "AutoRemove": True,
                "Binds": [
                    "/var/run/docker.sock:/var/run/docker.sock",
                    "/opt/dockerstackmover:/work",
                ],
            },
        })
        if create.status_code == 404:
            pull = await d.post("/images/create", params={"fromImage": "docker", "tag": "cli"}, timeout=180)
            if pull.status_code not in (200, 201):
                raise HTTPException(502, "Nelze stáhnout pomocný docker:cli image.")
            create = await d.post("/containers/create", params={"name": "dockerstackmover-updater"}, json={
                "Image": "docker:cli", "Cmd": ["sh", "-c", "sleep 2; " + script],
                "HostConfig": {"AutoRemove": True, "Binds": ["/var/run/docker.sock:/var/run/docker.sock", "/opt/dockerstackmover:/work"]},
            })
        if create.status_code not in (201,):
            raise HTTPException(502, "Nelze vytvořit updater: " + create.text[:300])
        cid = create.json()["Id"]
        start = await d.post("/containers/" + cid + "/start")
        if start.status_code not in (204, 304):
            raise HTTPException(502, "Nelze spustit updater: " + start.text[:300])
    return {"ok": True, "target": latest, "message": "Aktualizace byla spuštěna. Web se během aktualizace krátce odpojí."}
