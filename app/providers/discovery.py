"""Обнаружение моделей провайдера по API-ключу (поток как в truemodel).

Пользователь вставляет ключ, жмёт «Обнаружить» — идём в каталог
провайдера и возвращаем список id моделей. Ключ нигде не сохраняется,
только используется для этого запроса. Нет каталога у провайдера —
возвращаем пресетный lineup с пометкой fallback.
"""
from __future__ import annotations

import os

import httpx

from ..dashboard.ssrf import validate_base_url
from . import openai_compat

_TIMEOUT_S = 20.0

# ponytail: пресетный lineup на случай, если каталог недоступен.
_FALLBACK: dict[str, list[str]] = {
    "openai": ["gpt-4o", "gpt-4o-mini"],
    "anthropic": ["claude-sonnet-4-6", "claude-haiku-4-5"],
    "gemini": ["gemini-2.0-flash", "gemini-1.5-pro"],
    "google": ["gemini-2.0-flash", "gemini-1.5-pro"],
    "mistral": ["mistral-large-latest", "mistral-small-latest"],
    "deepseek": ["deepseek-chat", "deepseek-reasoner"],
    "groq": ["llama-3.3-70b-versatile", "mixtral-8x7b-32768"],
    "openrouter": ["openai/gpt-4o-mini", "anthropic/claude-haiku-4-5"],
    "ollama": ["llama3.1", "mistral"],
    "vllm": ["meta-llama-3.1-8b"],
    "lmstudio": ["local-model"],
}


def _allow_private() -> bool:
    return os.getenv("ALLOW_PRIVATE_URLS", "false").lower() in ("1", "true", "yes")


async def _openai_models(base_url: str, api_key: str) -> list[str]:
    url = validate_base_url(base_url, _allow_private()) + "/models"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    async with httpx.AsyncClient(timeout=_TIMEOUT_S, follow_redirects=False) as c:
        resp = await c.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    items = data.get("data", []) if isinstance(data, dict) else []
    return [str(x.get("id", "")) for x in items if isinstance(x, dict) and x.get("id")]


async def _anthropic_models(api_key: str) -> list[str]:
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    async with httpx.AsyncClient(timeout=_TIMEOUT_S, follow_redirects=False) as c:
        resp = await c.get("https://api.anthropic.com/v1/models", headers=headers)
        resp.raise_for_status()
        data = resp.json()
    items = data.get("data", []) if isinstance(data, dict) else []
    return [str(x.get("id", "")) for x in items if isinstance(x, dict) and x.get("id")]


async def _gemini_models(api_key: str) -> list[str]:
    url = ("https://generativelanguage.googleapis.com/v1beta/models"
           f"?key={api_key}&pageSize=100")
    async with httpx.AsyncClient(timeout=_TIMEOUT_S, follow_redirects=False) as c:
        resp = await c.get(url)
        resp.raise_for_status()
        data = resp.json()
    items = data.get("models", []) if isinstance(data, dict) else []
    out = []
    for x in items:
        name = str(x.get("name", "") or "")
        if name.startswith("models/"):
            name = name[len("models/"):]
        if name:
            out.append(name)
    return out


async def list_models(provider_id: str, base_url: str, api_key: str) -> dict:
    """Вернуть {models, source, note}. source: live | fallback."""
    pid = (provider_id or "").lower()
    try:
        if pid == "anthropic":
            models = await _anthropic_models(api_key)
        elif pid in ("gemini", "google"):
            models = await _gemini_models(api_key)
        else:
            base = base_url or "https://api.openai.com/v1"
            if pid == "mistral" and not base_url:
                base = "https://api.mistral.ai/v1"
            models = await _openai_models(base, api_key)
        models = [m for m in models if m]
        if models:
            return {"models": sorted(set(models)), "source": "live",
                    "note": f"Каталог провайдера отдал {len(models)} шт."}
    except Exception as e:
        note = f"Каталог недоступен ({type(e).__name__}), показан пресет."
        fb = _FALLBACK.get(pid, _FALLBACK["openai"])
        return {"models": fb, "source": "fallback", "note": note}
    fb = _FALLBACK.get(pid, _FALLBACK["openai"])
    return {"models": fb, "source": "fallback",
            "note": "Каталог пуст — показан пресетный lineup."}


async def check_model(provider_id: str, model: str, base_url: str,
                      api_key: str) -> dict:
    """Дешёвый live-ping одной модели: минимальный chat-запрос."""
    import time

    from ..dashboard.registry import verdict as _verdict
    from . import native

    t0 = time.monotonic()
    try:
        pid = (provider_id or "").lower()
        if pid == "anthropic":
            res = await native.anthropic_chat(
                [{"role": "user", "content": "ping"}], model, api_key, _TIMEOUT_S)
        elif pid in ("gemini", "google"):
            res = await native.gemini_chat(
                [{"role": "user", "content": "ping"}], model, api_key, _TIMEOUT_S)
        else:
            base = base_url or "https://api.openai.com/v1"
            if pid == "mistral" and not base_url:
                base = "https://api.mistral.ai/v1"
            res = await openai_compat.chat(
                [{"role": "user", "content": "ping"}], model, base, api_key,
                _TIMEOUT_S)
        ms = int((time.monotonic() - t0) * 1000)
        served = res.served_model or model
        return {"alive": True, "latency_ms": ms, "served_id": served,
                "claimed_id": f"{provider_id}:{model}",
                "verdict": _verdict(f"{provider_id}:{model}", served)}
    except Exception as e:
        ms = int((time.monotonic() - t0) * 1000)
        return {"alive": False, "latency_ms": ms, "served_id": None,
                "claimed_id": f"{provider_id}:{model}", "error": str(e)[:200]}
