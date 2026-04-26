#!/usr/bin/env bash
set -euo pipefail

LABEL="${LABEL:-com.imsg-agent.archive-monitor}"
REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
DATA_DIR="${IMSG_DATA_DIR:-$HOME/imsg-data}"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$LAUNCH_AGENTS_DIR/$LABEL.plist"
UV_BIN="${UV_BIN:-}"
NO_ATTACHMENTS=0
PRINT_ONLY=0

usage() {
  cat <<'EOF'
Usage: bash scripts/install_launchd.sh [--no-attachments] [--print-only]

Installs a user launchd agent that runs:
  uv run imsg-archive monitor

Environment overrides:
  LABEL=com.imsg-agent.archive-monitor
  REPO_DIR=/path/to/imsg-agent
  IMSG_DATA_DIR=~/imsg-data
  UV_BIN=/opt/homebrew/bin/uv
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-attachments)
      NO_ATTACHMENTS=1
      shift
      ;;
    --print-only)
      PRINT_ONLY=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "$UV_BIN" ]]; then
  UV_BIN="$(command -v uv || true)"
fi

if [[ -z "$UV_BIN" ]]; then
  echo "uv not found in PATH. Set UV_BIN=/path/to/uv and retry." >&2
  exit 1
fi

LOG_DIR="$DATA_DIR/logs"
STDOUT_LOG="$LOG_DIR/imsg-archive-monitor.log"
STDERR_LOG="$LOG_DIR/imsg-archive-monitor.err.log"
UID_VALUE="$(id -u)"

write_plist() {
  local target="$1"
  {
    cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$UV_BIN</string>
    <string>run</string>
    <string>imsg-archive</string>
    <string>monitor</string>
EOF
    if [[ "$NO_ATTACHMENTS" -eq 1 ]]; then
      cat <<'EOF'
    <string>--no-attachments</string>
EOF
    fi
    cat <<EOF
  </array>
  <key>WorkingDirectory</key>
  <string>$REPO_DIR</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$STDOUT_LOG</string>
  <key>StandardErrorPath</key>
  <string>$STDERR_LOG</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>IMSG_DATA_DIR</key>
    <string>$DATA_DIR</string>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
</dict>
</plist>
EOF
  } > "$target"
}

if [[ "$PRINT_ONLY" -eq 1 ]]; then
  tmp_plist="$(mktemp)"
  write_plist "$tmp_plist"
  cat "$tmp_plist"
  rm -f "$tmp_plist"
  exit 0
fi

mkdir -p "$LAUNCH_AGENTS_DIR" "$LOG_DIR"
write_plist "$PLIST_PATH"
plutil -lint "$PLIST_PATH"

launchctl bootout "gui/$UID_VALUE" "$PLIST_PATH" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$UID_VALUE" "$PLIST_PATH"
launchctl enable "gui/$UID_VALUE/$LABEL"
launchctl kickstart -k "gui/$UID_VALUE/$LABEL"

cat <<EOF
Installed and started $LABEL

Status:
  launchctl print gui/$UID_VALUE/$LABEL

Logs:
  tail -f "$STDOUT_LOG"
  tail -f "$STDERR_LOG"

Stop:
  launchctl bootout gui/$UID_VALUE "$PLIST_PATH"
EOF
