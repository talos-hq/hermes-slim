#!/bin/sh
# Something else answers /health on the API port: setup must fail, not adopt it.
set -eu
pkill -f 'gateway run' 2>/dev/null || true
archive="$(ls -t /dist/*-linux-x64.tar.zst | head -1)"
export HOME="$(mktemp -d)" HERMES_HOME="$(mktemp -d)"
mkdir -p "$HOME/fake" && printf '{"status": "ok"}' > "$HOME/fake/health"
"$HOME/.hermes-slim/current/python/bin/python3" -V 2>/dev/null || true
sh /repo/slim/install.sh --archive "$archive" -- --no-gateway --non-interactive --first-name A --last-name B \
  --openrouter-key "sk-or-v1-$(printf '0%.0s' $(seq 64))" --skip-online-checks >/dev/null
py="$HOME/.hermes-slim/current/python/bin/python3"
"$py" -m http.server 8642 --bind 127.0.0.1 --directory "$HOME/fake" >/dev/null 2>&1 &
fake=$!
trap 'kill $fake' EXIT
sleep 1
if "$HOME/.local/bin/hermes-slim-setup" --non-interactive --first-name A --last-name B \
  --openrouter-key "sk-or-v1-$(printf '0%.0s' $(seq 64))" --skip-online-checks --gateway-wait 60 --json; then
  echo "setup adopted a foreign /health" >&2; exit 1
fi
echo PORT BUSY OK
