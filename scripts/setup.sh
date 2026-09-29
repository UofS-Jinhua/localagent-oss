#!/usr/bin/env bash
# Set up LocalAgent on macOS or Linux: a virtualenv, the dependencies, and (optionally) a model server.
#   ./scripts/setup.sh [--base-url http://host:1234] [--dev] [--headless]
#   --headless: no desktop window (a server you reach over ssh); use `la ui --browser`.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
venv="$repo/.venv"
base_url=""
dev=0
desktop=1  # pywebview: only the desktop window needs it, and a headless server has none

while [ $# -gt 0 ]; do
  case "$1" in
    --base-url) base_url="$2"; shift 2 ;;
    --dev) dev=1; shift ;;
    --headless) desktop=0; shift ;;
    -h|--help) sed -n '2,4p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done

# Plain ifs: a `[ ... ] && printf` that ends a $(...) returns 1 when false, and set -e then
# ends the script without a word (it did, for every --headless run).
extras=""
if [ "$dev" = 1 ]; then extras="dev,"; fi
if [ "$desktop" = 1 ]; then extras="${extras}desktop,"; fi
extras="${extras%,}"

find_python() {
  for candidate in python3.14 python3.13 python3.12 python3 python; do
    if command -v "$candidate" > /dev/null 2>&1 &&
       "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

python_exe="$(find_python)" || {
  echo "No Python 3.12+ found. Install it (python.org, brew, or your package manager) and run this again." >&2
  exit 1
}
echo "==> using $("$python_exe" -V) ($python_exe)"

if [ ! -x "$venv/bin/python" ]; then
  echo "==> creating the virtualenv ($venv)"
  "$python_exe" -m venv "$venv"
else
  echo "==> reusing the existing virtualenv ($venv)"
fi

echo "==> installing LocalAgent and its dependencies [$extras]"
"$venv/bin/python" -m pip install --quiet --upgrade pip
if [ -n "$extras" ]; then
  "$venv/bin/python" -m pip install --quiet -e "$repo[$extras]"
else
  "$venv/bin/python" -m pip install --quiet -e "$repo"
fi
"$venv/bin/python" -c "import localagent; print('    installed LocalAgent', localagent.__version__)"

if [ -n "$base_url" ]; then
  echo "==> registering $base_url"
  "$venv/bin/la" init --base-url "$base_url"
elif [ ! -f "$repo/data/models.yaml" ] && [ ! -f "$HOME/.localagent/models.yaml" ]; then
  echo "    no model server registered yet: run  .venv/bin/la init --base-url http://<host>:1234"
fi

cat <<EOF

Done.
  Open the window:   .venv/bin/la ui          (needs a desktop session)
  Over ssh:          .venv/bin/la ui --browser --no-open --port 8765
                     then on your own machine:  ssh -L 8765:localhost:8765 <you>@<this host>
                     and open the printed http://127.0.0.1:8765/#token=... there
  Command line:      .venv/bin/la run -w <folder> "fix the failing test"
  Add a model:       .venv/bin/la init --base-url http://<host>:1234
EOF
