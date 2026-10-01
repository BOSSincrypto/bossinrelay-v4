from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from pathlib import Path

from .dashboard.admin_api import router as admin_router
from .dashboard.ratelimit import RateLimitMiddleware
from .dashboard.views import router as views_router
from .db import client as db_client
from .routers.v1 import router as v1_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    # Освободить поток libsql sync-executor ДО teardown интерпретатора,
    # иначе threading._shutdown вечно ждёт non-daemon поток (см. client.close_client).
    db_client.close_client()


app = FastAPI(title="BossInRelay — админка", lifespan=lifespan)
app.add_middleware(RateLimitMiddleware)
_BASE = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=str(_BASE / "dashboard" / "static")), name="static")
app.include_router(admin_router)
app.include_router(views_router)
app.include_router(v1_router)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
