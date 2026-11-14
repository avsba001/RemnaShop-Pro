#!/usr/bin/env bash
# Run through non-PTY SSH/CI. No network, installs or real credentials.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
source <(sed '/^main "\$@"$/d' "${ROOT}/bootstrap.sh")
if ( : </dev/tty ) 2>/dev/null; then
  echo 'SKIP: this test requires a process without a controlling terminal'
  exit 0
fi
TMP="$(mktemp -d)"
trap 'rm -rf -- "${TMP}"' EXIT
INSTALL_DIR="${TMP}"
printf 'ADMIN_ID=123456\nBOT_TOKEN=123456:abcdefghijklmnopqrstuvwxyz\n' >"${TMP}/.env"
prompt_required_env
printf 'ADMIN_ID=\nBOT_TOKEN=\n' >"${TMP}/.env"
if (prompt_required_env); then
  echo 'FAIL: missing credentials accepted without TTY' >&2
  exit 1
fi
echo 'Non-PTY bootstrap credentials PASS'
