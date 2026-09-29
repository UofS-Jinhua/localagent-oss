#!/usr/bin/env bash
# Started by llama-swap on the first request after an idle unload: pick a free GPU, run
# llama-server on it, and stop it when llama-swap asks or when llama-swap itself is gone.
#   start-llm.sh PORT      (lives in the server's work directory; see the README)
set -euo pipefail

W="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$W/logs/start-llm.log"
# Neutral names: other users on a shared server see llama-server's command line in ps.
MODEL_DIR="$W/models/qwen3.8-27b"
GPU_ORDER="3 2 1 0"  # first choice first

# The guard: stop llama-server once llama-swap or this script is gone. Nothing else would ever
# stop it, and it would hold the GPU for good. A killed tmux session takes llama-swap down without
# a clean unload, and this script with it, so the guard runs in its own session (setsid) and logs
# to a file (llama-swap's stderr pipe is gone by then).
if [ "${1:-}" = --guard ]; then
  script="$2" swap="$3" server="$4"
  while kill -0 "$script" 2>/dev/null && kill -0 "$swap" 2>/dev/null; do sleep 5; done
  if [ "$(cat "/proc/$server/comm" 2>/dev/null)" = llama-server ]; then
    echo "$(date -Is) llama-swap or start-llm is gone: stopping llama-server $server" >> "$LOG"
    kill -TERM "$server"
  fi
  exit 0
fi

PORT="${1:?usage: start-llm.sh PORT}"
mkdir -p "$W/logs"

# The uids of whoever listens on TCP port $1 here, on any address, space-separated (none: it is free).
listeners() {
  local hex f sl addr rem st q tr rt uid rest
  hex="$(printf '%04X' "$1")"
  for f in /proc/net/tcp /proc/net/tcp6; do
    [ -r "$f" ] || continue
    while read -r sl addr rem st q tr rt uid rest; do
      if [ "$st" = 0A ] && [ "${addr##*:}" = "$hex" ]; then echo "$uid"; fi
    done < "$f"
  done | sort -u | paste -sd' '
}

# Anyone logged in here may listen on 127.0.0.1, and this port is free whenever the model is unloaded.
# Held by another user, llama-server could not bind it, yet llama-swap's health check would reach their
# program and llama-swap would then send it the requests. So start only on a free port, and below make
# sure the listener that appears is ours.
taken="$(listeners "$PORT")"
if [ -n "$taken" ]; then
  echo "$(date -Is) port $PORT is already taken (uid $taken), not starting" | tee -a "$LOG" >&2
  exit 1
fi

# A card counts as free only with no process on it and under 1 GiB used: never share one.
gpu=""
for i in $GPU_ORDER; do
  used="$(nvidia-smi -i "$i" --query-gpu=memory.used --format=csv,noheader,nounits | tr -d ' ')"
  procs="$(nvidia-smi -i "$i" --query-compute-apps=pid --format=csv,noheader | grep -c . || true)"
  if [ "$used" -lt 1024 ] && [ "$procs" -eq 0 ]; then gpu="$i"; break; fi
done
if [ -z "$gpu" ]; then
  echo "$(date -Is) no free GPU, not starting" | tee -a "$LOG" >&2
  nvidia-smi --query-gpu=index,memory.used,utilization.gpu --format=csv >&2
  exit 1
fi

# Q8_0 with its MTP head drafting 2 tokens, the vision projector, and a 524K KV pool shared by 4 slots
# (each conversation still stops at the model's 262K). Measured on one A100 (docs/design.md 1.6): 69.5 GiB,
# one stream 81 tok/s, four at once 148 tok/s in total; 262K over 2 slots was 49 GiB and 104 tok/s at best.
export CUDA_VISIBLE_DEVICES="$gpu"
"$W/llama.cpp/build/bin/llama-server" \
  --host 127.0.0.1 --port "$PORT" \
  --api-key-file "$W/secrets/api.key" \
  --model "$MODEL_DIR/qwen3.8-27b-q8_0.gguf" \
  --mmproj "$MODEL_DIR/mmproj-qwen3.8-27b-f16.gguf" \
  --alias qwen3.8-27b \
  --ctx-size 524288 --parallel 4 --kv-unified \
  --n-gpu-layers 999 --flash-attn on --jinja \
  --batch-size 4096 --ubatch-size 2048 \
  --spec-type draft-mtp --spec-draft-n-max 2 &
server=$!
echo "$(date -Is) GPU $gpu, port $PORT, llama-server $server, llama-swap $PPID" >> "$LOG"

# Its own fds, so llama-swap is not left waiting for the guard to close the log pipe.
setsid "$0" --guard "$$" "$PPID" "$server" < /dev/null > /dev/null 2>&1 &

# llama-swap's unload (idle timer, API, swap) sends TERM here: pass it on and wait.
trap 'kill -TERM "$server" 2>/dev/null; wait "$server"; exit 0' TERM INT HUP

# Someone could still take the port between that check and llama-server binding it (which it does
# before loading the model, within seconds). Whoever listens first must be us.
owners=""
for _ in $(seq 300); do  # up to a minute
  owners="$(listeners "$PORT")"
  if [ -n "$owners" ] || ! kill -0 "$server" 2>/dev/null; then break; fi
  sleep 0.2
done
if [ -n "$owners" ] && [ "$owners" != "$(id -u)" ]; then
  echo "$(date -Is) port $PORT was taken (uid $owners) before llama-server could listen; stopping" | tee -a "$LOG" >&2
  kill -TERM "$server" 2>/dev/null || true
  wait "$server" || true
  exit 1
fi
wait "$server"
