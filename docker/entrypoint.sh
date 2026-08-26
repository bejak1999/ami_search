#!/bin/sh
set -e

# A missing secret key would silently rotate on every restart and log everyone
# out, so generate one into the data volume the first time and reuse it.
if [ -z "${SECRET_KEY:-}" ]; then
    KEY_FILE="${DATA_DIR:-/data}/.secret_key"
    if [ ! -f "$KEY_FILE" ]; then
        mkdir -p "$(dirname "$KEY_FILE")"
        python -c "import secrets; print(secrets.token_urlsafe(48))" > "$KEY_FILE"
        chmod 600 "$KEY_FILE"
        echo "[amisearch] Generated a persistent SECRET_KEY in $KEY_FILE"
    fi
    SECRET_KEY="$(cat "$KEY_FILE")"
    export SECRET_KEY
fi

exec python -m uvicorn app.main:app \
    --host "${HOST:-0.0.0.0}" \
    --port "${PORT:-8080}" \
    --proxy-headers \
    --forwarded-allow-ips '*' \
    --log-level "${UVICORN_LOG_LEVEL:-info}" \
    "$@"
