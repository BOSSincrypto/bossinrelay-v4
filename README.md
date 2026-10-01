# BossInRelay v4 — LLM-релей с админкой

Небольшой шлюз к нескольким LLM-провайдерам: пулы моделей со стратегиями
(manual/cascade/random/cheapest), кэширование ответов, лимиты расходов,
сессии диалогов и веб-админка на Jinja. Стек: Python 3.12 + FastAPI,
embedded libsql (`relay.db`) с опциональной репликацией в Turso Cloud.
Без Turso всё работает — файл БД локальный.

Целевой сервер: 512MB RAM, 2GB диск, uplink 4 Gbps, хостинг infrlo.com
(домен и HTTPS уже выданы панелью хостинга).

## Архитектура

`app/main.py` собирает FastAPI-приложение из трёх роутеров. `app/dashboard/views.py`
отдаёт HTML-страницы админки (обзор, провайдеры, пулы, кэш, сессии, квоты,
настройки). `app/dashboard/admin_api.py` — `/admin/*` API: `/stats` уже читает
реальные агрегаты из `requests_log` (seed — только пока трафика нет),
`/keys/{kid}/check` делает настоящий пробный вызов через ядро. Конфиг
(провайдеры/пулы/квоты/настройки) — пока in-memory `STORE`
(`app/dashboard/store.py`). Авторизация общая — Bearer `ADMIN_TOKEN` или
cookie `admin_token` (`app/dashboard/auth.py`).

Ядро (готово):

- `app/core/engine.py` — проход chat: точный кэш → пул (manual/cascade/random/
  cheapest) → fallback по ключам с success-rate breaker (порог 0.8, cooldown
  120с) и context-window-fallback; unlock подставляет `system_prompt` пула;
  квоты день/месяц; персистентные сессии в БД (трим 40 сообщений); запись
  в `requests_log` батчами; дешёвая identity-проверка (`served_id`, вердикт).
- `app/providers/` — `openai_compat` (OpenAI, OpenRouter, DeepSeek, Groq, Ollama,
  vLLM, LM Studio), `native` (Anthropic, Gemini, Mistral). Ретрай стрима —
  только до первого байта SSE.
- `app/cache/` — точный SHA256 + семантический (numpy-косинус, порог пула,
  флаг `SEMANTIC_CACHE_ENABLED=false` отключает уровень).
- `app/db/` — libsql: embedded файл `LIBSQL_DB_PATH` по умолчанию, при
  `LIBSQL_URL` + `LIBSQL_AUTH_TOKEN` — Turso Cloud.
- `app/routers/v1.py` — публичные `/v1/chat/completions` (non-stream + SSE),
  `/v1/embeddings`, `/v1/models`, `/v1/sessions`. Клиентские ключи
  `sk-relay-...` из `CLIENT_API_KEYS`, учёт в таблице `users`.

## Быстрый старт локально

```bash
cp .env.example .env        # вписать ADMIN_TOKEN и хотя бы один ключ
uv sync                     # или: pip install -r requirements.txt
uv run uvicorn app.main:app --reload --port 8000
# Админка: http://127.0.0.1:8000/  (редирект на /login)
```

Проверка API (токен из `.env`):

```bash
T=твой-admin-токен
curl -H "Authorization: Bearer $T" http://127.0.0.1:8000/admin/stats
curl -H "Authorization: Bearer $T" http://127.0.0.1:8000/admin/providers
```

## Деплой на infrlo

```bash
cp .env.example .env   # заполнить: ADMIN_TOKEN, ключи, DOMAIN
chmod +x deploy/infrlo.sh
./deploy/infrlo.sh deploy   # сборка + запуск + healthcheck
./deploy/infrlo.sh backup   # бэкап relay.db в backups/relay-<дата>.db
./deploy/infrlo.sh logs     # логи контейнера
```

Переменные скрипта (можно в окружении): `APP_NAME` (по умолчанию
bossinrelay), `IMAGE`, `PORT`, `DOMAIN`, `SSH_TARGET` (например
`root@1.2.3.4` — тогда команды docker идут по ssh, а `.env`
читается локально). Контейнер стартует с `--memory 480m`
(запас под сам Docker на тарифе 512MB) и volume `relay-data`
для `/srv/data/relay.db`. HTTPS и домен — на стороне панели
infrlo (реверс-прокси на `127.0.0.1:8000`).

## API: примеры curl

Базовый URL ниже — `http://127.0.0.1:8000` локально или
`https://твой-домен` на infrlo. Админские вызовы — с заголовком
`Authorization: Bearer $ADMIN_TOKEN`. Эндпоинты `/v1/*` появятся
вместе с ядром; контракт фиксируем здесь заранее.

Обычный chat (ядро):

```bash
curl -X POST $BASE/v1/chat -H 'Content-Type: application/json' -d '{
  "pool": "chat-main",
  "messages": [{"role": "user", "content": "Привет! Что ты умеешь?"}]
}'
```

Стриминг (SSE, ядро):

```bash
curl -N -X POST $BASE/v1/chat/stream -H 'Content-Type: application/json' -d '{
  "pool": "chat-main",
  "messages": [{"role": "user", "content": "Напиши функцию суммы на Python"}]
}'
```

Список сессий и одна сессия (админка):

```bash
curl -H "Authorization: Bearer $T" $BASE/admin/sessions
curl -H "Authorization: Bearer $T" $BASE/admin/sessions/sess_1
```

Управление пулом и сброс кэша (админка):

```bash
curl -X POST -H "Authorization: Bearer $T" \
  -H 'Content-Type: application/json' \
  -d '{"id":"chat-main","strategy":"cascade","members":["openai:gpt-4o-mini"]}' \
  $BASE/admin/pools
curl -X POST -H "Authorization: Bearer $T" $BASE/admin/cache/clear
```

## Структура БД (ядро, libsql)

Таблицы: `users` (id, api_key_hash, quota_day, quota_month, created_at),
`providers` (id, name, base_url, enabled), `models` (provider_id, name,
price_in, price_out, enabled), `cache_entries` (prompt_hash CHAR(64),
pool, response, embedding BLOB NULL, created_at, expires_at),
`requests_log` (ts, user_id, pool, provider, model, prompt_tokens,
completion_tokens, cost_usd, latency_ms, cache_hit).

Персист конфига релея: `provider_keys` (id, provider_id, masked, note,
enabled, last_check_json), `provider_models` (provider_id, name),
`pools` (id, strategy, members_json, unlock_on, system_prompt, enabled),
`quotas` (key, limit_day, limit_month), `settings` (k, v),
`model_verdicts` (model_key, verdict, served_id, ceiling, error_class,
note, checked_at). При старте STORE заливается из БД
(`store.ensure_loaded()`), при пустой БД seed пишется в неё (bootstrap).
Каждая мутация конфига через `/admin/*` вызывает `store.persist()`.
Сырые API-ключи не хранятся никогда — только masked, сами ключи в env.

Индексы: `cache_entries(prompt_hash, pool)`, `cache_entries(expires_at)`
для TTL-чистки, `requests_log(ts)`, `requests_log(user_id, ts)`.
Чистка протухшего кэша — периодическим `DELETE WHERE expires_at < now`.
Записи статистики в лог батчатся (накопление ~50–100 строк, один INSERT),
чтобы не дёргать диск на каждый запрос.

Глубокая проверка модели (`app/core/deepcheck.py`): 7 сигналов с взвешенным
голосованием hard=3/soft=2/context=1 — сверка `response.model`, утечка id
в текстах ошибок, потолок output, токенайзер-фингерпринт (допуск 5%),
энтропия однотокенной выборки (N=20), self-report cutoff + «НЕ ЗНАЮ»-проба,
пазлы (арифметика, подсчёт букв, формат). Запуск — только кнопкой из дашборда
(`POST /admin/models/{provider}/{model}/deep-check`), в фоне через
`BackgroundTasks`, бюджет ~120с, вердикт + уверенность падают в реестр
и персистятся. Self-test без живых ключей:
`python -m app.core.deepcheck --self-test` (мок транспорта, оба вердикта).

Remote-vs-embedded: дефолт — embedded файл `relay.db`, работает без сети
и без Turso. Sync в Turso Cloud включается двумя переменными окружения
`LIBSQL_URL` + `LIBSQL_AUTH_TOKEN`; при их отсутствии код молча остаётся
на локальном файле.

## Оптимизация под 512MB

Одно правило важнее остальных: запускать ровно один воркер uvicorn
(`--workers 1` уже зашит в Dockerfile). Каждый дополнительный воркер —
это ещё одна копия приложения, numpy и пулов соединений, на 512MB это
смерть. Остальное: лимит конкурентных запросов `--limit-concurrency 64`
держит очередь вместо раздувания памяти; кэш ограничен
`CACHE_MAX_ENTRIES=10000` и `CACHE_TTL_SECONDS=3600`; статистика пишется
батчами, а не по одному INSERT; семантический уровень кэша отключается
флагом `SEMANTIC_CACHE_ENABLED=false` — тогда в памяти нет numpy-матрицы
эмбеддингов и остаётся только дешёвый точный кэш по SHA256. Первый уровень
кэша всегда точный (SHA256 промпта + пул), второй — семантический на
numpy без torch/faiss: косинусная близость эмбеддингов, порог
`similarity_threshold` и TTL берутся из настроек пула.
