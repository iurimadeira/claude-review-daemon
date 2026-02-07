#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="claude-review-daemon"

# ── Colors ──────────────────────────────────────────────────────────────────

red()   { printf '\033[0;31m%s\033[0m\n' "$*"; }
green() { printf '\033[0;32m%s\033[0m\n' "$*"; }
yellow(){ printf '\033[0;33m%s\033[0m\n' "$*"; }
bold()  { printf '\033[1m%s\033[0m\n' "$*"; }

# ── 1. Pre-flight checks ───────────────────────────────────────────────────

if [ "$(id -u)" -eq 0 ]; then
  red "Error: Do not run this script as root."
  echo "Run as your normal user — the script will use sudo only where needed."
  exit 1
fi

bold "Checking prerequisites..."

check_bin() {
  local name="$1"
  local path
  path="$(command -v "$name" 2>/dev/null || true)"
  if [ -z "$path" ]; then
    red "  ✗ $name not found in PATH"
    return 1
  fi
  green "  ✓ $name → $path"
  echo "$path"
}

missing=0

PYTHON3=$(check_bin python3) || missing=1
CLAUDE=$(check_bin claude)   || missing=1
GH=$(check_bin gh)           || missing=1

if [ "$missing" -ne 0 ]; then
  red "Missing required tools. Install them and re-run."
  exit 1
fi

# Extract just the path (last line of check_bin output, which is the echo)
PYTHON3=$(command -v python3)
CLAUDE=$(command -v claude)
GH=$(command -v gh)

# Python version check
py_version=$("$PYTHON3" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
py_major=$("$PYTHON3" -c 'import sys; print(sys.version_info.major)')
py_minor=$("$PYTHON3" -c 'import sys; print(sys.version_info.minor)')

if [ "$py_major" -lt 3 ] || { [ "$py_major" -eq 3 ] && [ "$py_minor" -lt 11 ]; }; then
  red "  ✗ python3 version $py_version found, need >= 3.11"
  exit 1
fi
green "  ✓ python3 version $py_version"

# gh auth check
if ! "$GH" auth status &>/dev/null; then
  red "  ✗ gh is not authenticated. Run: gh auth login"
  exit 1
fi
green "  ✓ gh authenticated"

# Detect OS
OS="$(uname -s)"
case "$OS" in
  Linux)  green "  ✓ OS: Linux (systemd)" ;;
  Darwin) green "  ✓ OS: macOS (launchd)" ;;
  *)      red "Unsupported OS: $OS"; exit 1 ;;
esac

echo ""

# ── 2. Create .env ─────────────────────────────────────────────────────────

ENV_FILE="$SCRIPT_DIR/.env"

if [ -f "$ENV_FILE" ]; then
  yellow "Skipping .env (already exists)"
else
  bold "Creating .env..."
  GH_TOKEN=$("$GH" auth token)
  cat > "$ENV_FILE" <<EOF
GH_TOKEN=$GH_TOKEN
CONFIG_FILE=$SCRIPT_DIR/config.toml
REPO_DIR=$SCRIPT_DIR/repos
# SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T.../B.../...
EOF
  green "Created $ENV_FILE"
fi

# ── 3. Create config.toml ──────────────────────────────────────────────────

CONFIG_FILE="$SCRIPT_DIR/config.toml"

if [ -f "$CONFIG_FILE" ]; then
  yellow "Skipping config.toml (already exists)"
else
  bold "Creating config.toml from example..."
  cp "$SCRIPT_DIR/config.toml.example" "$CONFIG_FILE"
  green "Created $CONFIG_FILE"
fi

# ── 4. Create repos directory ──────────────────────────────────────────────

mkdir -p "$SCRIPT_DIR/repos"

# ── 5. Install service ─────────────────────────────────────────────────────

# Build a PATH that includes directories of all detected binaries
build_path() {
  local dirs=""
  for bin in "$PYTHON3" "$CLAUDE" "$GH"; do
    local dir
    dir="$(dirname "$bin")"
    case ":$dirs:" in
      *":$dir:"*) ;;
      *) dirs="${dirs:+$dirs:}$dir" ;;
    esac
  done
  # Append standard paths
  for std in /usr/local/bin /usr/bin /bin; do
    case ":$dirs:" in
      *":$std:"*) ;;
      *) dirs="$dirs:$std" ;;
    esac
  done
  echo "$dirs"
}

SVC_PATH=$(build_path)

if [ "$OS" = "Linux" ]; then
  # ── Linux: systemd ─────────────────────────────────────────────────────
  bold "Installing systemd service..."

  UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
  CURRENT_USER="$(whoami)"

  sudo tee "$UNIT_FILE" > /dev/null <<EOF
[Unit]
Description=Claude Review Daemon - Polling daemon for Claude Max PR reviews
After=network.target

[Service]
Type=simple
User=$CURRENT_USER
WorkingDirectory=$SCRIPT_DIR
ExecStart=$PYTHON3 $SCRIPT_DIR/bridge.py
Restart=always
RestartSec=5

TimeoutStopSec=300
KillSignal=SIGTERM

EnvironmentFile=$ENV_FILE
Environment=PATH=$SVC_PATH
Environment=ANTHROPIC_API_KEY=

[Install]
WantedBy=multi-user.target
EOF

  sudo systemctl daemon-reload
  sudo systemctl enable "$SERVICE_NAME"
  sudo systemctl restart "$SERVICE_NAME"

  echo ""
  green "Service installed and started."
  echo ""
  bold "Status:"
  sudo systemctl status "$SERVICE_NAME" --no-pager || true
  echo ""
  bold "Next steps:"
  echo "  1. Edit config.toml to add your repositories"
  echo "  2. Restart: sudo systemctl restart $SERVICE_NAME"
  echo "  3. Logs:    journalctl -u $SERVICE_NAME -f"

elif [ "$OS" = "Darwin" ]; then
  # ── macOS: launchd ─────────────────────────────────────────────────────
  bold "Installing launchd service..."

  PLIST_LABEL="com.${SERVICE_NAME}"
  PLIST_DIR="$HOME/Library/LaunchAgents"
  PLIST_FILE="$PLIST_DIR/${PLIST_LABEL}.plist"
  LOG_FILE="$SCRIPT_DIR/daemon.log"

  mkdir -p "$PLIST_DIR"

  # Source .env to get variables for the plist
  set -a
  source "$ENV_FILE"
  set +a

  cat > "$PLIST_FILE" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$PLIST_LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON3</string>
        <string>$SCRIPT_DIR/bridge.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$SCRIPT_DIR</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$SVC_PATH</string>
        <key>GH_TOKEN</key>
        <string>${GH_TOKEN:-}</string>
        <key>CONFIG_FILE</key>
        <string>${CONFIG_FILE:-$SCRIPT_DIR/config.toml}</string>
        <key>REPO_DIR</key>
        <string>${REPO_DIR:-$SCRIPT_DIR/repos}</string>
        <key>SLACK_WEBHOOK_URL</key>
        <string>${SLACK_WEBHOOK_URL:-}</string>
        <key>ANTHROPIC_API_KEY</key>
        <string></string>
    </dict>
    <key>KeepAlive</key>
    <true/>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$LOG_FILE</string>
    <key>StandardErrorPath</key>
    <string>$LOG_FILE</string>
</dict>
</plist>
EOF

  # Unload if already loaded, then load
  DOMAIN_TARGET="gui/$(id -u)"
  launchctl bootout "$DOMAIN_TARGET/$PLIST_LABEL" 2>/dev/null || true
  launchctl bootstrap "$DOMAIN_TARGET" "$PLIST_FILE"

  echo ""
  green "Service installed and started."
  echo ""
  bold "Status:"
  launchctl list | grep "$SERVICE_NAME" || yellow "  (service may still be starting)"
  echo ""
  bold "Next steps:"
  echo "  1. Edit config.toml to add your repositories"
  echo "  2. Restart: launchctl kickstart -k $DOMAIN_TARGET/$PLIST_LABEL"
  echo "  3. Logs:    tail -f $LOG_FILE"
  echo ""
  echo "  After changing .env, re-run install.sh to update the service."
fi
