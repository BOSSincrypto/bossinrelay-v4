"""Нативные провайдеры с отличающимся форматом: Anthropic, Gemini, Mistral.

Остальные (OpenRouter, DeepSeek, Groq, Ollama, vLLM, LM Studio) идут
через openai_compat с другим base_url — дублировать код незачем.
"""
from __future__ import annotations

import json

import httpx

from . import openai_compat
from .base import ProviderResult, UpstreamError, classify_status


def _raise(resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    retryable, cls = classify_status(resp.status_code)
    try:
        msg = json.dumps(resp.json(), ensure_ascii=False)[:500]
    except Exception:
        msg = resp.text[:500]
    raise UpstreamError(f"upstream {resp.status_code}: {msg}", status=resp.status_code,
                        retryable=retryable, error_class=cls)


def _split_system(messages: list) -> tuple[str, list]:
    system, rest = [], []
    for m in messages:
        (system if m.get("role") == "system" else rest).append(m)
    sys_text = "\n".join(m.get("content", "") for m in system if isinstance(m, dict))
    return sys_text, rest


async def anthropic_chat(messages: list, model: str, api_key: str,
                         timeout_s: float = 60.0) -> ProviderResult:
    url = "https://api.anthropic.com/v1/messages"
    system, rest = _split_system(messages)
    body: dict = {"model": model, "max_tokens": 4096,
                  "messages": [{"role": m["role"], "content": m.get("content", "")}
                               for m in rest if m.get("role") in ("user", "assistant")]}
    if system:
        body["system"] = system
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01",
               "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=False) as c:
        try:
            resp = await c.post(url, headers=headers, json=body)
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise UpstreamError(f"сеть/таймаут: {e}", retryable=True, error_class="server")
        _raise(resp)
        data = resp.json()
    try:
        text = "".join(b.get("text", "") for b in data.get("content", [])
                       if b.get("type") == "text")
        usage = data.get("usage", {})
    except (AttributeError, TypeError):
        raise UpstreamError("кривой JSON от Anthropic", retryable=False, error_class="server")
    return ProviderResult(text=text, served_model=data.get("model"),
                          prompt_tokens=int(usage.get("input_tokens", 0)),
                          completion_tokens=int(usage.get("completion_tokens", 0)))


async def gemini_chat(messages: list, model: str, api_key: str,
                      timeout_s: float = 60.0) -> ProviderResult:
    sys_text, rest = _split_system(messages)
    contents = [{"role": "user" if m.get("role") == "user" else "model",
                 "parts": [{"text": m.get("content", "")}]} for m in rest]
    body: dict = {"contents": contents}
    if sys_text:
        body["systemInstruction"] = {"parts": [{"text": sys_text}]}
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
           f":generateContent?key={api_key}")
    async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=False) as c:
        try:
            resp = await c.post(url, json=body)
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise UpstreamError(f"сеть/таймаут: {e}", retryable=True, error_class="server")
        _raise(resp)
        data = resp.json()
    try:
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts)
        meta = data.get("usageMetadata", {})
    except (KeyError, IndexError, TypeError):
        raise UpstreamError("кривой JSON от Gemini", retryable=False, error_class="server")
    return ProviderResult(text=text, served_model=model,
                          prompt_tokens=int(meta.get("promptTokenCount", 0)),
                          completion_tokens=int(meta.get("candidatesTokenCount", 0)))


async def mistral_chat(messages: list, model: str, api_key: str,
                       timeout_s: float = 60.0) -> ProviderResult:
    # Mistral говорит на OpenAI-диалекте — делегируем совместимому клиенту.
    return await openai_compat.chat(messages, model, "https://api.mistral.ai/v1",
                                    api_key, timeout_s)


def provider_kind(provider_id: str) -> str:
    pid = (provider_id or "").lower()
    if pid == "anthropic":
        return "anthropic"
    if pid in ("gemini", "google"):
        return "gemini"
    return "openai_compat"
