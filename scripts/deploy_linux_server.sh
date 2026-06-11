#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage:
  scripts/deploy_linux_server.sh [options]

Options:
  --project-dir DIR       AutoFluid project directory. Default: parent of this script.
  --repo-url URL          Optional git URL. Cloned when project dir is missing.
  --python EXE            Python executable used to create .venv. Default: python3.
  --service-name NAME     systemd service name. Default: autofluid-daemon.
  --service-user USER     Linux user that runs the daemon. Default: current user.
  --ipc-host HOST         AUTOFLUID_IPC_HOST for new .env template. Default: 127.0.0.1.
  --ipc-port PORT         AUTOFLUID_IPC_PORT for new .env template. Default: 9527.
  --no-systemd            Skip systemd unit installation.
  --start                 Start or restart the systemd service after deployment.
  --help                  Show this help.

Environment overrides:
  AUTOFLUID_DEPLOY_PROJECT_DIR
  AUTOFLUID_DEPLOY_REPO_URL
  AUTOFLUID_DEPLOY_PYTHON
  AUTOFLUID_DEPLOY_SERVICE_NAME
  AUTOFLUID_DEPLOY_SERVICE_USER
  AUTOFLUID_DEPLOY_IPC_HOST
  AUTOFLUID_DEPLOY_IPC_PORT
  AUTOFLUID_DEPLOY_NO_SYSTEMD=1
  AUTOFLUID_DEPLOY_START=1
EOF
}

log() {
  printf '[AutoFluid deploy] %s\n' "$*"
}

die() {
  printf '[AutoFluid deploy] ERROR: %s\n' "$*" >&2
  exit 1
}

quote_systemd_value() {
  local value=$1
  value=${value//\\/\\\\}
  value=${value//\"/\\\"}
  printf '"%s"' "$value"
}

resolve_path() {
  local path=$1
  if command -v realpath >/dev/null 2>&1; then
    realpath "$path"
  else
    (cd "$path" 2>/dev/null && pwd -P) || return 1
  fi
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
if [[ -n "${AUTOFLUID_DEPLOY_PROJECT_DIR:-}" ]]; then
  PROJECT_DIR=$AUTOFLUID_DEPLOY_PROJECT_DIR
elif [[ -f "$SCRIPT_DIR/../start_daemon.py" ]]; then
  PROJECT_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd -P)
else
  PROJECT_DIR="$HOME/AutoFluidSimulation"
fi
REPO_URL=${AUTOFLUID_DEPLOY_REPO_URL:-}
PYTHON_EXE=${AUTOFLUID_DEPLOY_PYTHON:-python3}
SERVICE_NAME=${AUTOFLUID_DEPLOY_SERVICE_NAME:-autofluid-daemon}
DEFAULT_SERVICE_USER=$(id -un)
if [[ "$DEFAULT_SERVICE_USER" == "root" && -n "${SUDO_USER:-}" ]]; then
  DEFAULT_SERVICE_USER=$SUDO_USER
fi
SERVICE_USER=${AUTOFLUID_DEPLOY_SERVICE_USER:-$DEFAULT_SERVICE_USER}
IPC_HOST=${AUTOFLUID_DEPLOY_IPC_HOST:-127.0.0.1}
IPC_PORT=${AUTOFLUID_DEPLOY_IPC_PORT:-9527}
INSTALL_SYSTEMD=1
START_SERVICE=${AUTOFLUID_DEPLOY_START:-0}

if [[ "${AUTOFLUID_DEPLOY_NO_SYSTEMD:-0}" == "1" ]]; then
  INSTALL_SYSTEMD=0
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project-dir)
      [[ $# -ge 2 ]] || die "--project-dir requires a value"
      PROJECT_DIR=$2
      shift 2
      ;;
    --repo-url)
      [[ $# -ge 2 ]] || die "--repo-url requires a value"
      REPO_URL=$2
      shift 2
      ;;
    --python)
      [[ $# -ge 2 ]] || die "--python requires a value"
      PYTHON_EXE=$2
      shift 2
      ;;
    --service-name)
      [[ $# -ge 2 ]] || die "--service-name requires a value"
      SERVICE_NAME=$2
      shift 2
      ;;
    --service-user)
      [[ $# -ge 2 ]] || die "--service-user requires a value"
      SERVICE_USER=$2
      shift 2
      ;;
    --ipc-host)
      [[ $# -ge 2 ]] || die "--ipc-host requires a value"
      IPC_HOST=$2
      shift 2
      ;;
    --ipc-port)
      [[ $# -ge 2 ]] || die "--ipc-port requires a value"
      IPC_PORT=$2
      shift 2
      ;;
    --no-systemd)
      INSTALL_SYSTEMD=0
      shift
      ;;
    --start)
      START_SERVICE=1
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      die "Unknown option: $1"
      ;;
  esac
done

case "$IPC_PORT" in
  ''|*[!0-9]*) die "IPC port must be an integer: $IPC_PORT" ;;
esac
if (( IPC_PORT < 1 || IPC_PORT > 65535 )); then
  die "IPC port out of range: $IPC_PORT"
fi

if [[ ! -d "$PROJECT_DIR" ]]; then
  if [[ -z "$REPO_URL" ]]; then
    die "Project directory does not exist: $PROJECT_DIR. Provide --repo-url or copy the repository first."
  fi
  require_command git
  log "Cloning repository into $PROJECT_DIR"
  mkdir -p "$(dirname -- "$PROJECT_DIR")"
  git clone "$REPO_URL" "$PROJECT_DIR"
fi

PROJECT_DIR=$(resolve_path "$PROJECT_DIR") || die "Project directory does not exist: $PROJECT_DIR"
cd "$PROJECT_DIR"

[[ -f start_daemon.py ]] || die "start_daemon.py not found in project directory: $PROJECT_DIR"
[[ -f requirements.txt ]] || die "requirements.txt not found in project directory: $PROJECT_DIR"

require_command "$PYTHON_EXE"
id "$SERVICE_USER" >/dev/null 2>&1 || die "Service user does not exist: $SERVICE_USER"
SERVICE_GROUP=$(id -gn "$SERVICE_USER")

log "Project: $PROJECT_DIR"
log "Python: $("$PYTHON_EXE" --version 2>&1)"

log "Creating runtime directories"
mkdir -p logs data data/scdoc
chmod 750 logs data data/scdoc
find scripts -maxdepth 1 -type f -name '*.sh' -exec chmod 750 {} \; 2>/dev/null || true

log "Creating or refreshing .venv"
"$PYTHON_EXE" -m venv .venv
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"
[[ -x "$VENV_PYTHON" ]] || die "Virtualenv Python was not created: $VENV_PYTHON"

log "Upgrading pip tooling"
"$VENV_PYTHON" -m pip install --upgrade pip setuptools wheel

log "Installing runtime dependencies"
"$VENV_PYTHON" -m pip install -r requirements.txt

if [[ -f requirements-dev.txt ]]; then
  log "Installing development dependencies for diagnostics"
  "$VENV_PYTHON" -m pip install -r requirements-dev.txt
fi

if [[ ! -f .env ]]; then
  log "Creating .env template"
  cat > .env <<EOF
AUTOFLUID_SERVER_MODE=server
AUTOFLUID_IPC_HOST=${IPC_HOST}
AUTOFLUID_IPC_PORT=${IPC_PORT}

# Required before exposing IPC beyond localhost or using LocalWorker/TUI.
AUTOFLUID_IPC_AUTH_TOKEN=

# Required before transfer/meshing/solver can connect to the Windows workstation.
AUTOFLUID_SSH_PASSWORD=

# If the Linux server reaches the workstation through a tunnel/VPN/public route,
# set these to the server-reachable endpoint.
AUTOFLUID_SSH_REACHABLE_HOST=
AUTOFLUID_SSH_REACHABLE_PORT=
AUTOFLUID_SSH_CONNECTIVITY_MODE=
EOF
  chmod 600 .env
else
  log ".env already exists; leaving it unchanged"
fi

if [[ "$(id -u)" -eq 0 ]]; then
  log "Setting runtime ownership to ${SERVICE_USER}:${SERVICE_GROUP}"
  chown -R "${SERVICE_USER}:${SERVICE_GROUP}" .venv logs data
  [[ -f .env ]] && chown "${SERVICE_USER}:${SERVICE_GROUP}" .env
fi

if [[ "$INSTALL_SYSTEMD" == "1" ]]; then
  require_command systemctl
  UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
  TMP_UNIT=$(mktemp)
  PROJECT_DIR_SYSTEMD=$(quote_systemd_value "$PROJECT_DIR")
  VENV_PYTHON_SYSTEMD=$(quote_systemd_value "$VENV_PYTHON")
  ENV_FILE_SYSTEMD=$(quote_systemd_value "$PROJECT_DIR/.env")

  log "Writing systemd unit: $UNIT_PATH"
  cat > "$TMP_UNIT" <<EOF
[Unit]
Description=AutoFluid Pipeline Daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${PROJECT_DIR_SYSTEMD}
Environment=PYTHONUNBUFFERED=1
Environment=AUTOFLUID_SERVER_MODE=server
EnvironmentFile=-${ENV_FILE_SYSTEMD}
ExecStart=${VENV_PYTHON_SYSTEMD} start_daemon.py
Restart=on-failure
RestartSec=5
UMask=0027

[Install]
WantedBy=multi-user.target
EOF

  if [[ "$(id -u)" -eq 0 ]]; then
    install -m 0644 "$TMP_UNIT" "$UNIT_PATH"
    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME"
    if [[ "$START_SERVICE" == "1" ]]; then
      systemctl restart "$SERVICE_NAME"
    fi
  else
    require_command sudo
    sudo install -m 0644 "$TMP_UNIT" "$UNIT_PATH"
    sudo systemctl daemon-reload
    sudo systemctl enable "$SERVICE_NAME"
    if [[ "$START_SERVICE" == "1" ]]; then
      sudo systemctl restart "$SERVICE_NAME"
    fi
  fi
  rm -f "$TMP_UNIT"
else
  log "Skipping systemd unit installation"
fi

log "Running Python import smoke check"
"$VENV_PYTHON" - <<'PY'
from engine.config import IPC_CONFIG, is_server_mode
from engine.daemon import PipelineDaemon

print(f"server_mode={is_server_mode()}")
print(f"ipc={IPC_CONFIG['host']}:{IPC_CONFIG['port']}")
print(f"daemon_class={PipelineDaemon.__name__}")
PY

cat <<EOF

Deployment complete.

Next checks:
  1. Edit ${PROJECT_DIR}/.env and set AUTOFLUID_IPC_AUTH_TOKEN and AUTOFLUID_SSH_PASSWORD.
  2. Verify the server can reach the workstation SSH endpoint from this Linux host.
  3. Start daemon:
       sudo systemctl restart ${SERVICE_NAME}
  4. Inspect status/logs:
       systemctl status ${SERVICE_NAME} --no-pager
       journalctl -u ${SERVICE_NAME} -f

Manual foreground start:
  cd ${PROJECT_DIR}
  AUTOFLUID_SERVER_MODE=server .venv/bin/python start_daemon.py
EOF
