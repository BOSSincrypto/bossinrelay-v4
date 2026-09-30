"""In-memory хранилище-ЗАГЛУШКА для админки.

Ядра пока нет (app/core, app/db, app/routers не созданы), поэтому формы
дергают /admin/* API, а этот модуль отдаёт данные из памяти с seed-данными.
Когда появится настоящее ядро — заменить содержимое STORE вызовами ядра,
контракт эндпоинтов при этом не меняется.
"""
from __future__ import annotations

import itertools
import time

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
