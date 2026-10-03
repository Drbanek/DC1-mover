from pathlib import Path
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .core import app
from .routes import general, proxy, migration, capacity, dns, settings, users, operations, provisioning, bootstrap_infra, update

STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")
