#!/usr/bin/env bash
# ============================================================================
# dev.sh — Onyx full-stack dev environment: bootstrap + launch + watch
# ============================================================================
#
# USAGE
#   ./dev.sh                     Full bootstrap + start everything
#   ./dev.sh --skip-docker       Skip Docker service startup (already running)
#   ./dev.sh --skip-migrate      Skip Alembic migrations (already applied)
#   ./dev.sh --skip-bootstrap    Skip toolchain install (uv, venv, npm)
#   ./dev.sh --no-celery         Don't start Celery workers (lighter dev loop)
#   ./dev.sh --with-opensearch   Also start OpenSearch (extra ~2GB RAM)
#   ./dev.sh --reset             Wipe Docker volumes, re-create from scratch
#   ./dev.sh --help              Show this message
#
# WATCH BEHAVIOUR
#   Frontend (Next.js)   → built-in HMR via npm run dev
#   API server (uvicorn) → --reload watches backend/onyx/**/*.py
#   Model server         → --reload watches backend/model_server/**/*.py
#   Celery workers       → watchmedo auto-restart on backend/onyx/**/*.py
#
# LOGS
#   All service output → backend/log/<service>_debug.log
#   Also tailed live in this terminal. Ctrl+C stops everything.
#
# LOGIN
#   http://localhost:3000  user: a@example.com  password: a
# ============================================================================

set -euo pipefail

# ── paths ────────────────────────────────────────────────────────────────────
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND="$ROOT/backend"
WEB="$ROOT/web"
COMPOSE_DIR="$ROOT/deployment/docker_compose"
LOG_DIR="$BACKEND/log"
VENV="$ROOT/.venv/bin/activate"
ENV_FILE="$ROOT/.vscode/.env"
ENV_TEMPLATE="$ROOT/.vscode/env_template.txt"

# ── colours ──────────────────────────────────────────────────────────────────
R='\033[0;31m'  # red
G='\033[0;32m'  # green
Y='\033[1;33m'  # yellow
B='\033[0;34m'  # blue
C='\033[0;36m'  # cyan
M='\033[0;35m'  # magenta
W='\033[1;37m'  # white/bold
DIM='\033[2m'   # dim
NC='\033[0m'    # reset

# ── flags ────────────────────────────────────────────────────────────────────
SKIP_DOCKER=false
SKIP_MIGRATE=false
SKIP_BOOTSTRAP=false
NO_CELERY=false
WITH_OPENSEARCH=false
RESET=false

for arg in "$@"; do
  case "$arg" in
    --skip-docker)    SKIP_DOCKER=true    ;;
    --skip-migrate)   SKIP_MIGRATE=true   ;;
    --skip-bootstrap) SKIP_BOOTSTRAP=true ;;
    --no-celery)      NO_CELERY=true      ;;
    --with-opensearch) WITH_OPENSEARCH=true ;;
    --reset)          RESET=true          ;;
    --help|-h)
      sed -n '2,/^# ====/{ /^# ====/d; s/^# \?//p; }' "$0"
      exit 0
      ;;
    *) echo -e "${R}Unknown option: $arg${NC}"; exit 1 ;;
  esac
done

# ── PID tracking + cleanup ───────────────────────────────────────────────────
declare -a PIDS=()

cleanup() {
  echo ""
  echo -e "${Y}[dev.sh] Shutting down all services...${NC}"
  for pid in "${PIDS[@]:-}"; do
    kill -- -"$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true
  done
  pkill -P $$ 2>/dev/null || true
  wait 2>/dev/null || true
  echo -e "${G}[dev.sh] All stopped.${NC}"
}
trap cleanup SIGINT SIGTERM EXIT

# ── helpers ──────────────────────────────────────────────────────────────────
log()   { echo -e "${W}[dev.sh]${NC} $*"; }
ok()    { echo -e "${G}  OK${NC}  $*"; }
warn()  { echo -e "${Y}  !!${NC}  $*"; }
die()   { echo -e "${R}  XX  $*${NC}" >&2; exit 1; }
hr()    { echo -e "${DIM}$(printf '%.0s─' {1..60})${NC}"; }

wait_for_port() {
  local port="$1" name="$2" retries="${3:-30}"
  local i=0
  while ! nc -z localhost "$port" 2>/dev/null; do
    i=$((i + 1))
    if [ "$i" -ge "$retries" ]; then
      warn "$name not ready on port $port after ${retries}s — continuing anyway"
      return 1
    fi
    sleep 1
  done
  ok "$name ready on port $port"
}

# Launch a background service, log to file + stream with coloured prefix
launch_service() {
  local color="$1" label="$2" logfile="$3"
  shift 3
  mkdir -p "$LOG_DIR"
  > "$logfile"  # truncate old log

  # Run in a new process group (setsid) so cleanup can kill the whole tree
  bash -c "$*" >> "$logfile" 2>&1 &
  local pid=$!
  PIDS+=("$pid")
  echo -e "${color}  >>  ${label}${NC}  pid=$pid  log=backend/log/$(basename "$logfile")"
}

# ════════════════════════════════════════════════════════════════════════════
#  PHASE 1: BOOTSTRAP (toolchain + dependencies)
# ════════════════════════════════════════════════════════════════════════════

if ! $SKIP_BOOTSTRAP; then
  log "Phase 1/4: Bootstrapping toolchain..."
  hr

  # ── Docker ──────────────────────────────────────────────────────────────
  command -v docker >/dev/null 2>&1 || die "Docker not found. Install Docker Desktop: https://docker.com/products/docker-desktop"
  docker info >/dev/null 2>&1     || die "Docker daemon not running. Start Docker Desktop first."
  ok "Docker $(docker --version | awk '{print $3}' | tr -d ',')"

  # ── uv (Python package manager) ────────────────────────────────────────
  if ! command -v uv >/dev/null 2>&1; then
    log "Installing uv (Astral Python package manager)..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # Add to current shell
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    command -v uv >/dev/null 2>&1 || die "uv installation failed"
    ok "uv installed: $(uv --version)"
  else
    ok "uv $(uv --version 2>/dev/null || echo '(installed)')"
  fi

  # ── Python 3.11 venv ───────────────────────────────────────────────────
  if [[ ! -f "$VENV" ]]; then
    log "Creating Python 3.11 virtual environment (uv will fetch 3.11 if needed)..."
    uv venv "$ROOT/.venv" --python 3.11
    ok ".venv created with Python 3.11"
  else
    ok ".venv exists"
  fi
  # shellcheck source=/dev/null
  source "$VENV"

  # ── Python dependencies ────────────────────────────────────────────────
  log "Installing Python dependencies..."
  (cd "$ROOT" && uv sync --all-extras)
  ok "Python deps installed"

  # ── Playwright browsers (needed by Web connector) ──────────────────────
  if ! python -c "from playwright.sync_api import sync_playwright" 2>/dev/null; then
    log "Installing Playwright browsers..."
    uv run playwright install
    ok "Playwright browsers installed"
  else
    ok "Playwright browsers present"
  fi

  # ── watchdog (for Celery hot-reload) ───────────────────────────────────
  if ! command -v watchmedo >/dev/null 2>&1; then
    log "Installing watchdog (for Celery file-watching)..."
    pip install watchdog
    ok "watchdog installed"
  else
    ok "watchmedo available"
  fi

  # ── pre-commit hooks ───────────────────────────────────────────────────
  if [[ ! -f "$ROOT/.git/hooks/pre-commit" ]]; then
    log "Installing pre-commit hooks..."
    uv run pre-commit install
    ok "pre-commit hooks installed"
  else
    ok "pre-commit hooks present"
  fi

  # ── Node.js ────────────────────────────────────────────────────────────
  if ! command -v node >/dev/null 2>&1; then
    die "Node.js not found. Install via: nvm install 22 && nvm use 22"
  fi
  NODE_MAJOR=$(node -v | sed 's/v\([0-9]*\).*/\1/')
  if [[ "$NODE_MAJOR" -lt 20 ]]; then
    warn "Node $(node -v) detected — v20+ recommended (v22 preferred)"
    warn "Run: nvm install 22 && nvm use 22"
  else
    ok "Node $(node -v)"
  fi

  # ── npm dependencies ───────────────────────────────────────────────────
  if [[ ! -d "$WEB/node_modules" ]] || [[ "$WEB/package.json" -nt "$WEB/node_modules/.package-lock.json" ]]; then
    log "Installing frontend dependencies..."
    (cd "$WEB" && npm install)
    ok "npm deps installed"
  else
    ok "npm deps up to date"
  fi

  # ── .vscode/.env (config file) ─────────────────────────────────────────
  if [[ ! -f "$ENV_FILE" ]]; then
    log "Creating .vscode/.env from template..."
    mkdir -p "$ROOT/.vscode"
    # Copy template, fill in safe defaults, and quote unset placeholders
    # so `source .env` doesn't choke on unquoted spaces like <REPLACE THIS>
    sed \
      -e "s|SAML_CONF_DIR=.*|SAML_CONF_DIR=$ROOT/backend/ee/onyx/configs/saml_config|" \
      -e "s|OPENSEARCH_INITIAL_ADMIN_PASSWORD=.*|OPENSEARCH_INITIAL_ADMIN_PASSWORD=StrongPassword123!|" \
      -e 's|=<REPLACE THIS>|=""|g' \
      -e 's|=<REPLACE_THIS>|=""|g' \
      "$ENV_TEMPLATE" > "$ENV_FILE"
    # Disable OpenSearch by default — most dev work only needs Vespa
    echo "" >> "$ENV_FILE"
    echo "# Disable OpenSearch for local dev (use --with-opensearch flag to enable)" >> "$ENV_FILE"
    echo "ENABLE_OPENSEARCH_INDEXING_FOR_ONYX=false" >> "$ENV_FILE"
    ok ".vscode/.env created from template (OpenSearch disabled)"
    warn "Edit .vscode/.env to add your GEN_AI_API_KEY / OPENAI_API_KEY for LLM features"
  else
    ok ".vscode/.env exists"
  fi

  hr
  echo ""
else
  ok "Bootstrap skipped (--skip-bootstrap)"
  # Still need to activate venv
  [[ -f "$VENV" ]] || die ".venv not found — run without --skip-bootstrap first"
  # shellcheck source=/dev/null
  source "$VENV"
fi

# ════════════════════════════════════════════════════════════════════════════
#  PHASE 2: DOCKER (external services)
# ════════════════════════════════════════════════════════════════════════════

if $RESET; then
  log "Phase 2/4: Resetting Docker volumes (--reset)..."
  hr
  bash "$BACKEND/scripts/restart_containers.sh"
  SKIP_DOCKER=true
  SKIP_MIGRATE=true
  ok "Docker volumes wiped and services restarted"
  hr
  echo ""
fi

if ! $SKIP_DOCKER; then
  log "Phase 2/4: Starting Docker services..."
  hr

  COMPOSE_SERVICES="index relational_db cache minio"
  COMPOSE_ARGS=(-f "$COMPOSE_DIR/docker-compose.yml" -f "$COMPOSE_DIR/docker-compose.dev.yml")

  if $WITH_OPENSEARCH; then
    COMPOSE_SERVICES="$COMPOSE_SERVICES opensearch"
    COMPOSE_ARGS+=(--profile opensearch-enabled)
    ok "OpenSearch included (--with-opensearch)"
  fi

  docker compose "${COMPOSE_ARGS[@]}" up -d $COMPOSE_SERVICES 2>&1

  ok "Docker containers starting"

  # Wait for critical services
  wait_for_port 5432 "PostgreSQL"
  wait_for_port 6379 "Redis"
  wait_for_port 9004 "MinIO"
  # Vespa takes longer to boot
  wait_for_port 19071 "Vespa config" 60

  if $WITH_OPENSEARCH; then
    wait_for_port 9200 "OpenSearch" 60
  fi

  hr
  echo ""
else
  ok "Skipping Docker (--skip-docker)"
fi

# ════════════════════════════════════════════════════════════════════════════
#  PHASE 3: DATABASE MIGRATIONS
# ════════════════════════════════════════════════════════════════════════════

if ! $SKIP_MIGRATE; then
  log "Phase 3/4: Running Alembic migrations..."
  hr
  (cd "$BACKEND" && alembic upgrade head)
  ok "Database migrated to head"
  hr
  echo ""
else
  ok "Skipping migrations (--skip-migrate)"
fi

# ════════════════════════════════════════════════════════════════════════════
#  PHASE 4: START SERVICES (with watch/reload)
# ════════════════════════════════════════════════════════════════════════════

log "Phase 4/4: Launching services with file-watching..."
hr

# Build the env-loading preamble used by each Python service
ENV_LOAD="set -a; [[ -f '$ENV_FILE' ]] && source '$ENV_FILE'; set +a; source '$VENV'; cd '$BACKEND'"

# -- 1. Frontend (Next.js — HMR built in) ------------------------------------
launch_service "$C" "web" "$LOG_DIR/web_server_debug.log" \
  "cd '$WEB' && npm run dev"

# -- 2. Model server (uvicorn --reload) ---------------------------------------
launch_service "$B" "model-server" "$LOG_DIR/model_server_debug.log" \
  "$ENV_LOAD; uvicorn model_server.main:app --reload --reload-dir '$BACKEND/model_server' --port 9000"

# -- 3. API server (uvicorn --reload) -----------------------------------------
launch_service "$M" "api-server" "$LOG_DIR/api_server_debug.log" \
  "$ENV_LOAD; AUTH_TYPE=\${AUTH_TYPE:-basic} uvicorn onyx.main:app --reload --reload-dir '$BACKEND/onyx' --port 8080"

# -- 4. Celery workers (watchmedo auto-restart or plain) ----------------------
if ! $NO_CELERY; then
  WATCHMEDO_BIN="$(command -v watchmedo 2>/dev/null || true)"
  if [[ -n "$WATCHMEDO_BIN" ]]; then
    launch_service "$Y" "celery" "$LOG_DIR/celery_debug.log" \
      "$ENV_LOAD; watchmedo auto-restart --directory='$BACKEND/onyx' --pattern='*.py' --recursive -- python ./scripts/dev_run_background_jobs.py"
    ok "Celery: file-watching enabled (watchmedo)"
  else
    launch_service "$Y" "celery" "$LOG_DIR/celery_debug.log" \
      "$ENV_LOAD; python ./scripts/dev_run_background_jobs.py"
    warn "Celery: no file-watching (install watchdog for auto-restart)"
  fi
else
  warn "Celery workers skipped (--no-celery)"
fi

hr
echo ""

# ════════════════════════════════════════════════════════════════════════════
#  STATUS BANNER
# ════════════════════════════════════════════════════════════════════════════

cat <<EOF

$(echo -e "${G}")━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━$(echo -e "${NC}")
$(echo -e "${W}")  Onyx Dev Environment$(echo -e "${NC}")
$(echo -e "${G}")━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━$(echo -e "${NC}")

  $(echo -e "${C}")Frontend${NC}       http://localhost:3000
  $(echo -e "${M}")API Server${NC}     http://localhost:8080
  $(echo -e "${B}")Model Server${NC}   http://localhost:9000

  $(echo -e "${DIM}")Login:  a@example.com / a${NC}
  $(echo -e "${DIM}")Logs:   backend/log/<service>_debug.log${NC}
  $(echo -e "${DIM}")Stop:   Ctrl+C${NC}

$(echo -e "${G}")━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━$(echo -e "${NC}")

  $(echo -e "${W}")Watch mode active:${NC}
    backend/onyx/**/*.py         → API server + Celery auto-restart
    backend/model_server/**/*.py → Model server auto-restart
    web/src/**/*                 → Next.js HMR (instant)

EOF

# ════════════════════════════════════════════════════════════════════════════
#  TAIL LOGS (coloured, multiplexed)
# ════════════════════════════════════════════════════════════════════════════

sleep 2

# Create empty log files if they don't exist yet (tail -f needs them)
for f in web_server_debug.log api_server_debug.log model_server_debug.log celery_debug.log; do
  touch "$LOG_DIR/$f"
done

log "Streaming logs — Ctrl+C to stop everything"
echo ""

# Use tail -f on all logs. Prefix each line with the service name.
# This gives a unified, coloured log stream in one terminal.
(
  tail -f "$LOG_DIR/web_server_debug.log"   2>/dev/null | sed -u "s/^/$(printf '\033[0;36m')[web]$(printf '\033[0m')     /" &
  tail -f "$LOG_DIR/api_server_debug.log"   2>/dev/null | sed -u "s/^/$(printf '\033[0;35m')[api]$(printf '\033[0m')     /" &
  tail -f "$LOG_DIR/model_server_debug.log" 2>/dev/null | sed -u "s/^/$(printf '\033[0;34m')[model]$(printf '\033[0m')   /" &
  tail -f "$LOG_DIR/celery_debug.log"       2>/dev/null | sed -u "s/^/$(printf '\033[1;33m')[celery]$(printf '\033[0m')  /" &
  wait
) &
TAIL_PID=$!
PIDS+=("$TAIL_PID")

# Block until a signal kills us
wait
