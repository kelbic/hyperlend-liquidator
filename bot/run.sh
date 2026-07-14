#!/usr/bin/env bash
# Convenience launcher for local runs (not the systemd path). Sources ~/.hyperlend-bot/env if
# present, then runs the executor. Defaults to a single DRY_RUN pass — safe.
#   ./bot/run.sh once      # one DRY_RUN pass (default)
#   ./bot/run.sh validate  # exercise the full fire path on live near-edge candidates (DRY)
#   ./bot/run.sh loop      # continuous loop (respects DRY_RUN from env)
#   ./bot/run.sh reset     # clear kill-switch / dedup
set -euo pipefail
cd "$(dirname "$0")/.."
ENV=${HL_ENV:-$HOME/.hyperlend-bot/env}
[ -f "$ENV" ] && set -a && . "$ENV" && set +a
if [ "${1:-once}" = "validate" ]; then
  exec python3 -u -m bot.validate
fi
exec python3 -u -m bot.executor "${1:-once}"
