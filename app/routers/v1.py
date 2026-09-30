"""Публичный OpenAI-совместимый API.

- POST /v1/chat/completions — обычный + stream (SSE), model = ID пула
- POST /v1/embeddings — эмбеддинги через EMBED_*
- GET /v1/models — пулы как модели
- POST /v1/sessions, GET /v1/sessions/{sid} — серверные сессии,
  продолжение через session_id в chat (контекст живёт при смене модели)

Авторизация: Bearer sk-relay-... (allowlist CLIENT_API_KEYS, учёт в users).
"""
from __future__ import annotations

import json
import os
import time
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..core import engine
from ..dashboard.store import STORE
from ..db import client as db
from ..providers import base as pbase

router = APIRouter(prefix="/v1")


def require_client(authorization: str | None = Header(default=None)) -> dict:
    raw = ""
    if authorization and authorization.lower().startswith("bearer "):
        raw = authorization[7:].strip()
    if not raw:
        raise HTTPException(401, "Нужен клиентский ключ")
    h = db.hash_key(raw)
    u = db.get_user_by_key_hash(h)
    if u:
        return u
    allowed = [k.strip() for k in os.getenv("CLIENT_API_KEYS", "").split(",") if k.strip()]
    if raw in allowed:
        uid = "u_" + h[:12]
        db.create_user(uid, h)
        return {"id": uid, "quota_day": 1000, "quota_month": 30000}
    raise HTTPException(401, "Неверный клиентский ключ")


class ChatBody(BaseModel):
    model: str = ""
    messages: list = []
    stream: bool = False
    session_id: str | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None


def _extra(body: ChatBody) -> dict:
    return {k: v for k, v in {"temperature": body.temperature, "top_p": body.top_p,
                              "max_tokens": body.max_tokens}.items() if v is not None}


def _quota(user: dict) -> None:
    try:
        engine.check_quota(user["id"], int(user.get("quota_day", 0)),
                           int(user.get("quota_month", 0)))
    except PermissionError as e:
        raise HTTPException(429, str(e))


def _map_error(e: Exception) -> HTTPException:
    if isinstance(e, LookupError):
        return HTTPException(404, str(e))
    if isinstance(e, pbase.UpstreamError):
        return HTTPException(502, f"апстрим: {e}")
    return HTTPException(502, f"все апстримы недоступны: {e}")


@router.get("/models")
def list_models(user: dict = Depends(require_client)):
    pools = [p for p in STORE["pools"] if p.get("enabled", True)]
    return {"object": "list",
            "data": [{"id": p["id"], "object": "model", "owned_by": "bossinrelay"}
                     for p in pools]}


class EmbedBody(BaseModel):
    input: str | list
    model: str = ""


@router.post("/embeddings")
async def embeddings(body: EmbedBody, user: dict = Depends(require_client)):
    _quota(user)
    texts = [body.input] if isinstance(body.input, str) else list(body.input)
    try:
        vecs = await engine.embed_texts([str(t) for t in texts])
    except Exception as e:
        raise HTTPException(502, f"embeddings: {e}")
    return {"object": "list",
            "data": [{"object": "embedding", "index": i, "embedding": v}
                     for i, v in enumerate(vecs)],
            "model": body.model or os.getenv("EMBED_MODEL", ""),
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}


class SessionNew(BaseModel):
    pool: str = ""


@router.post("/sessions")
def new_session(body: SessionNew, user: dict = Depends(require_client)):
    try:
        pool = engine.resolve_pool(body.pool)
    except LookupError as e:
        raise HTTPException(404, str(e))
    sid = "sess_" + uuid.uuid4().hex[:12]
    engine.session_append(sid, pool["id"], [])
    return {"id": sid, "pool": pool["id"]}


@router.get("/sessions/{sid}")
def get_session(sid: str, user: dict = Depends(require_client)):
    s = engine.session_get(sid)
    if not s:
        raise HTTPException(404, "сессия не найдена")
    return s


@router.post("/chat/completions")
async def chat_completions(body: ChatBody, user: dict = Depends(require_client)):
    _quota(user)
    pool_id, history = _history(body)
    if body.stream:
        return StreamingResponse(
            _sse(pool_id, history, user["id"], body.session_id, list(body.messages)),
            media_type="text/event-stream")
    try:
        res = await engine.complete(pool_id, history, user["id"], _extra(body))
    except Exception as e:
        raise _map_error(e)
    _save_turn(body.session_id, list(body.messages), res)
    return res


def _history(body: ChatBody) -> tuple[str, list]:
    if body.session_id:
        s = engine.session_get(body.session_id)
        if not s:
            raise HTTPException(404, "сессия не найдена")
        return s["pool"], s["history"] + list(body.messages)
    if not body.model:
        raise HTTPException(422, "нужен model (ID пула)")
    return body.model, list(body.messages)


def _save_turn(sid: str | None, user_msgs: list, res: dict) -> None:
    if not sid:
        return
    try:
        text = res["choices"][0]["message"]["content"]
        u = res.get("usage", {})
        engine.session_append(sid, res.get("model", ""), list(user_msgs) +
                              [{"role": "assistant", "content": text}],
                              int(u.get("total_tokens", 0)), 0.0)
    except Exception:
        pass  # сессия не должна ронять ответ


async def _sse(pool_id: str, history: list, user_id: str,
               sid: str | None, new_msgs: list):
    ts = int(time.time())
    cid = f"chatcmpl-{pool_id}-{ts * 1000}"
    full: list[str] = []
    model = pool_id
    try:
        async for ch in engine.complete_stream(pool_id, history, user_id):
            model = ch.get("model", model)
            if not ch.get("done"):
                full.append(ch.get("delta", ""))
            chunk = {"id": cid, "object": "chat.completion.chunk", "created": ts,
                     "model": model, "choices": [
                         {"index": 0,
                          "delta": {} if ch.get("done") else {"content": ch.get("delta", "")},
                          "finish_reason": "stop" if ch.get("done") else None}]}
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
    except Exception as e:
        yield ("data: " + json.dumps({"error": {"message": str(e),
                                                "type": "upstream_error"}},
                                     ensure_ascii=False) + "\n\n")
    else:
        if sid and full:
            _save_turn(sid, new_msgs,
                       {"choices": [{"message": {"content": "".join(full)}}],
                        "usage": {}, "model": model})
    yield "data: [DONE]\n\n"
