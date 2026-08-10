#!/usr/bin/env bash

set -u

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
readonly LOG_DIR="${PROJECT_ROOT}/logs"
readonly BACKEND_PID_FILE="${LOG_DIR}/backend.pid"
readonly FRONTEND_PID_FILE="${LOG_DIR}/frontend.pid"

if (($# > 0)); then
  if [[ "$1" == "-h" || "$1" == "--help" ]]; then
    printf 'Usage: ./scripts/shutdown.sh\n'
    exit 0
  fi
  printf 'Error: shutdown.sh does not accept arguments.\n' >&2
  exit 1
fi

stop_service() {
  local service="$1"
  local pid_file="$2"
  local pid=""
  local attempts=0

  if [[ ! -f "${pid_file}" ]]; then
    printf '%s is not running (no PID file).\n' "${service}"
    return
  fi

  read -r pid < "${pid_file}" || true
  if [[ ! "${pid}" =~ ^[0-9]+$ ]]; then
    printf '%s has an invalid PID file; removing it.\n' "${service}"
    rm -f "${pid_file}"
    return
  fi

  if ! kill -0 "${pid}" 2>/dev/null; then
    printf '%s is not running; removing its stale PID file.\n' "${service}"
    rm -f "${pid_file}"
    return
  fi

  printf 'Stopping %s (PID %s)...\n' "${service}" "${pid}"
  kill -TERM -- "-${pid}" 2>/dev/null || kill -TERM "${pid}" 2>/dev/null || true

  while kill -0 "${pid}" 2>/dev/null && ((attempts < 10)); do
    sleep 1
    ((attempts += 1))
  done

  if kill -0 "${pid}" 2>/dev/null; then
    printf '%s did not stop gracefully; forcing shutdown.\n' "${service}"
    kill -KILL -- "-${pid}" 2>/dev/null || kill -KILL "${pid}" 2>/dev/null || true
  fi

  rm -f "${pid_file}"
  printf '%s stopped.\n' "${service}"
}

stop_service "Backend" "${BACKEND_PID_FILE}"
stop_service "Frontend" "${FRONTEND_PID_FILE}"
