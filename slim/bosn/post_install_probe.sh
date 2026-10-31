#!/bin/sh
# What does an installed bundle do on commands that, in a git install, fetch or build?
set -u
pkill -f 'gateway run' 2>/dev/null || true
archive="$(ls -t /dist/*-linux-x64.tar.zst | head -1)"
export HOME="$(mktemp -d)" HERMES_HOME="$(mktemp -d)"
sh /repo/slim/install.sh --archive "$archive" -- --no-gateway --non-interactive --first-name A --last-name B \
  --openrouter-key "sk-or-v1-$(printf '0%.0s' $(seq 64))" --skip-online-checks >/dev/null
h="$HOME/.local/bin/hermes"
for cmd in "update --check" "update" "doctor" "pm status" "tools list"; do
  echo "===== hermes $cmd"
  timeout 120 $h $cmd </dev/null 2>&1 | tail -12
  echo "[exit $?]"
done
echo "===== files written inside the install dir"
find "$HOME/.hermes-slim/current" -newer "$HOME/.hermes-slim/current/BUNDLE.json" -type f | grep -v __pycache__ | head
