"""Registry семейств моделей + вердикты identity-проверки (контракт для ядра).

Ядро при health-check ключа делает дешёвый пробный вызов и кладёт рядом
с last_check: served_id (что реально ответил response.model), ceiling
(замеренный output-потолок), error_class (класс ошибки). Вердикт считается
по правилам ниже, дашборд рисует его пилюлей.

Вердикты: matches | likely | wrong-tier | mismatch.
"""
from __future__ import annotations

# потолки/output — ориентировочные, для эвристики wrong-tier, не для биллинга.
FAMILIES: dict[str, dict] = {
    "gpt-4o": {"vendor": "openai", "family": "gpt-4o", "context": 128_000,
               "output_ceiling": 16_384, "cutoff": "2023-10"},
    "gpt-4o-mini": {"vendor": "openai", "family": "gpt-4o", "context": 128_000,
                    "output_ceiling": 16_384, "cutoff": "2023-10"},
    "claude-sonnet-4-6": {"vendor": "anthropic", "family": "claude-sonnet", "context": 200_000,
                          "output_ceiling": 8192, "cutoff": "2025-01"},
    "claude-haiku-4-5": {"vendor": "anthropic", "family": "claude-haiku", "context": 200_000,
                         "output_ceiling": 8192, "cutoff": "2025-01"},
}

_CHEAP_MARKERS = ("mini", "haiku", "flash", "nano", "lite")


def _tier(model_id: str) -> str:
    m = (model_id or "").lower()
    return "cheap" if any(t in m for t in _CHEAP_MARKERS) else "full"


def classify_error(text: str) -> str:
    """Выловить класс ошибки + id модели из текста (relabel часто палится там)."""
    t = (text or "").lower()
    if "insufficient_quota" in t or "quota" in t or "billing" in t:
        return "quota"
    if "invalid_api_key" in t or "unauthorized" in t or "401" in t:
        return "auth"
    if "not_found" in t or "does not exist" in t or "404" in t:
        return "not_found"
    if "rate_limit" in t or "429" in t or "retry" in t:
        return "rate_limited"
    return "server" if t else "unknown"


def verdict(claimed_id: str, served_id: str | None) -> str:
    claimed, served = (claimed_id or ""), (served_id or "")
    if not served:
        return "mismatch"
    if served == claimed:
        return "matches"
    if served.split("-")[0] == claimed.split("-")[0] or served.split(":")[-1] == claimed:
        return "likely"
    if _tier(served) != _tier(claimed):
        return "wrong-tier"
    return "mismatch"
