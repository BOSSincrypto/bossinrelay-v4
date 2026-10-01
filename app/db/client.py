"""libsql слой: embedded файл по умолчанию, Turso remote при обеих переменных.

- Дефолт: локальный файл LIBSQL_DB_PATH (./data/relay.db), работает без сети.
- При заданных LIBSQL_URL + LIBSQL_AUTH_TOKEN: клиент ходит напрямую в Turso
  (libsql:// конвертируется в https://), данные живут в облаке.
- Один глобальный ClientSync + threading.Lock (один процесс, один воркер).
- requests_log пишется батчами (~50 строк / один batch()), плюс сброс
  по возрасту (старшая запись старше 60с), чтобы хвост не терялся.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import threading
import time

from libsql_client import create_client_sync

from .schema import DDL, INDEXES

# ponytail: поток исполнителя libsql — daemon. Иначе любой короткий процесс
# (import app.main, self-test, скрипты) висит на выходе: интерпретатор
# сначала ждёт не-daemon потоки и только потом выполняет atexit, так что
# close_client через atexit уже не спасает. Явное закрытие (lifespan, CLI)
# по-прежнему работает как раньше — flush + join.
try:
    from libsql_client import sync as _libsql_sync

    def _daemon_executor_init(self) -> None:
        import asyncio as _aio
        import collections as _coll

        self._thread = threading.Thread(
            target=self._run, name="libsql_client", daemon=True)
        self._loop = _aio.new_event_loop()
        self._lock = threading.Lock()
        self._closed = False
        self._queue = _coll.deque()
        self._waker = None
        self._thread.start()

    _libsql_sync._AsyncExecutor.__init__ = _daemon_executor_init  # type: ignore[method-assign]
except Exception:
    pass

_BATCH_SIZE = 50
_FLUSH_AGE_S = 60.0

_lock = threading.Lock()
_client = None
_inited = False
_pending: list[tuple] = []  # строки requests_log, ждут batch-записи


def _db_path() -> str:
    return os.getenv("LIBSQL_DB_PATH", "./data/relay.db")


def get_client():
    global _client
    if _client is None or getattr(_client, "closed", False):
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


def close_client() -> None:
    """Закрыть глобальный клиент (освободить поток libsql sync-executor).

    Без этого CLI-процессы (self-test, скрипты) висят: поток executor'а
    non-daemon. В uvicorn не критично, но вызываем и там через atexit.
    Порядок важен: сначала flush на живом клиенте, потом отцепляем и закрываем —
    иначе flush пересоздаст клиент и поток останется."""
    global _client
    with _lock:
        c = _client
    if c is None:
        return
    try:
        flush_log()  # _client ещё на месте — новый клиент не создастся
    except Exception:
        pass
    with _lock:
        if _client is c:
            _client = None
    try:
        c.close()
    except Exception:
        pass


atexit.register(close_client)


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
    global _inited
    c = get_client()
    try:
        c.execute("SELECT 1")
    except Exception:
        global _client
        _client = None
        c = get_client()
    # таблицы могут отсутствовать (первый старт) — init идемпотентен,
    # флаг убирает DDL-прогон на каждый запрос.
    if not _inited:
        init_db()
        _inited = True
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


def cache_count() -> int:
    c = _ensure()
    with _lock:
        rs = c.execute("SELECT COUNT(*) AS n FROM cache_entries")
    return int(rs.rows[0]["n"] or 0)


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


# ---------- config (персист конфига релея; сырые ключи НЕ храним) ----------

def load_config() -> dict:
    """Весь конфиг из БД: providers, pools, quotas, settings, verdicts.
    Пустые таблицы -> пустые коллекции (caller решает: seed/bootstrap)."""
    c = _ensure()
    with _lock:
        provs = c.execute(
            "SELECT id, name, base_url, enabled FROM providers").rows
        keys = c.execute(
            "SELECT id, provider_id, masked, note, enabled, last_check_json"
            " FROM provider_keys").rows
        pmodels = c.execute(
            "SELECT provider_id, name FROM provider_models").rows
        pools = c.execute(
            "SELECT id, strategy, members_json, unlock_on, system_prompt, enabled"
            " FROM pools").rows
        quotas = c.execute("SELECT key, limit_day, limit_month FROM quotas").rows
        settings = c.execute("SELECT k, v FROM settings").rows
        verdicts = c.execute(
            "SELECT model_key, verdict, served_id, ceiling, error_class, note,"
            " checked_at FROM model_verdicts").rows
    models_by_prov: dict[str, list[str]] = {}
    for r in pmodels:
        models_by_prov.setdefault(r["provider_id"], []).append(r["name"])
    keys_by_prov: dict[str, list[dict]] = {}
    for r in keys:
        try:
            last_check = json.loads(r["last_check_json"] or "{}") or None
        except Exception:
            last_check = None
        keys_by_prov.setdefault(r["provider_id"], []).append({
            "id": r["id"], "masked": r["masked"], "note": r["note"],
            "enabled": bool(r["enabled"]), "last_check": last_check})
    return {
        "providers": [{
            "id": r["id"], "name": r["name"], "base_url": r["base_url"],
            "models": models_by_prov.get(r["id"], []),
            "enabled": bool(r["enabled"]),
            "keys": keys_by_prov.get(r["id"], [])} for r in provs],
        "pools": [{
            "id": r["id"], "strategy": r["strategy"],
            "members": json.loads(r["members_json"] or "[]"),
            "unlock_on": r["unlock_on"], "system_prompt": r["system_prompt"],
            "enabled": bool(r["enabled"])} for r in pools],
        "quotas": {r["key"]: {"limit_day": r["limit_day"],
                              "limit_month": r["limit_month"]} for r in quotas},
        "settings": {r["k"]: json.loads(r["v"]) for r in settings},
        "verdicts": {r["model_key"]: {
            "verdict": r["verdict"], "served_id": r["served_id"],
            "ceiling": r["ceiling"], "error_class": r["error_class"],
            "note": r["note"], "checked_at": r["checked_at"]} for r in verdicts},
    }


def save_provider(pid: str, name: str = "", base_url: str = "",
                  enabled: bool = True, models: list | None = None) -> None:
    c = _ensure()
    with _lock:
        c.execute(
            "INSERT INTO providers (id, name, base_url, enabled) VALUES (?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET name=excluded.name,"
            " base_url=excluded.base_url, enabled=excluded.enabled",
            [pid, name, base_url, 1 if enabled else 0])
        if models is not None:
            c.execute("DELETE FROM provider_models WHERE provider_id=?", [pid])
            for m in models:
                c.execute(
                    "INSERT OR IGNORE INTO provider_models (provider_id, name)"
                    " VALUES (?,?)", [pid, m])


def delete_provider_cfg(pid: str) -> None:
    c = _ensure()
    with _lock:
        c.execute("DELETE FROM provider_keys WHERE provider_id=?", [pid])
        c.execute("DELETE FROM provider_models WHERE provider_id=?", [pid])
        c.execute("DELETE FROM providers WHERE id=?", [pid])


def save_key(kid: str, provider_id: str, masked: str, note: str = "",
             enabled: bool = True, last_check: dict | None = None) -> None:
    c = _ensure()
    with _lock:
        c.execute(
            "INSERT INTO provider_keys (id, provider_id, masked, note, enabled,"
            " last_check_json) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET provider_id=excluded.provider_id,"
            " masked=excluded.masked, note=excluded.note,"
            " enabled=excluded.enabled,"
            " last_check_json=excluded.last_check_json",
            [kid, provider_id, masked, note, 1 if enabled else 0,
             json.dumps(last_check or {})])


def delete_key_cfg(kid: str) -> None:
    c = _ensure()
    with _lock:
        c.execute("DELETE FROM provider_keys WHERE id=?", [kid])


def save_pool(pid: str, strategy: str = "cascade", members: list | None = None,
              unlock_on: str = "", system_prompt: str = "",
              enabled: bool = True) -> None:
    c = _ensure()
    with _lock:
        c.execute(
            "INSERT INTO pools (id, strategy, members_json, unlock_on,"
            " system_prompt, enabled) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET strategy=excluded.strategy,"
            " members_json=excluded.members_json, unlock_on=excluded.unlock_on,"
            " system_prompt=excluded.system_prompt, enabled=excluded.enabled",
            [pid, strategy, json.dumps(members or []), unlock_on,
             system_prompt, 1 if enabled else 0])


def delete_pool_cfg(pid: str) -> None:
    c = _ensure()
    with _lock:
        c.execute("DELETE FROM pools WHERE id=?", [pid])


def save_quota(key: str, limit_day: int, limit_month: int) -> None:
    c = _ensure()
    with _lock:
        c.execute(
            "INSERT INTO quotas (key, limit_day, limit_month) VALUES (?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET limit_day=excluded.limit_day,"
            " limit_month=excluded.limit_month",
            [key, int(limit_day), int(limit_month)])


def delete_quota_cfg(key: str) -> None:
    c = _ensure()
    with _lock:
        c.execute("DELETE FROM quotas WHERE key=?", [key])


def save_setting(k: str, v) -> None:
    c = _ensure()
    with _lock:
        c.execute(
            "INSERT INTO settings (k, v) VALUES (?,?)"
            " ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            [k, json.dumps(v)])


def save_verdict(model_key: str, verdict: str = "", served_id: str = "",
                 ceiling: int = 0, error_class: str = "", note: str = "",
                 checked_at: int = 0) -> None:
    c = _ensure()
    with _lock:
        c.execute(
            "INSERT INTO model_verdicts (model_key, verdict, served_id, ceiling,"
            " error_class, note, checked_at) VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(model_key) DO UPDATE SET verdict=excluded.verdict,"
            " served_id=excluded.served_id, ceiling=excluded.ceiling,"
            " error_class=excluded.error_class, note=excluded.note,"
            " checked_at=excluded.checked_at",
            [model_key, verdict, served_id, int(ceiling or 0), error_class,
             note, int(checked_at or 0)])


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
