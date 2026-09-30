"""OpenAI-совместимый провайдер: OpenAI, OpenRouter, DeepSeek, Groq,
Ollama, vLLM, LM Studio — все через /chat/completions. Редиректы — через SSRF-guard."""
from __future__ import annotations

import json
import os

import httpx

from ..dashboard.ssrf import validate_base_url
from .base import ProviderResult, StreamEvent, UpstreamError, classify_status


def _headers(api_key: str) -> dict:
    h = {"Content-Type": "application/json"}
    if api_key:  # локальные модели (Ollama/vLLM) часто без ключа — шлём без заголовка
        h["Authorization"] = f"Bearer {api_key}"
    return h


def _raise_for_status(resp: httpx.Response, read_body: bool = True) -> None:
    if resp.status_code < 400:
        return
    retryable, cls = classify_status(resp.status_code)
    retry_after = None
    if resp.status_code == 429:
        try:
            retry_after = int(resp.headers.get("retry-after", "0")) or None
        except ValueError:
            retry_after = None
    if read_body:
        try:
            detail = resp.json()
            msg = json.dumps(detail, ensure_ascii=False)[:500]
        except Exception:
            try:
                msg = resp.text[:500]
            except Exception:
                msg = ""
    else:
        msg = ""  # стрим: тело не читаем, иначе ломаем итерацию
    raise UpstreamError(f"upstream {resp.status_code}: {msg}", status=resp.status_code,
                        retryable=retryable, error_class=cls, retry_after=retry_after)


async def chat(messages: list, model: str, base_url: str, api_key: str,
               timeout_s: float = 60.0, extra: dict | None = None) -> ProviderResult:
    url = validate_base_url(base_url, _allow_private()) + "/chat/completions"
    body = {"model": model, "messages": messages, **(extra or {})}
    async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=False) as c:
        try:
            resp = await c.post(url, headers=_headers(api_key), json=body)
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise UpstreamError(f"сеть/таймаут: {e}", retryable=True, error_class="server")
        _raise_for_status(resp)
        data = resp.json()
    try:
        choice = data["choices"][0]["message"]
        usage = data.get("usage", {})
    except (KeyError, IndexError, TypeError):
        raise UpstreamError("кривой JSON от апстрима", retryable=False, error_class="server")
    return ProviderResult(
        text=choice.get("content") or "",
        served_model=data.get("model"),
        prompt_tokens=int(usage.get("prompt_tokens", 0)),
        completion_tokens=int(usage.get("completion_tokens", 0)),
    )


async def chat_stream(messages: list, model: str, base_url: str, api_key: str,
                      timeout_s: float = 60.0,
                      extra: dict | None = None):  # async-генератор StreamEvent
    url = validate_base_url(base_url, _allow_private()) + "/chat/completions"
    body = {"model": model, "messages": messages, "stream": True, **(extra or {})}
    async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=False) as c:
        try:
            async with c.stream("POST", url, headers=_headers(api_key), json=body) as resp:
                _raise_for_status(resp, read_body=False)  # ошибка ДО первого байта
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        yield StreamEvent(done=True)
                        return
                    try:
                        chunk = json.loads(payload)
                    except Exception:
                        continue
                    delta = ""
                    try:
                        delta = chunk["choices"][0]["delta"].get("content") or ""
                    except (KeyError, IndexError, TypeError):
                        pass
                    yield StreamEvent(delta=delta, served_model=chunk.get("model"))
        except UpstreamError:
            raise
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise UpstreamError(f"сеть/таймаут: {e}", retryable=True, error_class="server")


async def embed(texts: list[str], model: str, base_url: str, api_key: str,
                timeout_s: float = 30.0) -> list[list[float]]:
    url = validate_base_url(base_url, _allow_private()) + "/embeddings"
    async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=False) as c:
        try:
            resp = await c.post(url, headers=_headers(api_key),
                                json={"model": model, "input": texts})
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise UpstreamError(f"сеть/таймаут: {e}", retryable=True, error_class="server")
        _raise_for_status(resp)
        data = resp.json()
    try:
        return [d["embedding"] for d in data["data"]]
    except (KeyError, TypeError):
        raise UpstreamError("кривой JSON embeddings", retryable=False, error_class="server")


def _allow_private() -> bool:
    return os.getenv("ALLOW_PRIVATE_URLS", "false").lower() in ("1", "true", "yes")
