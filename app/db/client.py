"""libsql слой: embedded файл по умолчанию, Turso remote при обеих переменных.

- Дефолт: локальный файл LIBSQL_DB_PATH (./data/relay.db), работает без сети.
- При заданных LIBSQL_URL + LIBSQL_AUTH_TOKEN: клиент ходит напрямую в Turso
  (libsql:// конвертируется в https://), данные живут в облаке.
- Один глобальный ClientSync + threading.Lock (один процесс, один воркер).
- requests_log пишется батчами (~50 строк / один batch()), плюс сброс
  по возрасту (старшая запись старше 60с), чтобы хвост не терялся.
"""
from __future__ import annotations

import hashlib
import os
import threading
import time

from libsql_client import create_client_sync

from .schema import DDL, INDEXES

_BATCH_SIZE = 50
_FLUSH_AGE_S = 60.0

_lock = threading.Lock()
_client = None
_pending: list[tuple] = []  # строки requests_log, ждут batch-записи


def _db_path() -> str:
    return os.getenv("LIBSQL_DB_PATH", "./data/relay.db")


def get_client():
    global _client
    if _client is None or _client.closed:
        url = os.getenv("LIBSQL_URL", "").strip()
        token = os.getenv("LIBSQL_AUTH_TOKEN", "").strip()
        if url and token:
            remote = url.replace("libsql://", "https://").rstrip("/")
            _client = create_client_sync(remote, auth_token=token)
        else:
            path = os.path.abspath(_db_path())
            os.makedirs(os.path.dirname(path), exist_ok=True)
            _client = create_client_sync(f"file:{path}")
    return _client


def init_db() -> bool:
    """Создать таблицы/индексы. Вызывается лениво при первом обращении."""
    c = get_client()
    with _lock:
        for chunk in (DDL + INDEXES).split(";"):
            stmt = chunk.strip()
            if stmt:
                c.execute(stmt)
    return True


def _ensure() -> object:
    c = get_client()
    try:
        c.execute("SELECT 1")
    except Exception:
        global _client
        _client = None
        c = get_client()
    # таблицы могут отсутствовать (первый старт) — init идемпотентен
    init_db()
    return c


def hash_key(raw: str) -> str:
    return hashlib.sha256((raw or "").encode()).hexdigest()


# ---------- requests_log: батчинг ----------

_LOG_COLS = ("ts", "user_id", "pool", "provider", "model", "prompt_tokens",
             "completion_tokens", "cost_usd", "latency_ms", "cache_hit")


def log_request(ts: int, user_id: str = "", pool: str = "", provider: str = "",
                model: str = "", prompt_tokens: int = 0, completion_tokens: int = 0,
                cost_usd: float = 0.0, latency_ms: int = 0, cache_hit: bool = False) -> None:
    """Накопить строку лога; сброс батчем при ~50 строках или возрасте 60с."""
    global _pending
    row = (int(ts), user_id, pool, provider, model, int(prompt_tokens),
           int(completion_tokens), float(cost_usd), int(latency_ms), 1 if cache_hit else 0)
    flush = False
    with _lock:
        _pending.append(row)
        if len(_pending) >= _BATCH_SIZE or (len(_pending) > 1 and time.time() - _pending[0][0] > _FLUSH_AGE_S):
            flush = True
    if flush:
        flush_log()


def flush_log() -> int:
    """Сбросить накопленное одним batch(). Вернуть число записанных строк."""
    global _pending
    with _lock:
        batch, _pending = _pending, []
    if not batch:
        return 0
    c = _ensure()
    placeholders = ",".join(["?"] * len(_LOG_COLS))
    sql = f"INSERT INTO requests_log ({','.join(_LOG_COLS)}) VALUES ({placeholders})"
    with _lock:
        c.batch([(sql, list(r)) for r in batch])
    return len(batch)


# ---------- cache ----------

def cache_get(prompt_hash: str, pool: str) -> dict | None:
    c = _ensure()
    now = int(time.time())
    with _lock:
        rs = c.execute(
            "SELECT response, expires_at FROM cache_entries WHERE prompt_hash=? AND pool=?",
            [prompt_hash, pool])
    if not rs.rows:
        return None
    row = rs.rows[0]
    if int(row["expires_at"]) < now:
        with _lock:
            c.execute("DELETE FROM cache_entries WHERE prompt_hash=? AND pool=?",
                      [prompt_hash, pool])
        return None
    return {"response": str(row["response"])}


def cache_put(prompt_hash: str, pool: str, response: str,
              embedding: bytes | None = None, ttl_seconds: int = 3600) -> None:
    c = _ensure()
    now = int(time.time())
    max_entries = int(os.getenv("CACHE_MAX_ENTRIES", "10000"))
    with _lock:
        c.execute("DELETE FROM cache_entries WHERE expires_at < ?", [now])
        c.execute(
            "INSERT OR REPLACE INTO cache_entries "
            "(prompt_hash, pool, response, embedding, created_at, expires_at) "
            "VALUES (?,?,?,?,?,?)",
            [prompt_hash, pool, response, embedding, now, now + int(ttl_seconds)])
        rs = c.execute("SELECT COUNT(*) AS n FROM cache_entries")
        over = int(rs.rows[0]["n"]) - max_entries
        if over > 0:
            c.execute(
                "DELETE FROM cache_entries WHERE rowid IN "
                "(SELECT rowid FROM cache_entries ORDER BY created_at ASC LIMIT ?)",
                [over])


def cache_clear(pool: str | None = None) -> int:
    c = _ensure()
    with _lock:
        if pool:
            rs = c.execute("DELETE FROM cache_entries WHERE pool=?", [pool])
        else:
            rs = c.execute("DELETE FROM cache_entries")
    return rs.rows_affected


def get_embeddings(pool: str, limit: int = 2000) -> list[dict]:
    """Свежие (непротухшие) эмбеддинги пула для семантического поиска."""
    c = _ensure()
    now = int(time.time())
    with _lock:
        rs = c.execute(
            "SELECT prompt_hash, response, embedding FROM cache_entries "
            "WHERE pool=? AND embedding IS NOT NULL AND expires_at > ? "
            "ORDER BY created_at DESC LIMIT ?",
            [pool, now, int(limit)])
    return [{"prompt_hash": r["prompt_hash"], "response": r["response"],
             "embedding": bytes(r["embedding"])} for r in rs.rows]


# ---------- users (клиентские ключи sk-relay-...) ----------

def get_user_by_key_hash(api_key_hash: str) -> dict | None:
    c = _ensure()
    with _lock:
        rs = c.execute(
            "SELECT id, quota_day, quota_month FROM users WHERE api_key_hash=?",
            [api_key_hash])
    if not rs.rows:
        return None
    r = rs.rows[0]
    return {"id": r["id"], "quota_day": r["quota_day"], "quota_month": r["quota_month"]}


def create_user(user_id: str, api_key_hash: str,
                quota_day: int = 1000, quota_month: int = 30000) -> None:
    c = _ensure()
    with _lock:
        c.execute(
            "INSERT OR IGNORE INTO users (id, api_key_hash, quota_day, quota_month, created_at)"
            " VALUES (?,?,?,?,?)",
            [user_id, api_key_hash, quota_day, quota_month, int(time.time())])


# ---------- stats (заготовка для /admin/stats) ----------

def get_usage_stats() -> dict:
    """Агрегаты из requests_log. Админка пока на STORE — подключится позже."""
    c = _ensure()
    now = int(time.time())
    day_start = now - (now % 86400)
    month_start = day_start - 29 * 86400
    with _lock:
        total = c.execute("SELECT COUNT(*) AS n FROM requests_log").rows[0]["n"]
        today = c.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(cost_usd),0) AS usd,"
            " COALESCE(AVG(latency_ms),0) AS lat FROM requests_log WHERE ts>=?",
            [day_start]).rows[0]
        month = c.execute(
            "SELECT COALESCE(SUM(cost_usd),0) AS usd FROM requests_log WHERE ts>=?",
            [month_start]).rows[0]
        hits = c.execute(
            "SELECT COUNT(*) AS n FROM requests_log WHERE ts>=? AND cache_hit=1",
            [day_start]).rows[0]
        by_pool = c.execute(
            "SELECT pool, COUNT(*) AS n, COALESCE(SUM(cost_usd),0) AS usd"
            " FROM requests_log WHERE ts>=? GROUP BY pool", [day_start]).rows
    return {
        "requests_total": int(total or 0),
        "requests_today": int(today["n"] or 0),
        "cache_hit_rate": (float(hits["n"]) / float(today["n"])) if today["n"] else 0.0,
        "spent_today_usd": float(today["usd"] or 0),
        "spent_month_usd": float(month["usd"] or 0),
        "avg_latency_ms": float(today["lat"] or 0),
        "by_pool": [{"pool": r["pool"], "requests": int(r["n"]),
                     "usd": float(r["usd"] or 0)} for r in by_pool],
    }
