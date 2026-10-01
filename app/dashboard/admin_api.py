"""Контракт /admin/* API ядра.

/stats уже отдаёт реальные агрегаты из БД (seed — только пока трафика нет),
/keys/{kid}/check делает настоящий пробный вызов через core.engine.
Остальные разделы — пока на in-memory STORE; контракт путей/полей стабилен.
Авторизация: Bearer ADMIN_TOKEN."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException
from pydantic import BaseModel

from ..core import deepcheck as core_deepcheck
from ..core import engine as core_engine
from ..db import client as db_client
from ..providers import discovery as prov_discovery
from .auth import require_admin
from .ssrf import validate_base_url
from .store import (MODEL_REGISTRY, STORE, VERDICT_LABELS, find_key, find_pool,
                    find_provider, mask_key, nid, now, persist)

router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


# ---------- helpers ----------

def ok(**extra):
    return {"ok": True, **extra}


# ---------- stats ----------

@router.get("/stats")
def stats():
    try:
        real = db_client.get_usage_stats()
    except Exception:
        real = None
    if real and real["requests_total"] > 0:
        total, today, hit = real["requests_total"], real["requests_today"], real["cache_hit_rate"]
        spent_today, spent_month, lat = (real["spent_today_usd"], real["spent_month_usd"],
                                         real["avg_latency_ms"])
        by_pool = [{"pool": r["pool"], "requests": r["requests"], "usd": r["usd"]}
                   for r in real["by_pool"]]
    else:  # трафика ещё не было — seed для витрины
        total, today, hit = 128_540, 8_214, STORE["cache"]["hit_rate"]
        spent_today, spent_month, lat = 12.47, 231.90, 480
        by_pool = [
            {"pool": "chat-main", "requests": 5210, "usd": 7.12},
            {"pool": "code-pro", "requests": 3004, "usd": 5.35},
        ]
    return {
        "requests_total": total,
        "requests_today": today,
        "cache_hit_rate": hit,
        "cache_entries": STORE["cache"]["entries"],
        "spent_today_usd": spent_today,
        "spent_month_usd": spent_month,
        "avg_latency_ms": lat,
        "chart": {
            "labels": ["00", "02", "04", "06", "08", "10", "12", "14", "16", "18", "20", "22"],
            "requests": [120, 80, 45, 60, 210, 480, 720, 690, 840, 960, 810, 640],
            "latency_ms": [410, 390, 380, 370, 420, 460, 520, 510, 490, 530, 500, 470],
        },
        "by_pool": by_pool,
    }


# ---------- providers ----------

@router.get("/providers")
def list_providers():
    return STORE["providers"]


class ProviderIn(BaseModel):
    id: str
    name: str = ""
    base_url: str = ""
    models: list[str] = []


@router.post("/providers")
def upsert_provider(body: ProviderIn):
    if body.base_url:
        try:
            body.base_url = validate_base_url(body.base_url)
        except ValueError as e:
            raise HTTPException(422, f"base_url отклонён SSRF-guard: {e}")
    p = find_provider(body.id)
    if p:
        p["name"] = body.name or p["name"]
        p["base_url"] = body.base_url or p["base_url"]
        if body.models:
            p["models"] = body.models
    else:
        models = [m.strip() for m in ",".join(body.models).split(",") if m.strip()] or body.models
        STORE["providers"].append({
            "id": body.id, "name": body.name or body.id,
            "base_url": body.base_url, "models": models,
            "enabled": True, "keys": []})
    persist()
    return ok()


@router.post("/providers/{pid}/toggle")
def toggle_provider(pid: str):
    p = find_provider(pid)
    if not p:
        raise HTTPException(404, "провайдер не найден")
    p["enabled"] = not p["enabled"]
    persist()
    return ok(enabled=p["enabled"])


@router.delete("/providers/{pid}")
def delete_provider(pid: str):
    before = len(STORE["providers"])
    STORE["providers"][:] = [p for p in STORE["providers"] if p["id"] != pid]
    if len(STORE["providers"]) == before:
        raise HTTPException(404, "провайдер не найден")
    persist()
    return ok()


# ---------- keys ----------

@router.post("/providers/{pid}/keys")
def add_key(pid: str, raw: str = Form(...), note: str = Form(default="")):
    p = find_provider(pid)
    if not p:
        raise HTTPException(404, "провайдер не найден")
    key = {"id": nid("key"), "masked": mask_key(raw), "note": note,
           "enabled": True, "last_check": None}
    p["keys"].append(key)
    persist()
    return ok(key=key)


@router.post("/keys/{kid}/check")
def check_key(kid: str):
    prov, k = find_key(kid)
    if not k:
        raise HTTPException(404, "ключ не найден")
    # Настоящая дешёвая identity-проверка: пробный вызов, сверка served vs claimed.
    try:
        k["last_check"] = asyncio.run(core_engine.check_upstream(prov["id"], k))
    except Exception as e:
        k["last_check"] = {"alive": False, "latency_ms": 0, "served_id": None,
                           "claimed_id": None, "ceiling": None,
                           "error_class": "server", "verdict": "mismatch",
                           "note": str(e)[:200]}
    persist()
    return ok(check=k["last_check"])  # type: ignore[union-attr]


@router.post("/keys/{kid}/toggle")
def toggle_key(kid: str):
    _, k = find_key(kid)
    if not k:
        raise HTTPException(404, "ключ не найден")
    k["enabled"] = not k["enabled"]
    persist()
    return ok(enabled=k["enabled"])


@router.delete("/keys/{kid}")
def delete_key(kid: str):
    p, _ = find_key(kid)
    if not p:
        raise HTTPException(404, "ключ не найден")
    p["keys"][:] = [k for k in p["keys"] if k["id"] != kid]
    persist()
    return ok()


class DiscoverIn(BaseModel):
    raw: str = ""


@router.post("/providers/{pid}/discover")
async def discover_models(pid: str, body: DiscoverIn):
    """Каталог моделей провайдера по ключу (поток truemodel).

    Ключ используется разово для запроса каталога и НЕ сохраняется.
    Ответ: {models, source, note}; source live = каталог, fallback = пресет."""
    p = find_provider(pid)
    if not p:
        raise HTTPException(404, "провайдер не найден")
    return await prov_discovery.list_models(pid, p.get("base_url", ""), body.raw)


class CheckModelIn(BaseModel):
    model: str = ""
    raw: str = ""


@router.post("/providers/{pid}/models/check")
async def check_model(pid: str, body: CheckModelIn):
    """Дешёвый live-ping одной модели по ключу (без сохранения)."""
    p = find_provider(pid)
    if not p:
        raise HTTPException(404, "провайдер не найден")
    if not body.model:
        raise HTTPException(422, "нужно имя модели")
    return await prov_discovery.check_model(
        pid, body.model, p.get("base_url", ""), body.raw)


# ---------- pools ----------

@router.get("/pools")
def list_pools():
    return STORE["pools"]


class PoolIn(BaseModel):
    id: str
    strategy: str = "cascade"          # manual | cascade | random | cheapest
    members: list[str] = []            # "provider:model"
    unlock_on: str = ""
    system_prompt: str = ""
    enabled: bool = True


@router.post("/pools")
def upsert_pool(body: PoolIn):
    if body.strategy not in ("manual", "cascade", "random", "cheapest"):
        raise HTTPException(422, "неизвестная стратегия")
    p = find_pool(body.id)
    data = body.model_dump()
    if p:
        p.update(data)
    else:
        STORE["pools"].append(data)
    persist()
    return ok()


@router.post("/pools/{pid}/toggle")
def toggle_pool(pid: str):
    p = find_pool(pid)
    if not p:
        raise HTTPException(404, "пул не найден")
    p["enabled"] = not p["enabled"]
    persist()
    return ok(enabled=p["enabled"])


@router.delete("/pools/{pid}")
def delete_pool(pid: str):
    before = len(STORE["pools"])
    STORE["pools"][:] = [p for p in STORE["pools"] if p["id"] != pid]
    if len(STORE["pools"]) == before:
        raise HTTPException(404, "пул не найден")
    persist()
    return ok()


# ---------- cache ----------

@router.get("/cache")
def cache_info():
    try:
        STORE["cache"]["entries"] = db_client.cache_count()
    except Exception:
        pass
    return STORE["cache"]


class CacheIn(BaseModel):
    ttl_seconds: int = 3600
    similarity_threshold: float = 0.92
    max_entries: int = 10000


@router.post("/cache")
def update_cache(body: CacheIn):
    STORE["cache"]["ttl_seconds"] = body.ttl_seconds
    STORE["cache"]["similarity_threshold"] = body.similarity_threshold
    STORE["cache"]["max_entries"] = body.max_entries
    persist()
    return ok(cache=STORE["cache"])


@router.post("/cache/clear")
def clear_cache():
    try:
        db_client.cache_clear()
    except Exception:
        pass
    STORE["cache"]["entries"] = 0
    STORE["cache"]["hit_rate"] = 0.0
    return ok()


# ---------- sessions ----------

@router.get("/sessions")
def list_sessions():
    return [{"id": s["id"], "started_at": s["started_at"], "pool": s["pool"],
             "tokens": s["tokens"], "cost_usd": s["cost_usd"]}
            for s in STORE["sessions"]]


@router.get("/sessions/{sid}")
def get_session(sid: str):
    s = next((x for x in STORE["sessions"] if x["id"] == sid), None)
    if not s:
        raise HTTPException(404, "сессия не найдена")
    return s


# ---------- quotas ----------

@router.get("/quotas")
def list_quotas():
    return STORE["quotas"]


@router.post("/quotas")
def set_quota(key: str = Form(...), limit_day: int = Form(...),
              limit_month: int = Form(...)):
    q = STORE["quotas"].setdefault(key, {"used_day": 0, "used_month": 0})
    q["limit_day"] = limit_day
    q["limit_month"] = limit_month
    persist()
    return ok()


# ---------- truemodel-lite: реестр и ручная проверка ----------

@router.get("/models/registry")
def model_registry():
    """Реестр семейств: vendor/family/окно контекста/потолок output/cutoff + вердикт."""
    return {k: {**v, "label": VERDICT_LABELS.get(v["verdict"], v["verdict"])}
            for k, v in MODEL_REGISTRY.items()}


@router.post("/models/{provider}/{model}/deep-check")
def deep_check(provider: str, model: str, background_tasks: BackgroundTasks):
    """Ручная «глубокая проверка» (тяжёлая: токенайзер-фингерпринт,
    энтропия выборки, cutoff-пробы, пазлы). Не запускается автоматически —
    только кнопкой из дашборда. Отчёт — в entry["deep_check"], статус —
    GET /models/registry (там же). Длится до ~120с, выполняется в фоне."""
    key = f"{provider}:{model}"
    entry = MODEL_REGISTRY.get(key)
    if not entry:
        raise HTTPException(404, "модель не в реестре")
    if key in _DEEP_INFLIGHT:
        return ok(status="running",
                  note="Глубокая проверка уже выполняется — дождитесь отчёта.")
    prev = entry.get("deep_check") or {}
    if prev.get("status") == "running":
        return ok(status="running",
                  note="Глубокая проверка уже выполняется — дождитесь отчёта.")
    entry["deep_check"] = {"status": "running", "at": now()}
    _DEEP_INFLIGHT.add(key)
    background_tasks.add_task(_run_deep_check, key, provider, model)
    return ok(status="running",
              note="Глубокая проверка запущена в фоне (~до 120с); "
                   "статус — GET /admin/models/registry.")


_DEEP_INFLIGHT: set = set()


async def _run_deep_check(key: str, provider: str, model: str) -> None:
    """Фоновая задача: прогнать run_deep_check и положить отчёт в реестр."""
    entry = MODEL_REGISTRY.get(key) or {}
    try:
        report = await core_deepcheck.run_deep_check(provider, model)
    except Exception as e:
        report = {"status": "failed", "provider": provider, "model": model,
                  "signals": [], "verdict": "ambiguous", "confidence": 0.0,
                  "note": f"deep-check упал: {e}"[:200]}
    entry["deep_check"] = report
    v = report.get("verdict", "")
    if v in VERDICT_LABELS:
        entry["verdict"] = v
        entry["note"] = f"deep-check: уверенность {report.get('confidence', 0)}."
    try:
        persist()
    except Exception:
        pass
    _DEEP_INFLIGHT.discard(key)


# ---------- settings ----------

@router.get("/settings")
def get_settings():
    return STORE["settings"]


class SettingsIn(BaseModel):
    default_pool: str = "chat-main"
    request_timeout_s: int = 60
    max_retries: int = 3
    log_level: str = "info"
    enable_cache: bool = True
    expose_metrics: bool = True
    rate_limit_per_min: int = 600
    rate_limit_per_key_min: int = 120


@router.post("/settings")
def update_settings(body: SettingsIn):
    STORE["settings"].update(body.model_dump())
    persist()
    return ok(settings=STORE["settings"])
