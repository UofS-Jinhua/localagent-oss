#!/usr/bin/env bash
# Install (or update) a private SearXNG for LocalAgent's WebSearch, in the folder this script is in.
#   setup-searxng.sh            then: run-searxng.sh, and point LocalAgent at http://127.0.0.1:18888
# No root, no docker: the source tarball, a uv virtualenv, and settings that only listen on 127.0.0.1.
# Search engines often block the addresses of shared servers; one user's own instance on such a
# server looks like one person searching, and answers in JSON (format json is switched on here).
set -euo pipefail

S="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UV="${UV:-$(command -v uv || true)}"
if [ -z "$UV" ]; then
  echo "uv is needed (https://docs.astral.sh/uv/); set UV=/path/to/uv" >&2
  exit 1
fi
export UV_CACHE_DIR="${UV_CACHE_DIR:-$S/.cache/uv}"  # keep caches off a full NFS home
export TMPDIR="${TMPDIR:-$S/tmp}"
mkdir -p "$TMPDIR"

echo "downloading SearXNG (source tarball; git clone from GitHub can be very slow on some servers)"
curl -sSfL --retry 3 https://codeload.github.com/searxng/searxng/tar.gz/refs/heads/master -o "$S/searxng.tar.gz"
rm -rf "$S/src.new"
mkdir -p "$S/src.new"
tar xzf "$S/searxng.tar.gz" -C "$S/src.new" --strip-components=1
rm -rf "$S/src" && mv "$S/src.new" "$S/src" && rm -f "$S/searxng.tar.gz"

"$UV" venv --quiet --allow-existing --python 3.12 "$S/.venv"
VIRTUAL_ENV="$S/.venv" "$UV" pip install --quiet -r "$S/src/requirements.txt" -r "$S/src/requirements-server.txt"

if [ ! -f "$S/settings.yml" ]; then
  umask 077
  cat > "$S/settings.yml" <<EOF
# Only what differs from SearXNG's defaults (src/searx/settings.yml).
use_default_settings: true
general:
  instance_name: "LocalAgent search"
search:
  formats: [html, json]   # json is what LocalAgent reads
server:
  bind_address: "127.0.0.1"
  port: 18888
  secret_key: "$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  limiter: false          # needs valkey, and there is one user
  public_instance: false
  image_proxy: false
EOF
fi
echo "installed in $S; start it with $S/run-searxng.sh"
