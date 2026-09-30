"""Семантический кэш: косинусная близость на numpy, без torch/faiss.

Второй уровень после точного. Полностью отключается флагом
SEMANTIC_CACHE_ENABLED=false — тогда numpy даже не импортируется.
"""
from __future__ import annotations

import os

_np = None


def enabled() -> bool:
    return os.getenv("SEMANTIC_CACHE_ENABLED", "true").lower() not in ("0", "false", "no")


def _numpy():
    global _np
    if _np is None:
        import numpy as np  # ленивый импорт: экономия RAM при выключенной семантике
        _np = np
    return _np


def to_blob(vec: list[float]) -> bytes:
    np = _numpy()
    return np.asarray(vec, dtype="float32").tobytes()


def from_blob(blob: bytes) -> list[float]:
    np = _numpy()
    return np.frombuffer(blob, dtype="float32").tolist()


def best_match(query: list[float], candidates: list[dict], threshold: float) -> dict | None:
    """candidates: [{prompt_hash, response, embedding(blob)}]. Вернуть лучший хит или None."""
    if not query or not candidates:
        return None
    np = _numpy()
    q = np.asarray(query, dtype="float32")
    qn = float(np.linalg.norm(q))
    if not qn:
        return None
    best = None
    best_score = float(threshold)
    for c in candidates:
        try:
            v = np.frombuffer(bytes(c["embedding"]), dtype="float32")
        except Exception:
            continue
        if v.shape != q.shape:
            continue
        vn = float(np.linalg.norm(v))
        if not vn:
            continue
        score = float(np.dot(q, v) / (qn * vn))
        if score > best_score:
            best_score = score
            best = c
    if best is None:
        return None
    return {"response": best["response"], "score": best_score,
            "prompt_hash": best["prompt_hash"]}
