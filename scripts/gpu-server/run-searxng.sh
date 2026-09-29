#!/usr/bin/env bash
# Start the SearXNG installed by setup-searxng.sh in a detached tmux session.
#   run-searxng.sh          listens on 127.0.0.1:18888 (settings.yml)
#   stop: la-services stop search (not tmux kill-session: granian takes its SIGHUP as "reload" and stays)
# LocalAgent on this machine: la web set --search searxng --url http://127.0.0.1:18888
set -euo pipefail

S="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if tmux has-session -t la-search 2>/dev/null; then
  echo "SearXNG is already running (tmux session la-search)"
  exit 0
fi
PORT="$(sed -n 's/^  port: *\([0-9]*\).*/\1/p' "$S/settings.yml")"
mkdir -p "$S/logs"
# Work in $S, not wherever this was run: a home directory on a network share can turn unreadable.
tmux new-session -d -s la-search -c "$S/src" \
  "SEARXNG_SETTINGS_PATH='$S/settings.yml' '$S/.venv/bin/granian' --interface wsgi --host 127.0.0.1 --port ${PORT:-18888} --workers 1 searx.webapp:app 2>&1 | tee -a '$S/logs/searxng.log'"
for _ in $(seq 1 30); do
  if curl -sf -o /dev/null "http://127.0.0.1:${PORT:-18888}/healthz"; then
    echo "SearXNG on 127.0.0.1:${PORT:-18888} (tmux session la-search)"
    exit 0
  fi
  sleep 1
done
echo "SearXNG did not answer within 30 s; see $S/logs/searxng.log" >&2
exit 1
