#!/usr/bin/env bash

# EDU-Mate 本地开发脚本。
#
# 这个脚本把前后端常用启动、测试、构建命令收拢到一个入口里，避免
# 每次联调都重新查 AGENTS.md。所有命令默认在项目根目录执行。

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEV_COMMAND="${1:-help}"

load_env_file() {
  local env_file="$ROOT_DIR/.env"
  if [[ -f "$env_file" ]]; then
    local name
    local -a preserved_names=()
    declare -A preserved_values=()
    while IFS='=' read -r name _; do
      if [[ "$name" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
        preserved_names+=("$name")
        preserved_values["$name"]="${!name-}"
      fi
    done < <(env)

    set -a
    # shellcheck source=/dev/null
    source "$env_file"
    set +a

    for name in "${preserved_names[@]}"; do
      export "$name=${preserved_values[$name]}"
    done
  fi
}

normalize_proxy_env() {
  if [[ "${EDUMATE_NORMALIZE_SOCKS_PROXY:-0}" != "1" ]]; then
    return 0
  fi

  local name value
  for name in HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy; do
    value="${!name-}"
    if [[ "$value" == socks://* ]]; then
      export "$name=socks5h://${value#socks://}"
      echo "Normalized $name from socks:// to socks5h:// for Python HTTP clients." >&2
    fi
  done
}

should_load_env_file() {
  case "$DEV_COMMAND" in
    backend|frontend|dev|app|mock|audio-stream|whisperlive-server|whisperlive-md|whisperlive-mic|llm-config|llm-smoke|rag-smoke|rebuild-global-index|build|install-rag)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

if should_load_env_file; then
  load_env_file
  normalize_proxy_env
fi

BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_HOST="${FRONTEND_HOST:-127.0.0.1}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
FRONTEND_PREVIEW_PORT="${FRONTEND_PREVIEW_PORT:-4173}"

PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
PIP_BIN="$ROOT_DIR/.venv/bin/pip"
UVICORN_BIN="$ROOT_DIR/.venv/bin/uvicorn"
export PATH="$ROOT_DIR/.venv/bin:$PATH"
OPENVINO_ROOT="${OPENVINO_ROOT:-/home/edu-mate_user/openvino}"
OPENVINO_PYTHON="${OPENVINO_PYTHON:-$OPENVINO_ROOT/venv/bin/python}"

usage() {
  cat <<EOF
EDU-Mate development helper

Usage:
  scripts/dev.sh <command>

Commands:
  app              First-run configure LLM, then run backend + built frontend
  backend          Start FastAPI backend with reload
  frontend         Start Vite frontend dev server
  dev              Start backend and frontend together
  test             Run backend tests and frontend tests
  backend-test     Run backend unittest suite
  frontend-test    Run frontend Vitest suite
  compile          Compile-check backend Python files
  build            Type-check and build frontend
  install-backend  Install backend Python dependencies
  install-rag      Install optional LlamaIndex/vector RAG dependencies
  desktop-shortcut Install/update EDU-Mate desktop launcher shortcut
  install-whisperlive
                   Install lightweight WhisperLive deps into OpenVINO Python
  mock             Send mock events to an existing frontend-created session
  audio-stream     Stream local test audio through OpenVINO Whisper/Qwen
  whisperlive-server
                   Start WhisperLive OpenVINO websocket server on iGPU
  whisperlive-md   Stream local audio to WhisperLive and periodically update Qwen notes
  whisperlive-mic  Stream ALSA microphone audio to WhisperLive and EDU-Mate
  llm-config       Configure backend LLM provider into .env
  llm-smoke        Manually test configured LLM provider with fixed classroom data
  rag-smoke        Smoke-test configured RAG backend and vector fallback status
  rebuild-global-index
                   Rebuild data/indexes/global documents snapshot

Environment:
  BACKEND_HOST     Backend host, default 127.0.0.1
  BACKEND_PORT     Backend port, default 8000
  FRONTEND_HOST    Frontend host, default 127.0.0.1
  FRONTEND_PORT    Frontend port, default 5173
  FRONTEND_PREVIEW_PORT
                   Built frontend preview port for app command, default 4173
  APP_OPEN_BROWSER Open browser in app command, default 1
  APP_BROWSER_DELAY_SECONDS
                   Delay before opening browser in app command, default 2
  APP_ENABLE_MIC   Start WhisperLive + microphone capture in app command, default 1
  APP_MIC_AUDIO_DEVICE
                   ALSA input device for app mic capture, default auto
  APP_MIC_LANGUAGE
                   Whisper language: auto, zh, en, etc.; default auto
  APP_MIC_NO_SPEECH_THRESH
                   Lower value filters silence/noise more aggressively, default 0.30
  APP_MIC_SAME_OUTPUT_THRESHOLD
                   Higher value makes final subtitles more stable, default 8
  APP_ENABLE_QWEN_NOTES
                   Keep structured_notes.md from microphone ASR, default 1
  APP_ENABLE_CLOUD_GRAPH
                   Send notes snapshots to cloud graph agent, default 1
  APP_MIC_LOOP_SESSIONS
                   Re-arm microphone after a classroom ends, default 1
  OPENVINO_ROOT    OpenVINO model/workspace root, default /home/edu-mate_user/openvino
  OPENVINO_PYTHON  Python with OpenVINO deps, default \$OPENVINO_ROOT/venv/bin/python

Examples:
  scripts/dev.sh app
  APP_ENABLE_MIC=0 scripts/dev.sh app
  APP_MIC_AUDIO_DEVICE=plughw:1,0 scripts/dev.sh app
  scripts/dev.sh desktop-shortcut
  scripts/dev.sh llm-config
  scripts/dev.sh llm-config --print-templates
  scripts/dev.sh dev
  scripts/dev.sh test
  scripts/dev.sh mock --session-id lec_xxx --no-end
  scripts/dev.sh audio-stream --max-audio-seconds 120 --whisper-device GPU --qwen-device CPU
  scripts/dev.sh install-whisperlive
  scripts/dev.sh install-rag
  scripts/dev.sh rag-smoke --require-llamaindex
  scripts/dev.sh whisperlive-server --port 9090
  scripts/dev.sh whisperlive-md --max-audio-seconds 300 --update-every-seconds 30
  scripts/dev.sh whisperlive-md --enable-cloud-graph --max-audio-seconds 300 --update-every-seconds 30 --graph-update-every-seconds 60
  scripts/dev.sh whisperlive-md --domain-terms "线性代数,矩阵,特征值" --max-audio-seconds 60 --update-every-seconds 20
  scripts/dev.sh whisperlive-md --whisperlive-model OpenVINO/whisper-medium-fp16-ov --max-audio-seconds 60 --fast-send
  scripts/dev.sh whisperlive-mic --enable-cloud-graph
  scripts/dev.sh whisperlive-mic --audio-device plughw:1,0 --language zh --no-qwen-notes
  scripts/dev.sh whisperlive-mic --partial-preview-interval 0.5 --same-output-threshold 2
  BACKEND_HOST=0.0.0.0 FRONTEND_HOST=0.0.0.0 scripts/dev.sh dev
EOF
}

require_backend_venv() {
  if [[ ! -x "$PYTHON_BIN" || ! -x "$UVICORN_BIN" ]]; then
    echo "Backend virtualenv is missing. Expected .venv/bin/python and .venv/bin/uvicorn." >&2
    echo "Create it and install dependencies first, or run: scripts/dev.sh install-backend" >&2
    exit 1
  fi
}

require_frontend_deps() {
  if [[ ! -d "$ROOT_DIR/frontend/node_modules" ]]; then
    echo "Frontend dependencies are missing. Run: cd frontend && npm install" >&2
    exit 1
  fi
}

require_openvino_python() {
  if [[ ! -x "$OPENVINO_PYTHON" ]]; then
    echo "OpenVINO Python is missing. Expected: $OPENVINO_PYTHON" >&2
    echo "Set OPENVINO_ROOT or OPENVINO_PYTHON to the local OpenVINO environment." >&2
    exit 1
  fi
}

run_backend() {
  require_backend_venv
  cd "$ROOT_DIR"
  exec "$UVICORN_BIN" backend.app.main:app \
    --reload \
    --reload-dir "$ROOT_DIR/backend" \
    --host "$BACKEND_HOST" \
    --port "$BACKEND_PORT"
}

run_frontend() {
  require_frontend_deps
  cd "$ROOT_DIR/frontend"
  exec npm run dev -- --host "$FRONTEND_HOST" --port "$FRONTEND_PORT"
}

run_dev() {
  require_backend_venv
  require_frontend_deps

  echo "Backend:  http://$BACKEND_HOST:$BACKEND_PORT"
  echo "Frontend: http://$FRONTEND_HOST:$FRONTEND_PORT"
  echo "Press Ctrl+C to stop both servers."

  cd "$ROOT_DIR"
  "$UVICORN_BIN" backend.app.main:app \
    --reload \
    --reload-dir "$ROOT_DIR/backend" \
    --host "$BACKEND_HOST" \
    --port "$BACKEND_PORT" &
  backend_pid=$!

  cd "$ROOT_DIR/frontend"
  npm run dev -- --host "$FRONTEND_HOST" --port "$FRONTEND_PORT" &
  frontend_pid=$!

  # 任意一个服务退出时，清理另一个后台进程，避免端口被旧进程占住。
  cleanup() {
    kill "$backend_pid" "$frontend_pid" 2>/dev/null || true
    wait "$backend_pid" "$frontend_pid" 2>/dev/null || true
  }
  trap cleanup EXIT INT TERM

  wait -n "$backend_pid" "$frontend_pid"
}

frontend_dist_is_stale() {
  local dist_index="$ROOT_DIR/frontend/dist/index.html"
  if [[ ! -f "$dist_index" || "${APP_REBUILD_FRONTEND:-0}" == "1" ]]; then
    return 0
  fi

  local newer_source
  newer_source="$(
    find \
      "$ROOT_DIR/frontend/src" \
      "$ROOT_DIR/frontend/public" \
      "$ROOT_DIR/frontend/index.html" \
      "$ROOT_DIR/frontend/package.json" \
      "$ROOT_DIR/frontend/package-lock.json" \
      "$ROOT_DIR/frontend/tsconfig.json" \
      "$ROOT_DIR/frontend/tsconfig.node.json" \
      "$ROOT_DIR/frontend/vite.config.ts" \
      -type f -newer "$dist_index" -print -quit 2>/dev/null || true
  )"
  [[ -n "$newer_source" ]]
}

run_app() {
  require_backend_venv
  require_frontend_deps

  cd "$ROOT_DIR"
  "$PYTHON_BIN" -m backend.scripts.configure_llm_provider
  load_env_file
  normalize_proxy_env

  if frontend_dist_is_stale; then
    echo "Building frontend for app mode..."
    run_build
  fi

  echo "Backend:  http://$BACKEND_HOST:$BACKEND_PORT"
  echo "Frontend: http://$FRONTEND_HOST:$FRONTEND_PREVIEW_PORT"
  echo "Press Ctrl+C to stop both servers."

  cd "$ROOT_DIR"
  "$UVICORN_BIN" backend.app.main:app \
    --host "$BACKEND_HOST" \
    --port "$BACKEND_PORT" &
  backend_pid=$!

  cd "$ROOT_DIR/frontend"
  npm run preview -- --host "$FRONTEND_HOST" --port "$FRONTEND_PREVIEW_PORT" &
  frontend_pid=$!

  whisperlive_pid=""
  microphone_pid=""
  start_app_microphone_stack

  browser_pid=""
  if [[ "${APP_OPEN_BROWSER:-1}" != "0" ]]; then
    (
      sleep "${APP_BROWSER_DELAY_SECONDS:-2}"
      if command -v xdg-open >/dev/null 2>&1; then
        xdg-open "$(app_browser_url)" >/dev/null 2>&1 || true
      fi
    ) &
    browser_pid=$!
  fi

  cleanup() {
    if [[ -n "$browser_pid" ]]; then
      kill "$browser_pid" 2>/dev/null || true
      wait "$browser_pid" 2>/dev/null || true
    fi
    kill "$backend_pid" "$frontend_pid" 2>/dev/null || true
    if [[ -n "$microphone_pid" ]]; then
      kill "$microphone_pid" 2>/dev/null || true
    fi
    if [[ -n "$whisperlive_pid" ]]; then
      kill "$whisperlive_pid" 2>/dev/null || true
    fi
    wait "$backend_pid" "$frontend_pid" 2>/dev/null || true
    if [[ -n "$microphone_pid" ]]; then
      wait "$microphone_pid" 2>/dev/null || true
    fi
    if [[ -n "$whisperlive_pid" ]]; then
      wait "$whisperlive_pid" 2>/dev/null || true
    fi
  }
  trap cleanup EXIT INT TERM

  wait -n "$backend_pid" "$frontend_pid"
}

start_app_microphone_stack() {
  if [[ "${APP_ENABLE_MIC:-1}" == "0" ]]; then
    echo "Microphone stack: disabled by APP_ENABLE_MIC=0"
    return 0
  fi
  if [[ ! -x "$OPENVINO_PYTHON" ]]; then
    echo "Microphone stack: skipped because OpenVINO Python is missing: $OPENVINO_PYTHON" >&2
    return 0
  fi

  local whisperlive_host="${APP_WHISPERLIVE_HOST:-127.0.0.1}"
  local whisperlive_connect_host="${APP_WHISPERLIVE_CONNECT_HOST:-127.0.0.1}"
  local whisperlive_port="${APP_WHISPERLIVE_PORT:-9090}"
  local audio_device="${APP_MIC_AUDIO_DEVICE:-auto}"
  local language="${APP_MIC_LANGUAGE:-auto}"
  local no_speech_thresh="${APP_MIC_NO_SPEECH_THRESH:-0.30}"
  local same_output_threshold="${APP_MIC_SAME_OUTPUT_THRESHOLD:-8}"
  local backend_url="${APP_MIC_BACKEND_URL:-$(app_backend_url)}"

  echo "WhisperLive: http://$whisperlive_connect_host:$whisperlive_port"
  echo "Microphone: device=$audio_device, language=$language"
  echo "ASR stability: no_speech_thresh=$no_speech_thresh, same_output_threshold=$same_output_threshold"
  echo "Microphone waits for the frontend to start a classroom session."

  local server_args=(
    backend/scripts/whisperlive_server.py
    --host "$whisperlive_host"
    --port "$whisperlive_port"
  )
  if [[ "${APP_WHISPERLIVE_ALLOW_CPU_FALLBACK:-0}" != "0" ]]; then
    server_args+=(--allow-cpu-fallback)
  fi

  cd "$ROOT_DIR"
  "$OPENVINO_PYTHON" "${server_args[@]}" &
  whisperlive_pid=$!

  if ! wait_for_tcp_port "$whisperlive_connect_host" "$whisperlive_port" "${APP_WHISPERLIVE_READY_TIMEOUT:-90}"; then
    echo "Microphone stack: WhisperLive did not become ready; microphone capture skipped." >&2
    return 0
  fi

  local mic_args=(
    backend/scripts/whisperlive_microphone.py
    --server "$whisperlive_connect_host"
    --port "$whisperlive_port"
    --backend-url "$backend_url"
    --session-id auto
    --wait-for-session
    --no-create-session
    --stop-when-session-ended
    --audio-device "$audio_device"
    --language "$language"
    --no-speech-thresh "$no_speech_thresh"
    --same-output-threshold "$same_output_threshold"
  )
  if [[ "${APP_ENABLE_QWEN_NOTES:-1}" == "0" ]]; then
    mic_args+=(--no-qwen-notes)
  fi
  if [[ "${APP_ENABLE_CLOUD_GRAPH:-1}" != "0" ]]; then
    mic_args+=(--enable-cloud-graph)
  fi
  if [[ -n "${APP_MIC_MAX_AUDIO_SECONDS:-}" ]]; then
    mic_args+=(--max-audio-seconds "$APP_MIC_MAX_AUDIO_SECONDS")
  fi
  if [[ -n "${APP_MIC_PARTIAL_PREVIEW_INTERVAL:-}" ]]; then
    mic_args+=(--partial-preview-interval "$APP_MIC_PARTIAL_PREVIEW_INTERVAL")
  fi
  if [[ -n "${APP_QWEN_MAX_SEGMENTS_PER_UPDATE:-}" ]]; then
    mic_args+=(--max-qwen-segments-per-update "$APP_QWEN_MAX_SEGMENTS_PER_UPDATE")
  fi
  (
    mic_child_pid=""
    trap 'if [[ -n "$mic_child_pid" ]]; then kill "$mic_child_pid" 2>/dev/null || true; wait "$mic_child_pid" 2>/dev/null || true; fi; exit 0' INT TERM
    while true; do
      "$OPENVINO_PYTHON" "${mic_args[@]}" &
      mic_child_pid=$!
      wait "$mic_child_pid"
      mic_status=$?
      mic_child_pid=""
      if [[ "${APP_MIC_LOOP_SESSIONS:-1}" == "0" || "$mic_status" -ne 0 ]]; then
        exit "$mic_status"
      fi
      echo "Microphone session finished; waiting for the next frontend classroom..."
      sleep 1
    done
  ) &
  microphone_pid=$!
}

wait_for_tcp_port() {
  local host="$1"
  local port="$2"
  local timeout_seconds="$3"
  local deadline=$((SECONDS + timeout_seconds))
  while (( SECONDS < deadline )); do
    if "$PYTHON_BIN" -c \
      'import socket, sys; s=socket.socket(); s.settimeout(0.5); s.connect((sys.argv[1], int(sys.argv[2]))); s.close()' \
      "$host" "$port" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

app_browser_url() {
  local host="$FRONTEND_HOST"
  if [[ "$host" == "0.0.0.0" || "$host" == "::" ]]; then
    host="127.0.0.1"
  fi
  printf 'http://%s:%s' "$host" "$FRONTEND_PREVIEW_PORT"
}

app_backend_url() {
  local host="$BACKEND_HOST"
  if [[ "$host" == "0.0.0.0" || "$host" == "::" ]]; then
    host="127.0.0.1"
  fi
  printf 'http://%s:%s' "$host" "$BACKEND_PORT"
}

run_backend_test() {
  require_backend_venv
  cd "$ROOT_DIR"
  "$PYTHON_BIN" -m unittest discover -s backend/tests
}

run_frontend_test() {
  require_frontend_deps
  cd "$ROOT_DIR/frontend"
  npm test
}

run_compile() {
  require_backend_venv
  cd "$ROOT_DIR"
  "$PYTHON_BIN" -m py_compile \
    backend/app/prompts.py \
    backend/app/main.py \
    backend/app/agent/*.py \
    backend/app/api/*.py \
    backend/app/core/*.py \
    backend/app/extraction/*.py \
    backend/app/llm/*.py \
    backend/app/models/*.py \
    backend/app/rag/*.py \
    backend/app/skills/*.py \
    backend/app/storage/*.py \
    backend/tests/*.py \
    backend/scripts/*.py
}

run_build() {
  require_frontend_deps
  cd "$ROOT_DIR/frontend"
  npm run build
}

run_install_backend() {
  if [[ ! -x "$PIP_BIN" ]]; then
    echo "Missing .venv/bin/pip. Create the virtualenv first:" >&2
    echo "  python -m venv .venv" >&2
    exit 1
  fi

  cd "$ROOT_DIR"
  "$PIP_BIN" install -r backend/requirements.txt
}

run_install_rag() {
  if [[ ! -x "$PIP_BIN" ]]; then
    echo "Missing .venv/bin/pip. Create the virtualenv first:" >&2
    echo "  python -m venv .venv" >&2
    exit 1
  fi

  cd "$ROOT_DIR"
  if [[ "${RAG_INSTALL_CPU_TORCH:-1}" != "0" ]]; then
    local torch_index_url="${RAG_TORCH_INDEX_URL:-https://download.pytorch.org/whl/cpu}"
    echo "Installing CPU-only torch first from: $torch_index_url"
    "$PIP_BIN" install --index-url "$torch_index_url" torch
  fi
  "$PIP_BIN" install -r backend/requirements-rag.txt
}

run_desktop_shortcut() {
  cd "$ROOT_DIR"
  "$ROOT_DIR/scripts/install_desktop_shortcut.sh"
}

run_install_whisperlive() {
  require_openvino_python
  cd "$ROOT_DIR"
  "$OPENVINO_PYTHON" -m pip install --no-deps "whisper-live==0.9.0"
  "$OPENVINO_PYTHON" -m pip install --upgrade-strategy only-if-needed \
    "fastapi" \
    "uvicorn" \
    "websockets" \
    "websocket-client" \
    "onnxruntime>=1.20,<2" \
    "python-multipart"
  echo
  echo "WhisperLive lightweight OpenVINO setup installed in: $OPENVINO_PYTHON"
  echo "WhisperLive microphone capture uses system ffmpeg + ALSA; no PyAudio dependency is required."
}

run_mock() {
  require_backend_venv
  cd "$ROOT_DIR"
  if [[ "$#" -eq 1 ]]; then
    echo "mock requires an existing frontend-created session_id." >&2
    echo "Start a classroom in the frontend first, then run:" >&2
    echo "  scripts/dev.sh mock --session-id REPLACE_WITH_SESSION_ID --no-end" >&2
    exit 1
  fi
  "$PYTHON_BIN" backend/scripts/mock_sender.py "${@:2}"
}

run_audio_stream() {
  require_openvino_python
  cd "$ROOT_DIR"
  "$OPENVINO_PYTHON" backend/scripts/local_audio_stream_sender.py "${@:2}"
}

run_whisperlive_server() {
  require_openvino_python
  cd "$ROOT_DIR"
  "$OPENVINO_PYTHON" backend/scripts/whisperlive_server.py "${@:2}"
}

run_whisperlive_md() {
  require_openvino_python
  cd "$ROOT_DIR"
  "$OPENVINO_PYTHON" backend/scripts/whisperlive_qwen_markdown.py "${@:2}"
}

run_whisperlive_mic() {
  require_openvino_python
  cd "$ROOT_DIR"
  "$OPENVINO_PYTHON" backend/scripts/whisperlive_microphone.py "${@:2}"
}

run_llm_smoke() {
  require_backend_venv
  cd "$ROOT_DIR"
  "$PYTHON_BIN" -m backend.scripts.llm_smoke
}

run_rag_smoke() {
  require_backend_venv
  cd "$ROOT_DIR"
  "$PYTHON_BIN" -m backend.scripts.rag_smoke "${@:2}"
}

run_rebuild_global_index() {
  require_backend_venv
  cd "$ROOT_DIR"
  "$PYTHON_BIN" -m backend.scripts.rebuild_global_index "${@:2}"
}

run_llm_config() {
  require_backend_venv
  cd "$ROOT_DIR"
  "$PYTHON_BIN" -m backend.scripts.configure_llm_provider "${@:2}"
}

command="$DEV_COMMAND"

case "$command" in
  app)
    run_app
    ;;
  backend)
    run_backend
    ;;
  frontend)
    run_frontend
    ;;
  dev)
    run_dev
    ;;
  test)
    run_backend_test
    run_frontend_test
    ;;
  backend-test)
    run_backend_test
    ;;
  frontend-test)
    run_frontend_test
    ;;
  compile)
    run_compile
    ;;
  build)
    run_build
    ;;
  install-backend)
    run_install_backend
    ;;
  install-rag)
    run_install_rag
    ;;
  desktop-shortcut)
    run_desktop_shortcut
    ;;
  install-whisperlive)
    run_install_whisperlive
    ;;
  mock)
    run_mock "$@"
    ;;
  audio-stream)
    run_audio_stream "$@"
    ;;
  whisperlive-server)
    run_whisperlive_server "$@"
    ;;
  whisperlive-md)
    run_whisperlive_md "$@"
    ;;
  whisperlive-mic)
    run_whisperlive_mic "$@"
    ;;
  llm-config)
    run_llm_config "$@"
    ;;
  llm-smoke)
    run_llm_smoke
    ;;
  rag-smoke)
    run_rag_smoke "$@"
    ;;
  rebuild-global-index)
    run_rebuild_global_index "$@"
    ;;
  help|-h|--help)
    usage
    ;;
  *)
    echo "Unknown command: $command" >&2
    echo >&2
    usage >&2
    exit 1
    ;;
esac
