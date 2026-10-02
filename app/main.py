from pathlib import Path
from fastapi.responses import FileResponse

from .core import app
from .routes import general, migration  # register routes

STATIC_DIR = Path(__file__).resolve().parent / "static"

@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")
