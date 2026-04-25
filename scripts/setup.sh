#!/usr/bin/env bash
# setup.sh — Verify environment and create ~/imsg-data/ directory tree.
set -euo pipefail

DATA_DIR="${IMSG_DATA_DIR:-$HOME/imsg-data}"

resolve_imsg_binary() {
  if [ -n "${IMSG_BINARY:-}" ]; then
    if command -v "$IMSG_BINARY" >/dev/null 2>&1; then
      command -v "$IMSG_BINARY"
      return 0
    fi
    if [ -x "$IMSG_BINARY" ]; then
      printf '%s\n' "$IMSG_BINARY"
      return 0
    fi
    return 1
  fi

  command -v imsg
}

IMSG_BINARY="$(resolve_imsg_binary || true)"

echo "=== imsg-agent setup ==="
echo ""

# Check imsg binary
echo "→ Checking imsg binary..."
if [ -z "$IMSG_BINARY" ]; then
  echo "  ✗ imsg executable not found on PATH."
  echo "    Install imsg so 'command -v imsg' works, or set IMSG_BINARY=/path/to/imsg."
  exit 1
fi
echo "  ✓ imsg binary: $IMSG_BINARY"

# Check Full Disk Access by attempting to read chat.db header
echo "→ Checking Full Disk Access..."
DB_PATH="$HOME/Library/Messages/chat.db"
if ! "$IMSG_BINARY" chats --limit 1 >/dev/null 2>&1; then
  echo "  ✗ Cannot read Messages database."
  echo "    Open: System Settings → Privacy & Security → Full Disk Access"
  echo "    Add your terminal app, then restart terminal."
  exit 1
fi
echo "  ✓ Full Disk Access: OK"

# Create data directory tree
echo "→ Creating data directories..."
mkdir -p \
  "$DATA_DIR/inbox" \
  "$DATA_DIR/chats" \
  "$DATA_DIR/outbox/attachments" \
  "$DATA_DIR/sent" \
  "$DATA_DIR/errors" \
  "$DATA_DIR/digests" \
  "$DATA_DIR/nudges"

# Initialize state.json if absent
STATE_FILE="$DATA_DIR/state.json"
if [ ! -f "$STATE_FILE" ]; then
  echo '{"cursor": 0}' > "$STATE_FILE"
  echo "  ✓ Created $STATE_FILE"
else
  echo "  ✓ $STATE_FILE exists (cursor: $(python3 -c "import json; print(json.load(open('$STATE_FILE'))['cursor'])"))"
fi

echo ""
echo "=== Setup complete. Data directory: $DATA_DIR ==="
echo ""
echo "Next steps:"
echo "  1. Copy .env.example to .env and add OPENAI_API_KEY"
echo "  2. uv sync"
echo "  3. uv run python -m agent.main"
