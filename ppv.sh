#!/usr/bin/env bash
# Manage the Post Prompt Viewer web service.
# Ported from latency_checker's latency-ui.sh (same conventions).
#
# Usage:
#   ./ppv.sh start    Start the server in the background
#   ./ppv.sh stop     Stop the running server
#   ./ppv.sh restart  Stop then start
#   ./ppv.sh status   Show running state
#   ./ppv.sh logs     Tail the log (Ctrl-C to exit)
#
# Environment variables (all optional; ./ppv.env is sourced if present):
#   PPV_HOST          Host to bind (default: 127.0.0.1)
#   PPV_PORT          Port to listen on (default: 8080)
#   PPV_LOG           Log file path (default: ./ppv.log)
#   PPV_PID           PID file path (default: ./ppv.pid)
#   PPV_BIN           Path to the post-prompt-viewer binary (default: on PATH)
#   PPV_PROXY_PREFIX  URL prefix when served behind a reverse proxy (default: "")
#   PPV_DATA_DIR      Data directory (SQLite db + recording cache)
#   PPV_AUTO_ANALYZE  Download + analyze recordings on ingest (default: true)

set -euo pipefail

# Deployment-local config (gitignored). Exports every assignment to the server.
if [[ -f ./ppv.env ]]; then
  set -a
  # shellcheck disable=SC1091
  . ./ppv.env
  set +a
fi

HOST="${PPV_HOST:-127.0.0.1}"
PORT="${PPV_PORT:-8080}"
LOG_FILE="${PPV_LOG:-./ppv.log}"
PID_FILE="${PPV_PID:-./ppv.pid}"
BIN="${PPV_BIN:-post-prompt-viewer}"

is_running() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

cmd_start() {
  if is_running; then
    echo "Already running (pid $(cat "$PID_FILE"))"
    return 0
  fi

  if ! command -v "$BIN" >/dev/null 2>&1; then
    echo "Error: '$BIN' not found on PATH. Install with: pip install -e ." >&2
    exit 1
  fi

  echo "Starting post-prompt-viewer on $HOST:$PORT (log: $LOG_FILE)"
  nohup "$BIN" --host "$HOST" --port "$PORT" >> "$LOG_FILE" 2>&1 &
  local pid=$!
  disown "$pid" 2>/dev/null || true
  echo "$pid" > "$PID_FILE"

  # Uvicorn takes a few seconds to fail-and-exit if the port is busy; watch the
  # process for up to 8 seconds. If it exits in that window, it failed.
  local deadline=$((SECONDS + 8))
  while (( SECONDS < deadline )); do
    if ! kill -0 "$pid" 2>/dev/null; then
      rm -f "$PID_FILE"
      echo "Failed to start — last log lines:" >&2
      tail -n 20 "$LOG_FILE" >&2 2>/dev/null || true
      exit 1
    fi
    sleep 0.25
  done

  echo "Started (pid $pid)"
}

cmd_stop() {
  if ! is_running; then
    echo "Not running"
    rm -f "$PID_FILE"
    return 0
  fi

  local pid
  pid="$(cat "$PID_FILE")"
  echo "Stopping pid $pid"
  kill "$pid"

  for _ in $(seq 1 20); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.5
  done

  if kill -0 "$pid" 2>/dev/null; then
    echo "Graceful stop timed out; sending SIGKILL"
    kill -9 "$pid" 2>/dev/null || true
  fi

  rm -f "$PID_FILE"
  echo "Stopped"
}

cmd_restart() { cmd_stop; cmd_start; }

cmd_status() {
  if is_running; then
    echo "Running (pid $(cat "$PID_FILE")) on $HOST:$PORT"
    echo "Log: $LOG_FILE"
  else
    echo "Not running"
    [[ -f "$PID_FILE" ]] && echo "Stale pid file: $PID_FILE"
  fi
}

cmd_logs() {
  if [[ ! -f "$LOG_FILE" ]]; then
    echo "No log file yet ($LOG_FILE)"
    exit 1
  fi
  tail -n 100 -f "$LOG_FILE"
}

case "${1:-}" in
  start)   cmd_start ;;
  stop)    cmd_stop ;;
  restart) cmd_restart ;;
  status)  cmd_status ;;
  logs)    cmd_logs ;;
  *)
    echo "Usage: $0 {start|stop|restart|status|logs}" >&2
    exit 1
    ;;
esac
