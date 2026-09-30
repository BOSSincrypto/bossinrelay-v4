from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .dashboard.admin_api import router as admin_router
from .dashboard.ratelimit import RateLimitMiddleware
from .dashboard.views import router as views_router
from .routers.v1 import router as v1_router

app = FastAPI(title="BossInRelay — админка")
app.add_middleware(RateLimitMiddleware)
app.mount("/static", StaticFiles(directory="app/dashboard/static"), name="static")
app.include_router(admin_router)
app.include_router(views_router)
app.include_router(v1_router)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
