#!/usr/bin/env bash
set -euo pipefail

# cron's default PATH is /usr/bin:/bin — uv lives in /opt/homebrew/bin
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:${PATH}"

ROOT="${HOME}/src/imsg-agent"
LOG_DIR="${HOME}/imsg-data/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/draft-review.log"

# capture stderr too — invisible cron failures are how we lost 81 days of DMARC
exec 2>> "$LOG_FILE"

cd "$ROOT"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] draft review pass" >> "$LOG_FILE"
uv run imsg-draft-review >> "$LOG_FILE"
