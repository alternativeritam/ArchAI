#!/usr/bin/env bash

set -Eeuo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
readonly BACKEND_DIR="${PROJECT_ROOT}/backend"
readonly FRONTEND_DIR="${PROJECT_ROOT}/frontend"
readonly LOG_DIR="${PROJECT_ROOT}/logs"
readonly BACKEND_LOG="${LOG_DIR}/backend.log"
readonly FRONTEND_LOG="${LOG_DIR}/frontend.log"
readonly BACKEND_PID_FILE="${LOG_DIR}/backend.pid"
readonly FRONTEND_PID_FILE="${LOG_DIR}/frontend.pid"

backend_port=8000
frontend_port=3000
debug=false
backend_pid=""
frontend_pid=""

usage() {
  cat <<'EOF'
Usage: ./scripts/start.sh [options]

Start the ArchAI backend and frontend development servers together. Services
run in the background by default and write their output to the logs directory.

Options:
  --backend-port PORT   Backend port (default: 8000)
  --frontend-port PORT  Frontend port (default: 3000)
  --debug               Run in the foreground, show logs, and reload backend code
  -h, --help            Show this help message
EOF
}

fail() {
  printf 'Error: %s\n' "$1" >&2
  exit 1
}

validate_port() {
  local name="$1"
  local port="$2"

  if [[ ! "${port}" =~ ^[0-9]+$ ]] || ((port < 1 || port > 65535)); then
    fail "${name} must be an integer between 1 and 65535 (received: ${port})."
  fi
}

while (($# > 0)); do
  case "$1" in
    --backend-port)
      (($# >= 2)) || fail "--backend-port requires a value."
      backend_port="$2"
      shift 2
      ;;
    --frontend-port)
      (($# >= 2)) || fail "--frontend-port requires a value."
      frontend_port="$2"
      shift 2
      ;;
    --debug)
      debug=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "Unknown option: $1. Run with --help for usage."
      ;;
  esac
done

validate_port "Backend port" "${backend_port}"
validate_port "Frontend port" "${frontend_port}"

if [[ "${backend_port}" == "${frontend_port}" ]]; then
  fail "Backend and frontend ports must be different."
fi

backend_python="${BACKEND_DIR}/.venv/bin/python"
frontend_next="${FRONTEND_DIR}/node_modules/.bin/next"

if [[ ! -x "${backend_python}" ]]; then
  fail "Backend environment not found. Create backend/.venv and install backend/requirements.txt first."
fi

if [[ ! -x "${frontend_next}" ]]; then
  fail "Frontend dependencies not found. Run 'npm install' in the frontend directory first."
fi

selected_origins="http://localhost:${frontend_port},http://127.0.0.1:${frontend_port}"
if [[ -n "${ARCHAI_CORS_ORIGINS:-}" ]]; then
  selected_origins="${selected_origins},${ARCHAI_CORS_ORIGINS}"
fi

pid_from_file() {
  local pid_file="$1"
  local pid=""

  [[ -f "${pid_file}" ]] || return 1
  read -r pid < "${pid_file}" || true
  [[ "${pid}" =~ ^[0-9]+$ ]] || return 1
  printf '%s' "${pid}"
}

ensure_service_stopped() {
  local service="$1"
  local pid_file="$2"
  local pid=""

  if pid="$(pid_from_file "${pid_file}")" && kill -0 "${pid}" 2>/dev/null; then
    fail "${service} is already running with PID ${pid}. Run ./scripts/shutdown.sh first."
  fi

  rm -f "${pid_file}"
}

terminate_process_group() {
  local pid="$1"

  if kill -0 "${pid}" 2>/dev/null; then
    kill -TERM -- "-${pid}" 2>/dev/null || kill -TERM "${pid}" 2>/dev/null || true
  fi
}

job_is_running() {
  local expected_pid="$1"
  local running_pid=""

  while read -r running_pid; do
    [[ "${running_pid}" == "${expected_pid}" ]] && return 0
  done < <(jobs -pr)
  return 1
}

cleanup_foreground() {
  local exit_code=$?
  trap - EXIT INT TERM

  [[ -z "${backend_pid}" ]] || terminate_process_group "${backend_pid}"
  [[ -z "${frontend_pid}" ]] || terminate_process_group "${frontend_pid}"
  [[ -z "${backend_pid}" ]] || wait "${backend_pid}" 2>/dev/null || true
  [[ -z "${frontend_pid}" ]] || wait "${frontend_pid}" 2>/dev/null || true

  exit "${exit_code}"
}

start_backend() {
  cd "${BACKEND_DIR}"
  exec env ARCHAI_CORS_ORIGINS="${selected_origins}" \
    "${backend_python}" -m uvicorn archai.api:app \
    --host 127.0.0.1 --port "${backend_port}" \
    --reload --reload-dir archai
}

start_frontend() {
  cd "${FRONTEND_DIR}"
  exec env NEXT_PUBLIC_ARCHAI_API_BASE_URL="http://127.0.0.1:${backend_port}" \
    "${frontend_next}" dev --port "${frontend_port}"
}

mkdir -p "${LOG_DIR}"
ensure_service_stopped "Backend" "${BACKEND_PID_FILE}"
ensure_service_stopped "Frontend" "${FRONTEND_PID_FILE}"

if [[ "${debug}" == true ]]; then
  printf 'Starting ArchAI backend at http://127.0.0.1:%s\n' "${backend_port}"
  start_backend &
  backend_pid=$!

  printf 'Starting ArchAI frontend at http://localhost:%s\n' "${frontend_port}"
  start_frontend &
  frontend_pid=$!

  trap cleanup_foreground EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM

  printf 'Debug mode is active. Press Ctrl+C to stop both services.\n'
  while kill -0 "${backend_pid}" 2>/dev/null && kill -0 "${frontend_pid}" 2>/dev/null; do
    sleep 1
  done

  if ! kill -0 "${backend_pid}" 2>/dev/null; then
    wait "${backend_pid}"
  else
    wait "${frontend_pid}"
  fi
  exit 0
fi

timestamp="$(date '+%Y-%m-%d %H:%M:%S %z')"
printf '\n[%s] Starting backend on port %s\n' "${timestamp}" "${backend_port}" >> "${BACKEND_LOG}"
printf '\n[%s] Starting frontend on port %s\n' "${timestamp}" "${frontend_port}" >> "${FRONTEND_LOG}"

# Job control gives each detached service its own process group so the shutdown
# script can stop each service and any child processes together.
set -m
(
  cd "${BACKEND_DIR}"
  exec nohup env ARCHAI_CORS_ORIGINS="${selected_origins}" \
    "${backend_python}" -m uvicorn archai.api:app \
    --host 127.0.0.1 --port "${backend_port}"
) >> "${BACKEND_LOG}" 2>&1 < /dev/null &
backend_pid=$!
(
  cd "${FRONTEND_DIR}"
  exec nohup env NEXT_PUBLIC_ARCHAI_API_BASE_URL="http://127.0.0.1:${backend_port}" \
    "${frontend_next}" dev --port "${frontend_port}"
) >> "${FRONTEND_LOG}" 2>&1 < /dev/null &
frontend_pid=$!
set +m

printf '%s\n' "${backend_pid}" > "${BACKEND_PID_FILE}"
printf '%s\n' "${frontend_pid}" > "${FRONTEND_PID_FILE}"

sleep 2
if ! job_is_running "${backend_pid}" || ! job_is_running "${frontend_pid}"; then
  terminate_process_group "${backend_pid}"
  terminate_process_group "${frontend_pid}"
  wait "${backend_pid}" 2>/dev/null || true
  wait "${frontend_pid}" 2>/dev/null || true
  rm -f "${BACKEND_PID_FILE}" "${FRONTEND_PID_FILE}"
  fail "A service exited during startup. Check ${BACKEND_LOG} and ${FRONTEND_LOG}."
fi

printf 'ArchAI services started in the background.\n'
printf '  Backend:  http://127.0.0.1:%s (PID %s)\n' "${backend_port}" "${backend_pid}"
printf '  Frontend: http://localhost:%s (PID %s)\n' "${frontend_port}" "${frontend_pid}"
printf '  Logs:     %s\n' "${LOG_DIR}"
printf 'Run ./scripts/shutdown.sh to stop both services.\n'
