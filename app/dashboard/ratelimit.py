"""Rate-limit на входе: до роутинга (и до парсинга тела).

Per-key лимит (по Authorization), иначе per-IP. Превышение — 429 + Retry-After.
Лимиты берутся из STORE["settings"]: rate_limit_per_min / rate_limit_per_key_min.
Ядро при переезде на настоящий gateway сохраняет семантику: 429 + Retry-After.
/healthz и /static не лимитируются (иначе деплой-чек и UI лягут).
"""
from __future__ import annotations

import time

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

_buckets: dict[str, list[float]] = {}
_WINDOW = 60.0


def _limited(key: str, limit: int) -> int:
    """Вернуть 0 если можно, иначе секунды до сброса окна."""
    now = time.monotonic()
    hits = [t for t in _buckets.get(key, []) if now - t < _WINDOW]
    if len(hits) < max(1, limit):
        hits.append(now)
        _buckets[key] = hits
        return 0
    _buckets[key] = hits
    return int(_WINDOW - (now - hits[0])) + 1


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path
        if path == "/healthz" or path.startswith("/static"):
            return await call_next(request)
        # ponytail: лимиты из STORE-заглушки; ядру читать из своего конфига.
        from .store import STORE

        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer ") and len(auth) > 7:
            limit = int(STORE["settings"].get("rate_limit_per_key_min", 120))
            bucket = "key:" + auth[7:20]
        else:
            limit = int(STORE["settings"].get("rate_limit_per_min", 600))
            bucket = "ip:" + (request.client.host if request.client else "?")
        wait = _limited(bucket, limit)
        if wait:
            return JSONResponse({"detail": "слишком много запросов"},
                                status_code=429, headers={"Retry-After": str(wait)})
        resp = await call_next(request)
        return resp
