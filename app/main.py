"""应用入口。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import config
from .api import router
from .db import create_all, init_engines


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_engines()
    create_all()
    config.ensure_dirs()
    yield


app = FastAPI(title="经营归因分析 Agent", version="0.1.0", lifespan=lifespan)
app.include_router(router)

_static = Path(__file__).resolve().parent / "web" / "static"
_static.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_static)), name="static")
