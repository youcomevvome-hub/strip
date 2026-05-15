import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import api as api_module
from .config import get_settings
from .db import Base, engine
from .scheduler import start_scheduler, stop_scheduler
from .ui import ui_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    _auto_migrate()
    start_scheduler()
    yield
    stop_scheduler()


def _auto_migrate():
    """Tiny additive migration for SQLite: add columns if missing."""
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    for table, col, ddl in [
        ("articles", "entities",        "ALTER TABLE articles ADD COLUMN entities JSON DEFAULT '{}'"),
        ("posts",    "entities",        "ALTER TABLE posts ADD COLUMN entities JSON DEFAULT '{}'"),
        ("posts",    "likes",           "ALTER TABLE posts ADD COLUMN likes INTEGER DEFAULT 0"),
        ("posts",    "views",           "ALTER TABLE posts ADD COLUMN views INTEGER DEFAULT 0"),
        ("posts",    "cover_image_url", "ALTER TABLE posts ADD COLUMN cover_image_url TEXT"),
        ("sources",  "keywords",        "ALTER TABLE sources ADD COLUMN keywords VARCHAR(1024) DEFAULT ''"),
        ("sources",  "group_name",      "ALTER TABLE sources ADD COLUMN group_name VARCHAR(120) DEFAULT ''"),
    ]:
        if not insp.has_table(table):
            continue
        cols = {c["name"] for c in insp.get_columns(table)}
        if col not in cols:
            with engine.begin() as conn:
                conn.execute(text(ddl))


app = FastAPI(
    title="Strip API",
    description="Scrape, structure with AI, and publish to every social network.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_module.auth_router)
app.include_router(api_module.sources_router)
app.include_router(api_module.articles_router)
app.include_router(api_module.posts_router)
app.include_router(api_module.meta_router)
app.include_router(ui_router)

# Uploaded images (used by the queue rich-text editor)
UPLOAD_DIR = Path(__file__).resolve().parent.parent / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")


@app.get("/health")
def health():
    return {"ok": True}
