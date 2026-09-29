#!/usr/bin/env bash
# Start, stop and look at everything LocalAgent runs on this server, in one place.
#
#   la-services.sh status              what runs, on which port, and what holds a GPU
#   la-services.sh start [swap|search|ui|all]
#   la-services.sh stop  [swap|search|ui|all]
#   la-services.sh restart [...]       (default for every command: all)
#
# From another computer, without logging in first (any app can do the same in one line:
# `ssh gpu1 bin/la-services start`):
#   la-services.sh --host gpu1 status         (or LA_HOST=gpu1; LA_REMOTE_CMD sets the path there,
#                                              default bin/la-services)
#
# Each service is a detached tmux session, so this only ever starts, kills or asks about one:
#   swap    la-swap    llama-swap on 18080; loads a model on the first request, unloads when idle
#   search  la-search  SearXNG on 18888, what WebSearch asks
#   ui      la-ui      the LocalAgent UI on 8765 (the app starts this one itself when you connect)
#
# Anyone logged in to a shared server may listen on 127.0.0.1, so a port that answers is not
# proof that our service does: status names a port someone else holds (and exits 1), and start
# will not start a service whose port is already taken.
#
# Paths come from the folder this script sits in: <base>/bin/la-services.sh uses <base>/localllm,
# <base>/searxng and <base>/bin/la-ui. Set LA_BASE to say otherwise.
set -uo pipefail

HOST="${LA_HOST:-}"
args=()
while [ $# -gt 0 ]; do
  case "$1" in
    -H|--host) HOST="${2:-}"; shift 2;;
    *) args+=("$1"); shift;;
  esac
done
set -- "${args[@]+"${args[@]}"}"
if [ -n "$HOST" ]; then
  # Drive the machine over ssh: the same script, run there. Host keys are checked as ssh always does.
  exec ssh -T "$HOST" "${LA_REMOTE_CMD:-bin/la-services}" "$@"
fi

BASE="${LA_BASE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SWAP="$BASE/localllm/run-swap.sh"
SEARCH="$BASE/searxng/run-searxng.sh"
UI="$BASE/bin/la-ui"
FORCE=0

session_of() { case "$1" in swap) echo la-swap;; search) echo la-search;; ui) echo la-ui;; esac; }
port_of()    { case "$1" in swap) echo 18080;; search) echo 18888;; ui) echo 8765;; esac; }
starter_of() { case "$1" in swap) echo "$SWAP";; search) echo "$SEARCH";; ui) echo "$UI";; esac; }

running() { tmux has-session -t "$(session_of "$1")" 2>/dev/null; }

# Who listens on a service's port, on any address: nothing, "me", or "uid 1234 ..." when anyone else
# does (read from /proc/net/tcp, whose uid column says who opened each socket).
holder() {
  local hex f sl addr rem st q tr rt uid rest who
  hex="$(printf '%04X' "$(port_of "$1")")"
  who="$(for f in /proc/net/tcp /proc/net/tcp6; do
    [ -r "$f" ] || continue
    while read -r sl addr rem st q tr rt uid rest; do
      if [ "$st" = 0A ] && [ "${addr##*:}" = "$hex" ]; then echo "$uid"; fi
    done < "$f"
  done | sort -u | paste -sd' ')"
  if [ "$who" = "$(id -u)" ]; then echo me; elif [ -n "$who" ]; then echo "uid $who"; fi
}

ui_busy() {
  # Refuse to pull the UI out from under a running task unless asked twice (--force).
  local url token
  url="$(tmux capture-pane -p -J -t la-ui -S -200 2>/dev/null | grep -o 'http://127.0.0.1:[0-9]*/#token=[^[:space:]&]*' | tail -1)"
  token="${url##*token=}"
  [ -n "$token" ] || return 1
  [ "$(holder ui)" = me ] || return 1  # the token goes to our own UI only
  curl -s -m 3 -H "Authorization: Bearer $token" "http://127.0.0.1:$(port_of ui)/api/state" 2>/dev/null | grep -q '"busy":true'
}

start_one() {
  local name="$1" starter; starter="$(starter_of "$name")"
  if running "$name"; then echo "$name: already running"; return 0; fi
  local who; who="$(holder "$name")"
  if [ "$who" = me ]; then
    echo "$name: port $(port_of "$name") is already taken by another program of yours; not starting" >&2
    return 1
  elif [ -n "$who" ]; then
    echo "$name: port $(port_of "$name") is held by $who, not you; not starting (it would get what is meant for $name)" >&2
    return 1
  fi
  if [ ! -x "$starter" ]; then echo "$name: $starter is missing (set LA_BASE?)" >&2; return 1; fi
  "$starter" >/dev/null || { echo "$name: $starter failed" >&2; return 1; }
  echo "$name: started on 127.0.0.1:$(port_of "$name")"
}

stop_one() {
  local name="$1"
  if ! running "$name"; then echo "$name: not running"; return 0; fi
  if [ "$name" = ui ] && [ "$FORCE" = 0 ] && ui_busy; then
    echo "ui: a task is running; wait for it or pass --force" >&2
    return 1
  fi
  local session sid; session="$(session_of "$name")"
  # tmux ends a session with SIGHUP, which granian (SearXNG) takes as "reload" and outlives: every stop
  # left one more SearXNG on the port. So TERM whatever the session started (its process session is the
  # pane's pid) and wait until it is gone -- llama-swap's model too, so the GPU is free on return.
  sid="$(tmux display-message -p -t "$session" '#{pane_pid}' 2>/dev/null)"
  if [ -n "$sid" ]; then
    pkill -TERM -s "$sid" 2>/dev/null
    for _ in $(seq 75); do pgrep -s "$sid" >/dev/null || break; sleep 0.2; done
    pkill -KILL -s "$sid" 2>/dev/null
  fi
  tmux kill-session -t "$session" 2>/dev/null
  echo "$name: stopped"
}

status_one() {
  local name="$1" mark="stopped" port who alarm=0; port="$(port_of "$name")"; who="$(holder "$name")"
  if [ -n "$who" ] && [ "$who" != me ]; then
    mark="PORT HELD BY $who, not you"; alarm=1
  elif running "$name"; then
    mark="running"
    [ "$who" = me ] || mark="starting (session up, port $port silent)"
  elif [ "$who" = me ]; then
    mark="stopped (another program of yours has the port)"
  fi
  printf '  %-7s %-10s %-48s %s\n' "$name" "$(session_of "$name")" "$mark" "127.0.0.1:$port"
  return $alarm
}

status() {
  echo "LocalAgent services on $(hostname) (base $BASE)"
  local name alarm=0
  for name in "$@"; do status_one "$name" || alarm=1; done
  local gpu
  gpu="$(nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader 2>/dev/null)"
  if [ -n "$gpu" ]; then echo "GPU in use:"; echo "$gpu" | sed 's/^/  /'
  else echo "GPU: nothing loaded (llama-swap loads a model on the first request)"; fi
  return $alarm
}

command="${1:-status}"
[ $# -gt 0 ] && shift
targets=()
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1;;
    all) targets+=(swap search ui);;
    swap|search|ui) targets+=("$arg");;
    *) echo "unknown service $arg (swap, search, ui, all)" >&2; exit 2;;
  esac
done
[ ${#targets[@]} -eq 0 ] && targets=(swap search ui)

failed=0
case "$command" in
  status) status "${targets[@]}" || failed=1;;
  start)  for name in "${targets[@]}"; do start_one "$name" || failed=1; done;;
  stop)   for name in $(printf '%s\n' "${targets[@]}" | tac); do stop_one "$name" || failed=1; done;;  # UI first, model server last
  restart)
    for name in $(printf '%s\n' "${targets[@]}" | tac); do stop_one "$name" || failed=1; done
    for name in "${targets[@]}"; do start_one "$name" || failed=1; done;;
  -h|--help|help) awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "${BASH_SOURCE[0]}";;
  *) echo "usage: la-services.sh {status|start|stop|restart} [swap|search|ui|all] [--force]" >&2; exit 2;;
esac
exit $failed
