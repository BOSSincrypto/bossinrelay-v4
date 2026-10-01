"""Движок релея: пулы, стратегии, fallback, breaker, квоты, сессии, кэш.

Читает конфиг из dashboard STORE (та же память, что крутит админку):
пулы, ключи, настройки. Состояние гонки (success-rate, cooldown) — в памяти
процесса. Персистентное (логи, кэш, сессии, users) — в app.db.
"""
from __future__ import annotations

import json
import os
import random
import time
from urllib.parse import urlparse

from ..cache import exact as exact_cache
from ..cache import semantic as sem_cache
from ..dashboard.registry import FAMILIES, classify_error, verdict
from ..dashboard.store import STORE, find_pool, find_provider
from ..db import client as db
from ..providers import base as pbase
from ..providers import native, openai_compat

# ---------- circuit breaker: success-rate + cooldown ----------

_BREAKER: dict[str, dict] = {}  # key_id -> {ok, fail, down_until}
_SUCCESS_THRESHOLD = 0.8
_COOLDOWN_S = 120


def _bk(kid: str) -> dict:
    return _BREAKER.setdefault(kid, {"ok": 0, "fail": 0, "down_until": 0.0})


def record_success(kid: str) -> None:
    b = _bk(kid)
    b["ok"] += 1
    if b["ok"] + b["fail"] > 200:  # окно не растёт бесконечно
        b["ok"] //= 2
        b["fail"] //= 2


def record_failure(kid: str) -> None:
    b = _bk(kid)
    b["fail"] += 1
    total = b["ok"] + b["fail"]
    if total >= 5 and b["ok"] / total < _SUCCESS_THRESHOLD:
        b["down_until"] = time.time() + _COOLDOWN_S


def key_alive(kid: str) -> bool:
    return _bk(kid)["down_until"] <= time.time()


# ---------- пулы и стратегии ----------

def resolve_pool(pool_id: str) -> dict:
    p = find_pool(pool_id or STORE["settings"].get("default_pool", ""))
    if not p or not p.get("enabled", True):
        raise LookupError(f"пул '{pool_id}' не найден или выключен")
    return p


def _price_of(provider_id: str, model: str) -> float:
    fam = FAMILIES.get(model, {})
    return float(fam.get("price", 0) or 0)


def order_members(pool: dict, strategy: str | None = None) -> list[tuple[str, str]]:
    strat = strategy or pool.get("strategy", "cascade")
    members = [m.split(":", 1) for m in pool.get("members", []) if ":" in m]
    if strat == "manual":
        return members[:1]
    if strat == "random":
        random.shuffle(members)
        return members
    if strat == "cheapest":
        return sorted(members, key=lambda pm: _price_of(*pm))
    return members  # cascade: порядок задан админом


def live_keys(provider_id: str) -> list[dict]:
    p = find_provider(provider_id)
    if not p or not p.get("enabled", True):
        return []
    return [k for k in p.get("keys", [])
            if k.get("enabled", True) and key_alive(k["id"])]


def with_unlock(pool: dict, messages: list) -> list:
    """Подставить system_prompt пула первым system-сообщением, если unlock_on задан."""
    if not pool.get("unlock_on"):
        return messages
    prompt = (pool.get("system_prompt") or "").strip()
    if not prompt:
        return messages
    msgs = [dict(m) for m in messages]
    if msgs and msgs[0].get("role") == "system":
        msgs[0] = {**msgs[0], "content": prompt + "\n" + msgs[0].get("content", "")}
    else:
        msgs.insert(0, {"role": "system", "content": prompt})
    return msgs


# ---------- квоты ----------

def check_quota(user_id: str, quota_day: int, quota_month: int) -> None:
    if quota_day <= 0 and quota_month <= 0:
        return
    now = int(time.time())
    day_start = now - (now % 86400)
    c = db.get_client()
    used_day = c.execute(
        "SELECT COUNT(*) AS n FROM requests_log WHERE user_id=? AND ts>=?",
        [user_id, day_start]).rows[0]["n"]
    if quota_day > 0 and int(used_day) >= quota_day:
        raise PermissionError("дневная квота исчерпана")
    if quota_month > 0:
        month_start = day_start - 29 * 86400
        used_month = c.execute(
            "SELECT COUNT(*) AS n FROM requests_log WHERE user_id=? AND ts>=?",
            [user_id, month_start]).rows[0]["n"]
        if int(used_month) >= quota_month:
            raise PermissionError("месячная квота исчерпана")


# ---------- сессии (персистентные, в БД) ----------

SESSION_TRIM = 40  # хранить последние N сообщений


def session_get(sid: str) -> dict | None:
    c = db.get_client()
    rs = c.execute("SELECT pool, history, tokens, cost_usd FROM sessions WHERE id=?", [sid])
    if not rs.rows:
        return None
    r = rs.rows[0]
    return {"id": sid, "pool": r["pool"], "history": json.loads(str(r["history"])),
            "tokens": r["tokens"], "cost_usd": r["cost_usd"]}


def session_append(sid: str, pool: str, new_messages: list,
                   tokens: int = 0, cost: float = 0.0) -> dict:
    s = session_get(sid) or {"id": sid, "pool": pool, "history": [],
                             "tokens": 0, "cost_usd": 0.0}
    hist = s["history"] + [dict(m) for m in new_messages]
    hist = hist[-SESSION_TRIM:]
    now = int(time.time())
    c = db.get_client()
    c.execute(
        "INSERT INTO sessions (id, pool, history, tokens, cost_usd, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?)"
        " ON CONFLICT(id) DO UPDATE SET history=excluded.history,"
        " tokens=sessions.tokens+excluded.tokens,"
        " cost_usd=sessions.cost_usd+excluded.cost_usd, updated_at=excluded.updated_at",
        [sid, pool, json.dumps(hist, ensure_ascii=False),
         int(tokens), float(cost), now, now])
    s.update({"history": hist, "tokens": s["tokens"] + int(tokens),
              "cost_usd": s["cost_usd"] + float(cost)})
    return s


# ---------- вызов одного апстрима ----------

_LOCAL_PROVIDERS = {"ollama", "vllm", "lmstudio", "local"}


def _is_local(provider_id: str, base_url: str) -> bool:
    if (provider_id or "").lower() in _LOCAL_PROVIDERS:
        return True
    try:
        host = (urlparse(base_url or "").hostname or "").lower()
    except Exception:
        return False
    if host in ("localhost", "::1") or host.endswith((".local", ".localhost")):
        return True
    try:
        import ipaddress
        ip = ipaddress.ip_address(host)
        return ip.is_loopback or ip.is_private or ip.is_link_local
    except ValueError:
        return False


async def _call_one(provider_id: str, model: str, key: dict, messages: list,
                    timeout_s: float) -> pbase.ProviderResult:
    prov = find_provider(provider_id)
    base_url = (prov or {}).get("base_url", "")
    raw_key = _raw_key(provider_id, key)
    if not raw_key and not _is_local(provider_id, base_url):
        raise pbase.UpstreamError(
            f"не задан ключ апстрима {provider_id} (env {provider_id.upper()}_KEYS)",
            retryable=False, error_class="auth")
    kind = native.provider_kind(provider_id)
    if kind == "anthropic":
        return await native.anthropic_chat(messages, model, raw_key, timeout_s)
    if kind == "gemini":
        return await native.gemini_chat(messages, model, raw_key, timeout_s)
    if provider_id.lower() == "mistral":
        return await native.mistral_chat(messages, model, raw_key, timeout_s)
    return await openai_compat.chat(messages, model, base_url, raw_key, timeout_s)


def _raw_key(provider_id: str, key: dict) -> str:
    # Ключи апстримов живут в env: {PROVIDER}_KEYS через запятую, порядок = порядку в админке.
    env = os.getenv(f"{provider_id.upper()}_KEYS", "") or os.getenv(
        f"{provider_id.upper()}_API_KEY", "")
    candidates = [k.strip() for k in env.split(",") if k.strip()]
    prov = find_provider(provider_id) or {}
    keys = prov.get("keys", [])
    idx = next((i for i, k in enumerate(keys) if k["id"] == key["id"]), 0)
    if candidates and idx < len(candidates):
        return candidates[idx]
    if candidates:
        return candidates[0]
    return ""  # без ключа апстрим ответит 401 — честная ошибка конфига


def _is_length_error(err: pbase.UpstreamError) -> bool:
    t = str(err).lower()
    return "context_length" in t or "maximum context" in t or "too many tokens" in t


def _context_of(model: str) -> int:
    return int(FAMILIES.get(model, {}).get("context", 0) or 0)


# ---------- главный проход ----------

async def complete(pool_id: str, messages: list, user_id: str = "",
                   extra: dict | None = None) -> dict:
    """Полный проход: кэш → пул → fallback → запись. Возвращает OpenAI-подобный dict."""
    t0 = time.monotonic()
    pool = resolve_pool(pool_id)
    settings = STORE["settings"]
    timeout_s = float(settings.get("request_timeout_s", 60))
    messages = with_unlock(pool, messages)
    params = dict(extra or {})

    cache_on = bool(settings.get("enable_cache", True))
    cache_cfg = STORE["cache"]
    ttl = int(cache_cfg.get("ttl_seconds", 3600))

    if cache_on:
        key = exact_cache.exact_key(pool["id"], pool.get("strategy", ""), messages, params)
        hit = db.cache_get(key, pool["id"])
        if hit:
            body = json.loads(hit["response"])
            db.log_request(int(time.time()), user_id, pool["id"], body.get("_provider", ""),
                           body.get("_model", ""), body.get("_pt", 0), body.get("_ct", 0),
                           0.0, int((time.monotonic() - t0) * 1000), True)
            return _openai_response(pool["id"], body["text"], body.get("_model", ""),
                                    body.get("_pt", 0), body.get("_ct", 0), cached=True)
        # Второй уровень: семантика. Только при включённом флаге и живом EMBED_API_KEY.
        threshold = float(cache_cfg.get("similarity_threshold", 0.92))
        if sem_cache.enabled():
            try:
                qtext = "\n".join(str(m.get("content", "")) for m in messages[-3:])
                qvec = (await embed_texts([qtext]))[0]
                sem = await semantic_lookup(pool["id"], qvec, threshold)
                if sem:
                    body = json.loads(sem["response"])
                    db.log_request(int(time.time()), user_id, pool["id"],
                                   body.get("_provider", ""), body.get("_model", ""),
                                   body.get("_pt", 0), body.get("_ct", 0),
                                   0.0, int((time.monotonic() - t0) * 1000), True)
                    return _openai_response(pool["id"], body["text"],
                                            body.get("_model", ""),
                                            body.get("_pt", 0), body.get("_ct", 0),
                                            cached=True)
            except Exception:
                pass  # семантика опциональна — молча идём к апстриму

    members = order_members(pool)
    if not members:
        raise LookupError(f"в пуле '{pool['id']}' нет участников")
    last_err: Exception | None = None
    max_retries = int(settings.get("max_retries", 3))

    for _attempt in range(max(1, max_retries)):
        progressed = False
        tried: list[str] = []  # заново каждую попытку, иначе повторы мертвы
        for provider_id, model in members:
            keys = live_keys(provider_id)
            if not keys:
                continue
            for k in keys:
                tag = f"{provider_id}:{model}:{k['id']}"
                if tag in tried:
                    continue
                tried.append(tag)
                progressed = True
                try:
                    res = await _call_one(provider_id, model, k, messages, timeout_s)
                    record_success(k["id"])
                    _store_cache(pool, messages, params, ttl, provider_id, model, res)
                    ms = int((time.monotonic() - t0) * 1000)
                    db.log_request(int(time.time()), user_id, pool["id"], provider_id, model,
                                   res.prompt_tokens, res.completion_tokens, 0.0, ms, False)
                    return _openai_response(pool["id"], res.text,
                                            res.served_model or model,
                                            res.prompt_tokens, res.completion_tokens)
                except pbase.UpstreamError as e:
                    last_err = e
                    record_failure(k["id"])
                    if not e.retryable:
                        break  # конфиг битый — следующий ключ не поможет
                    if _is_length_error(e):
                        big = sorted(members, key=lambda pm: _context_of(pm[1]),
                                     reverse=True)
                        if big and big[0][1] != model and _context_of(big[0][1]) > _context_of(model):
                            members = big  # context-window-fallback
                    continue
        if not progressed:
            break
    raise last_err or RuntimeError("все апстримы недоступны")


async def complete_stream(pool_id: str, messages: list, user_id: str = ""):
    """Async-генератор SSE-чанков OpenAI-формата. Ретрай только до первого байта."""
    pool = resolve_pool(pool_id)
    settings = STORE["settings"]
    timeout_s = float(settings.get("request_timeout_s", 60))
    messages = with_unlock(pool, messages)
    members = order_members(pool)
    last_err: Exception | None = None
    for provider_id, model in members:
        for k in live_keys(provider_id):
            try:
                prov = find_provider(provider_id) or {}
                if native.provider_kind(provider_id) != "openai_compat" \
                        and provider_id.lower() not in ("mistral",):
                    # нативные без SSE — обычным вызовом одним чанком
                    res = await _call_one(provider_id, model, k, messages, timeout_s)
                    record_success(k["id"])
                    yield {"delta": res.text, "model": res.served_model or model, "done": False}
                    yield {"delta": "", "model": res.served_model or model, "done": True}
                    return
                gen = openai_compat.chat_stream(messages, model, prov.get("base_url", ""),
                                                _raw_key(provider_id, k), timeout_s)
                first = True
                async for ev in gen:
                    if first:
                        first = False
                        record_success(k["id"])  # первый байт — успех
                    yield {"delta": ev.delta, "model": ev.served_model or model,
                           "done": ev.done}
                return
            except pbase.UpstreamError as e:
                last_err = e
                record_failure(k["id"])
                if not e.retryable:
                    break
                continue  # следующий ключ — стрим ещё не начался для клиента
    raise last_err or RuntimeError("все апстримы недоступны")


def _store_cache(pool: dict, messages: list, params: dict, ttl: int,
                 provider_id: str, model: str, res: pbase.ProviderResult) -> None:
    try:
        key = exact_cache.exact_key(pool["id"], pool.get("strategy", ""), messages, params)
        body = json.dumps({"text": res.text, "_provider": provider_id, "_model": model,
                           "_pt": res.prompt_tokens, "_ct": res.completion_tokens},
                          ensure_ascii=False)
        db.cache_put(key, pool["id"], body, None, ttl)
    except Exception:
        pass  # кэш не должен ронять ответ


def _openai_response(pool: str, text: str, model: str, pt: int, ct: int,
                     cached: bool = False) -> dict:
    return {
        "id": f"chatcmpl-{pool}-{int(time.time() * 1000)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model or pool,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": pt, "completion_tokens": ct,
                  "total_tokens": pt + ct},
        "cached": cached,
    }


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """Эмбеддинги для семантического кэша через провайдер из env EMBED_*."""
    base_url = os.getenv("EMBED_BASE_URL", "https://api.openai.com/v1")
    api_key = os.getenv("EMBED_API_KEY", "")
    model = os.getenv("EMBED_MODEL", "text-embedding-3-small")
    if not api_key:
        raise RuntimeError("EMBED_API_KEY не задан — семантический кэш недоступен")
    return await openai_compat.embed(texts, model, base_url, api_key)


async def semantic_lookup(pool_id: str, query_vec: list[float],
                          threshold: float) -> dict | None:
    cands = db.get_embeddings(pool_id)
    return sem_cache.best_match(query_vec, cands, threshold)


async def check_upstream(provider_id: str, key: dict) -> dict:
    """Дешёвая identity-проверка ключа: пробный вызов, сверка served vs claimed."""
    prov = find_provider(provider_id) or {}
    claimed = (prov.get("models") or ["?"])[0]
    t0 = time.monotonic()
    try:
        res = await _call_one(provider_id, claimed, key,
                              [{"role": "user", "content": "ping"}], 20.0)
        ms = int((time.monotonic() - t0) * 1000)
        served = res.served_model or claimed
        return {"alive": True, "latency_ms": ms, "served_id": served,
                "claimed_id": f"{provider_id}:{claimed}", "ceiling": None,
                "error_class": None, "verdict": verdict(f"{provider_id}:{claimed}", served)}
    except pbase.UpstreamError as e:
        ms = int((time.monotonic() - t0) * 1000)
        return {"alive": False, "latency_ms": ms, "served_id": None,
                "claimed_id": f"{provider_id}:{claimed}", "ceiling": None,
                "error_class": e.error_class,
                "verdict": verdict(f"{provider_id}:{claimed}", None)}
