#!/usr/bin/env sh
set -eu

umask 027

CONFIG_PATH="${REMNASHOP_CONFIG:-/app/config.json}"
DB_PATH="${REMNASHOP_DB:-/app/starlight.db}"

mkdir -p "$(dirname "$CONFIG_PATH")" "$(dirname "$DB_PATH")"

# The general HTTP service must not load/modify bot identities or run bot recovery.
if [ "$#" -gt 0 ]; then
  exec "$@"
fi

python docker/config.py "$CONFIG_PATH"

exec python bot.py
