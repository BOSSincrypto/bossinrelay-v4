"""Точный кэш: SHA256 нормализованного запроса. Первый уровень, дешёвый."""
from __future__ import annotations

import hashlib
import json


def exact_key(pool: str, model: str, messages: list, params: dict) -> str:
    norm = json.dumps(
        {"pool": pool, "model": model, "messages": messages, "params": params},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()
