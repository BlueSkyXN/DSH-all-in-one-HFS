#!/usr/bin/env bash
set -Eeuo pipefail

readonly expected_uid="1000"
readonly data_root="${DSH_HOME:-/data/dsh}"
readonly workspace="${DSH_WORKSPACE:-${data_root}/workspace}"
readonly internal_port="${DSH_INTERNAL_PORT:-3080}"
readonly public_port="${PORT:-7860}"
readonly trusted_host="${DSH_TRUSTED_HOST:-blueskyxn-dsh-all-in-one-hfs.hf.space}"
readonly dsh_bin="/opt/dsh/node_modules/.bin/dsh"
readonly nginx_auth_file="/tmp/dsh-nginx/htpasswd"

children=()

fail() {
  printf '[dsh-hfs] ERROR: %s\n' "$*" >&2
  exit 1
}

verify_writable_directory() {
  local directory="$1"
  local probe="${directory}/.hfs-write-probe-${BASHPID}-${RANDOM}"
  local payload="dsh-hfs-write-probe"

  test ! -e "${probe}" || fail "write probe collision at ${probe}"
  printf '%s\n' "${payload}" >"${probe}" \
    || fail "cannot write to ${directory}"
  test "$(tr -d '\r\n' <"${probe}")" = "${payload}" \
    || fail "cannot read back a write probe from ${directory}"
  rm -- "${probe}" || fail "cannot remove a write probe from ${directory}"
}

terminate_children() {
  local pid
  for pid in "${children[@]:-}"; do
    kill -TERM "${pid}" 2>/dev/null || true
  done
  for pid in "${children[@]:-}"; do
    wait "${pid}" 2>/dev/null || true
  done
}

# shellcheck disable=SC2329  # Invoked by the TERM/INT trap below.
handle_signal() {
  trap - TERM INT
  terminate_children
  exit 0
}

trap handle_signal TERM INT

test "$(id -u)" = "${expected_uid}" \
  || fail "runtime UID must be ${expected_uid} on Hugging Face Spaces"
test -x "${dsh_bin}" || fail "the pinned dsh executable is missing"
test "$(${dsh_bin} --version)" = "${DSH_VERSION}" \
  || fail "installed dsh version does not match DSH_VERSION=${DSH_VERSION}"
test "${data_root}" = "/data/dsh" \
  || fail "DSH_HOME must remain /data/dsh for the mounted bucket contract"
test "${workspace}" = "/data/dsh/workspace" \
  || fail "DSH_WORKSPACE must remain /data/dsh/workspace"
mountpoint -q /data \
  || fail "/data must be a mounted persistent Hugging Face bucket"

: "${ADMIN_USERNAME:?ADMIN_USERNAME is required}"
: "${ADMIN_PASSWORD:?ADMIN_PASSWORD is required}"
[[ "${ADMIN_USERNAME}" =~ ^[A-Za-z0-9_.@-]{1,64}$ ]] \
  || fail "ADMIN_USERNAME contains unsupported characters"
[[ "${#ADMIN_PASSWORD}" -ge 16 ]] \
  || fail "ADMIN_PASSWORD must be at least 16 characters"
[[ "${ADMIN_PASSWORD}" != *$'\n'* && "${ADMIN_PASSWORD}" != *$'\r'* ]] \
  || fail "ADMIN_PASSWORD must be a single line"
[[ "${trusted_host}" =~ ^[A-Za-z0-9.-]+(:[0-9]{1,5})?$ ]] \
  || fail "DSH_TRUSTED_HOST must be one bare host[:port] authority"
[[ "${internal_port}" =~ ^[0-9]+$ && "${public_port}" =~ ^[0-9]+$ ]] \
  || fail "DSH_INTERNAL_PORT and PORT must be numeric"
test "${internal_port}" = "3080" \
  || fail "DSH_INTERNAL_PORT must remain 3080"
test "${public_port}" = "7860" \
  || fail "PORT must remain 7860"

umask 077
mkdir -p \
  "${data_root}/home" \
  "${workspace}" \
  /tmp/dsh-cache \
  /tmp/npm-cache \
  /tmp/dsh-nginx/client_body \
  /tmp/dsh-nginx/proxy \
  /tmp/dsh-nginx/fastcgi \
  /tmp/dsh-nginx/uwsgi \
  /tmp/dsh-nginx/scgi
verify_writable_directory "${data_root}"
verify_writable_directory "${workspace}"

printf '%s\n' "${ADMIN_PASSWORD}" \
  | htpasswd -imc "${nginx_auth_file}" "${ADMIN_USERNAME}" >/dev/null
chmod 0600 "${nginx_auth_file}"

export DSH_HOME="${data_root}"
export DSH_WORKSPACE="${workspace}"
export HOME="${data_root}/home"
export XDG_CACHE_HOME="${XDG_CACHE_HOME:-/tmp/dsh-cache}"
export NPM_CONFIG_CACHE="${NPM_CONFIG_CACHE:-/tmp/npm-cache}"

cd "${workspace}"

"${dsh_bin}" web \
  --port "${internal_port}" \
  --trusted-host "${trusted_host}" &
dsh_pid=$!
children+=("${dsh_pid}")

ready=0
for _ in $(seq 1 180); do
  if curl -fsS -o /dev/null "http://127.0.0.1:${internal_port}/"; then
    ready=1
    break
  fi
  if ! kill -0 "${dsh_pid}" 2>/dev/null; then
    wait "${dsh_pid}" || true
    fail "dsh exited before its loopback Web UI became ready"
  fi
  sleep 1
done

if [[ "${ready}" != "1" ]]; then
  terminate_children
  fail "dsh did not become ready within 180 seconds"
fi

printf '[dsh-hfs] dsh is ready on loopback port %s; starting authenticated proxy on port %s\n' \
  "${internal_port}" "${public_port}"
nginx -c /etc/nginx/nginx.conf -g 'daemon off;' &
nginx_pid=$!
children+=("${nginx_pid}")

set +e
wait -n "${dsh_pid}" "${nginx_pid}"
status=$?
set -e

printf '[dsh-hfs] ERROR: a supervised process exited; stopping the container\n' >&2
terminate_children
if [[ "${status}" = "0" ]]; then
  exit 1
fi
exit "${status}"
