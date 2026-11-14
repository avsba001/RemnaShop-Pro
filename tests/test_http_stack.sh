#!/usr/bin/env bash
# Isolated function test: no installation, privilege changes, network or Docker.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
export INSTALL_DIR="${ROOT}"
source <(sed '/^main "\$@"$/d' "${ROOT}/bootstrap.sh")
MOCK_HTTP_HEALTH=healthy

docker() {
  case "$*" in
    *"ps -q remnashop") printf '%s\n' bot-id ;;
    *"ps -q http") printf '%s\n' http-id ;;
    *".State.Status"*) printf '%s\n' running ;;
    *".State.Health"*)
      if [[ "$*" == *http-id* ]]; then printf '%s\n' "${MOCK_HTTP_HEALTH}"; else printf '%s\n' healthy; fi ;;
    *".RestartCount"*) printf '%s\n' 0 ;;
    *) echo "unexpected mock docker invocation" >&2; return 99 ;;
  esac
}
HEALTH_TIMEOUT_SECONDS=5
wait_for_health
MOCK_HTTP_HEALTH=unhealthy
if wait_for_health; then
  echo "FAIL: unhealthy HTTP service was accepted" >&2
  exit 1
fi
echo "HTTP + Bot health gates PASS"
