#!/usr/bin/env bash
# Isolated preparation flow test: never installs or starts real services.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
source <(sed '/^main "\$@"$/d' "${ROOT}/bootstrap.sh")
TMP="$(mktemp -d)"
trap 'rm -rf -- "${TMP}"' EXIT
INSTALL_DIR="${TMP}/remnashop-pro"
mkdir -p "${INSTALL_DIR}"
printf 'ADMIN_ID=\r\nBOT_TOKEN=\r\n' >"${INSTALL_DIR}/.env.example"
NO_START=1
USE_LOCAL_SOURCE=1
install_base_dependencies() { :; }
install_docker() { :; }
install_docker_compose() { :; }
prepare_repo() { :; }
prompt_required_env() { echo 'unexpected credential prompt' >&2; return 99; }
start_stack() { echo 'unexpected startup' >&2; return 99; }
wait_for_health() { echo 'unexpected health wait' >&2; return 99; }
MOCK_RUNNING=''
docker() {
  printf '%s\n' "$*" >>"${TMP}/docker.calls"
  case "$*" in
    'ps -q --filter label=com.docker.compose.project=remnashop') printf '%s' "${MOCK_RUNNING}" ;;
    'compose -p remnashop config --quiet'|'compose -p remnashop build') return 0 ;;
    *) echo 'unexpected Docker command' >&2; return 99 ;;
  esac
}
install_flow
[ -f "${INSTALL_DIR}/.env" ]
grep -q '^ADMIN_ID=$' "${INSTALL_DIR}/.env"
grep -q '^BOT_TOKEN=$' "${INSTALL_DIR}/.env"
[ -z "$(get_env_value ADMIN_ID)" ]
[ -z "$(get_env_value BOT_TOKEN)" ]
grep -q '^compose -p remnashop build$' "${TMP}/docker.calls"
MOCK_RUNNING=existing-container
if (set -e; install_flow); then
  echo 'FAIL: running stack accepted as stopped deployment' >&2
  exit 1
fi
echo 'Prepare-only flow PASS'
