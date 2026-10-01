"""In-memory хранилище-ЗАГЛУШКА для админки.

Ядра пока нет (app/core, app/db, app/routers не созданы), поэтому формы
дергают /admin/* API, а этот модуль отдаёт данные из памяти с seed-данными.
Когда появится настоящее ядро — заменить содержимое STORE вызовами ядра,
контракт эндпоинтов при этом не меняется.
"""
from __future__ import annotations

import itertools
import time

from ..db import client as db_client

_ids = itertools.count(100)


def nid(prefix: str) -> str:
    return f"{prefix}_{next(_ids)}"


def now() -> int:
    return int(time.time())


STORE: dict = {
    "providers": [
        {
            "id": "openai",
            "name": "OpenAI",
            "base_url": "https://api.openai.com/v1",
            "models": ["gpt-4o", "gpt-4o-mini"],
            "enabled": True,
            "keys": [
                {"id": "key_1", "masked": "sk-...a1b2", "note": "основной",
                 "enabled": True, "last_check": {"alive": True, "latency_ms": 210}},
            ],
        },
        {
            "id": "anthropic",
            "name": "Anthropic",
            "base_url": "https://api.anthropic.com",
            "models": ["claude-sonnet-4-6", "claude-haiku-4-5"],
            "enabled": True,
            "keys": [
                {"id": "key_2", "masked": "sk-ant-...9f3c", "note": "резерв",
                 "enabled": True, "last_check": {"alive": True, "latency_ms": 340}},
            ],
        },
    ],
    "pools": [
        {
            "id": "chat-main",
            "strategy": "cascade",
            "members": ["openai:gpt-4o-mini", "anthropic:claude-haiku-4-5"],
            "unlock_on": "",
            "system_prompt": "Отвечай кратко и по-русски.",
            "enabled": True,
        },
        {
            "id": "code-pro",
            "strategy": "cheapest",
            "members": ["openai:gpt-4o", "anthropic:claude-sonnet-4-6"],
            "unlock_on": "премиум-тариф",
            "system_prompt": "Ты senior-разработчик. Код без лишних слов.",
            "enabled": True,
        },
    ],
    "cache": {
        "ttl_seconds": 3600,
        "similarity_threshold": 0.92,
        "max_entries": 10000,
        "entries": 8420,
        "hit_rate": 0.63,
    },
    "sessions": [
        {
            "id": "sess_1",
            "started_at": "30.09.2026 09:12",
            "pool": "chat-main",
            "tokens": 1840,
            "cost_usd": 0.0041,
            "messages": [
                {"role": "user", "text": "Привет! Что ты умеешь?"},
                {"role": "assistant", "text": "Привет! Отвечаю на вопросы, помогаю с кодом и текстами."},
                {"role": "user", "text": "Напиши функцию суммы на Python"},
                {"role": "assistant", "text": "def add(a, b):\n    return a + b"},
            ],
        },
        {
            "id": "sess_2",
            "started_at": "30.09.2026 08:47",
            "pool": "code-pro",
            "tokens": 5230,
            "cost_usd": 0.0318,
            "messages": [
                {"role": "user", "text": "Разбери этот трейсбек: KeyError: 'user_id'"},
                {"role": "assistant", "text": "Ключ user_id отсутствует в словаре — проверьте, откуда он должен прийти..."},
            ],
        },
    ],
    "quotas": {
        "openai:gpt-4o": {"limit_day": 5000, "limit_month": 100000, "used_day": 312, "used_month": 5400},
        "openai:gpt-4o-mini": {"limit_day": 50000, "limit_month": 1000000, "used_day": 8210, "used_month": 120400},
        "anthropic:claude-haiku-4-5": {"limit_day": 30000, "limit_month": 500000, "used_day": 1930, "used_month": 41200},
    },
    "settings": {
        "default_pool": "chat-main",
        "request_timeout_s": 60,
        "max_retries": 3,
        "log_level": "info",
        "enable_cache": True,
        "expose_metrics": True,
        "rate_limit_per_min": 600,
        "rate_limit_per_key_min": 120,
    },
}


def mask_key(raw: str) -> str:
    raw = (raw or "").strip()
    if len(raw) <= 8:
        return "***"
    return f"{raw[:3]}-...{raw[-4:]}"


def find_provider(pid: str) -> dict | None:
    return next((p for p in STORE["providers"] if p["id"] == pid), None)


def find_key(kid: str) -> tuple[dict | None, dict | None]:
    for p in STORE["providers"]:
        for k in p["keys"]:
            if k["id"] == kid:
                return p, k
    return None, None


def find_pool(pid: str) -> dict | None:
    return next((p for p in STORE["pools"] if p["id"] == pid), None)


# ---------- truemodel-lite: реестр семейств (dashboard-видимая часть) ----------
# SSRF-guard, redact-трейс на запись и per-key rate-limit живут в ядре
# (app/core, app/providers — вне скоупа дашборда); здесь только справочник
# и seed-вердикты дешёвой identity-проверки для отображения пилюлями.
MODEL_REGISTRY: dict = {
    "openai:gpt-4o": {
        "vendor": "OpenAI", "family": "GPT-4o",
        "context": 128000, "output_ceiling": 16384, "cutoff": "2023-10",
        "verdict": "matches",
        "note": "response.model совпал с заявленным id.",
    },
    "openai:gpt-4o-mini": {
        "vendor": "OpenAI", "family": "GPT-4o mini",
        "context": 128000, "output_ceiling": 16384, "cutoff": "2023-10",
        "verdict": "likely",
        "note": "Семейство совпало, точный id подтвердить не удалось.",
    },
    "anthropic:claude-sonnet-4-6": {
        "vendor": "Anthropic", "family": "Claude Sonnet 4",
        "context": 200000, "output_ceiling": 8192, "cutoff": "2025-03",
        "verdict": "matches",
        "note": "response.model совпал с заявленным id.",
    },
    "anthropic:claude-haiku-4-5": {
        "vendor": "Anthropic", "family": "Claude Haiku 4",
        "context": 200000, "output_ceiling": 8192, "cutoff": "2025-03",
        "verdict": "likely",
        "note": "Семейство совпало, точный id подтвердить не удалось.",
    },
}

VERDICT_LABELS = {
    "matches": "совпала",
    "likely": "похожа",
    "wrong-tier": "не тот тир",
    "mismatch": "подмена",
}


# ---------- персист конфига в libsql ----------
# STORE остаётся источником правды в памяти; БД — переживает рестарт.
# Сырые ключи НЕ сохраняем: в provider_keys лежит только masked.

_CACHE_SETTING_KEYS = {
    "ttl_seconds": "cache.ttl_seconds",
    "similarity_threshold": "cache.similarity_threshold",
    "max_entries": "cache.max_entries",
}

_loaded = False


def load_from_db() -> bool:
    """Залить конфиг из БД в STORE. True — в БД были данные,
    False — БД пуста (нужен bootstrap из seed)."""
    cfg = db_client.load_config()
    if not cfg["providers"] and not cfg["pools"]:
        return False
    STORE["providers"][:] = cfg["providers"]
    STORE["pools"][:] = cfg["pools"]
    for key in [k for k in STORE["quotas"] if k not in cfg["quotas"]]:
        del STORE["quotas"][key]
    for key, lim in cfg["quotas"].items():
        q = STORE["quotas"].setdefault(key, {"used_day": 0, "used_month": 0})
        q["limit_day"] = lim["limit_day"]
        q["limit_month"] = lim["limit_month"]
    rev_cache = {v: k for k, v in _CACHE_SETTING_KEYS.items()}
    for k, v in cfg["settings"].items():
        if k in rev_cache:
            STORE["cache"][rev_cache[k]] = v
        else:
            STORE["settings"][k] = v
    for mk, vd in cfg["verdicts"].items():
        if mk in MODEL_REGISTRY:
            if vd["verdict"]:
                MODEL_REGISTRY[mk]["verdict"] = vd["verdict"]
            if vd["note"]:
                MODEL_REGISTRY[mk]["note"] = vd["note"]
    return True


def persist() -> None:
    """Сохранить весь STORE в БД (идемпотентно). Best-effort: при
    недоступности БД молча пропускаем — STORE остаётся источником правды."""
    try:
        cfg = db_client.load_config()
        for p in STORE["providers"]:
            db_client.save_provider(
                p["id"], p.get("name", ""), p.get("base_url", ""),
                p.get("enabled", True), p.get("models", []))
            for k in p.get("keys", []):
                db_client.save_key(
                    k["id"], p["id"], k.get("masked", ""), k.get("note", ""),
                    k.get("enabled", True), k.get("last_check"))
        keep_keys = {k["id"] for p in STORE["providers"]
                     for k in p.get("keys", [])}
        for rp in cfg["providers"]:
            if rp["id"] not in {p["id"] for p in STORE["providers"]}:
                db_client.delete_provider_cfg(rp["id"])
            else:
                for k in rp.get("keys", []):
                    if k["id"] not in keep_keys:
                        db_client.delete_key_cfg(k["id"])
        for pool in STORE["pools"]:
            db_client.save_pool(
                pool["id"], pool.get("strategy", "cascade"),
                pool.get("members", []), pool.get("unlock_on", ""),
                pool.get("system_prompt", ""), pool.get("enabled", True))
        for rp in cfg["pools"]:
            if rp["id"] not in {p["id"] for p in STORE["pools"]}:
                db_client.delete_pool_cfg(rp["id"])
        for key, q in STORE["quotas"].items():
            db_client.save_quota(key, q["limit_day"], q["limit_month"])
        for key in [k for k in cfg["quotas"] if k not in STORE["quotas"]]:
            db_client.delete_quota_cfg(key)
        for k, v in STORE["settings"].items():
            db_client.save_setting(k, v)
        for ck, sk in _CACHE_SETTING_KEYS.items():
            db_client.save_setting(sk, STORE["cache"][ck])
        for mk, e in MODEL_REGISTRY.items():
            db_client.save_verdict(mk, verdict=e.get("verdict", ""),
                                   note=e.get("note", ""))
    except Exception:
        pass


def ensure_loaded() -> None:
    """При старте: забрать конфиг из БД; при пустой БД — seed STORE в БД."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        if not load_from_db():
            persist()  # bootstrap
    except Exception:
        pass


ensure_loaded()
