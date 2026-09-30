#!/usr/bin/env bash
# Деплой BossInRelay на infrlo.com: сборка, пуш, запуск, бэкап relay.db.
# Использование:
#   cp .env.example .env   # заполнить ADMIN_TOKEN, ключи провайдеров, DOMAIN
#   ./deploy/infrlo.sh deploy   # полный цикл
#   ./deploy/infrlo.sh backup   # только бэкап БД
#   ./deploy/infrlo.sh logs     # логи контейнера
set -euo pipefail

APP_NAME="${APP_NAME:-bossinrelay}"
IMAGE="${IMAGE:-bossinrelay:latest}"
PORT="${PORT:-8000}"
DOMAIN="${DOMAIN:-}"
SSH_TARGET="${SSH_TARGET:-}"   # например: root@123.45.67.89 ; пусто = локальный docker

need() { command -v "$1" >/dev/null 2>&1 || { echo "Нет $1 в PATH" >&2; exit 1; }; }
need docker

run_remote() {
    # Выполняет команду: локально либо по ssh на сервер infrlo.
    if [ -n "$SSH_TARGET" ]; then
        ssh -o BatchMode=yes "$SSH_TARGET" "$@"
    else
        "$@"
    fi
}

load_env() {
    if [ -f .env ]; then
        set -a
        # shellcheck disable=SC1091
        . ./.env
        set +a
    fi
}

build() {
    echo "==> Сборка образа $IMAGE"
    docker build -t "$IMAGE" .
    echo "Размер образа:"
    docker images "$IMAGE" --format '{{.Repository}}:{{.Tag}}  {{.Size}}'
}

backup() {
    echo "==> Бэкап relay.db"
    local ts
    ts="$(date +%Y%m%d-%H%M%S)"
    mkdir -p backups
    # Online-бэкап sqlite/libsql: .backup не блокирует чтение.
    if run_remote docker exec "$APP_NAME" sh -c \
        "sqlite3 \"\${LIBSQL_DB_PATH:-/srv/data/relay.db}\" '.backup /tmp/relay-backup.db'" 2>/dev/null; then
        if [ -n "$SSH_TARGET" ]; then
            scp "$SSH_TARGET:/tmp/relay-backup.db" "backups/relay-$ts.db"
        else
            run_remote docker cp "$APP_NAME:/tmp/relay-backup.db" "backups/relay-$ts.db"
        fi
    else
        # Fallback: копия файла БД из volume (контейнер остановить не требуем,
        # но бэкап может быть слегка несогласован при активной записи).
        echo "sqlite3 в контейнере нет, копирую файл БД напрямую"
        if [ -n "$SSH_TARGET" ]; then
            ssh "$SSH_TARGET" "docker cp $APP_NAME:/srv/data/relay.db /tmp/relay-backup.db"
            scp "$SSH_TARGET:/tmp/relay-backup.db" "backups/relay-$ts.db"
        else
            run_remote docker cp "$APP_NAME:/srv/data/relay.db" "backups/relay-$ts.db"
        fi
    fi
    ls -la "backups/relay-$ts.db"
}

deploy() {
    load_env
    if [ "${ADMIN_TOKEN:-changeme}" = "changeme" ] || [ -z "${ADMIN_TOKEN:-}" ]; then
        echo "ОШИБКА: задай ADMIN_TOKEN в .env (openssl rand -hex 32)" >&2
        exit 1
    fi
    build
    echo "==> Остановка старого контейнера (если есть)"
    run_remote docker rm -f "$APP_NAME" 2>/dev/null || true
    echo "==> Запуск"
    # Лимит памяти под тариф 512MB; swap отключён.
    # shellcheck disable=SC2086
    run_remote docker run -d --name "$APP_NAME" --restart unless-stopped \
        --memory 480m --memory-swap 480m \
        -p "127.0.0.1:${PORT}:8000" \
        -v relay-data:/srv/data \
        --env-file .env \
        -e ADMIN_TOKEN="$ADMIN_TOKEN" \
        ${LIBSQL_URL:+-e LIBSQL_URL="$LIBSQL_URL"} \
        ${LIBSQL_AUTH_TOKEN:+-e LIBSQL_AUTH_TOKEN="$LIBSQL_AUTH_TOKEN"} \
        ${OPENAI_API_KEY:+-e OPENAI_API_KEY="$OPENAI_API_KEY"} \
        ${ANTHROPIC_API_KEY:+-e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY"} \
        ${OPENROUTER_API_KEY:+-e OPENROUTER_API_KEY="$OPENROUTER_API_KEY"} \
        ${DEEPSEEK_API_KEY:+-e DEEPSEEK_API_KEY="$DEEPSEEK_API_KEY"} \
        ${SEMANTIC_CACHE_ENABLED:+-e SEMANTIC_CACHE_ENABLED="$SEMANTIC_CACHE_ENABLED"} \
        "$IMAGE"
    echo "==> Проверка здоровья"
    sleep 8
    local base="http://127.0.0.1:${PORT}"
    if [ -n "$SSH_TARGET" ]; then
        ssh "$SSH_TARGET" "curl -sf $base/healthz || curl -sf $base/login -o /dev/null"
    else
        curl -sf "$base/healthz" || curl -sf "$base/login" -o /dev/null
    fi
    echo "OK: контейнер жив"
    if [ -n "$DOMAIN" ]; then
        echo "Внешняя проверка: curl -s https://$DOMAIN/login -o /dev/null -w '%{http_code}'"
        curl -sk "https://$DOMAIN/login" -o /dev/null -w 'HTTP %{http_code}\n' || true
    else
        echo "(DOMAIN не задан — HTTPS проверь вручную на infrlo-панели)"
    fi
}

logs() {
    run_remote docker logs -f --tail 100 "$APP_NAME"
}

case "${1:-deploy}" in
    deploy) deploy ;;
    build) build ;;
    backup) backup ;;
    logs) logs ;;
    *) echo "Использование: $0 {deploy|build|backup|logs}" >&2; exit 1 ;;
esac
