#!/usr/bin/env bash
# Start llama-swap in a detached tmux session; no GPU is used until a request arrives.
#   run-swap.sh [PORT]     (default 18080, listens on 127.0.0.1 only)
#   stop: tmux kill-session -t la-swap
set -euo pipefail

W="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${1:-18080}"

if tmux has-session -t la-swap 2>/dev/null; then
  echo "llama-swap is already running (tmux session la-swap)"
  exit 0
fi
mkdir -p "$W/logs"
# Work in $W, not wherever this was run: a home directory on a network share can turn unreadable
# while this runs, and nothing here should depend on it.
tmux new-session -d -s la-swap -c "$W" \
  "LLM_HOME='$W' LLAMA_SWAP_KEY=\"\$(cat '$W/secrets/api.key')\" '$W/bin/llama-swap' --config '$W/llama-swap.yaml' --listen 127.0.0.1:$PORT 2>&1 | tee -a '$W/logs/llama-swap.log'"
echo "llama-swap on 127.0.0.1:$PORT (tmux session la-swap)"
