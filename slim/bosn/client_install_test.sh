#!/bin/sh
# Install the newest bundle in /dist with install.sh, as a fresh user would,
# then prove the install works without any network package step.
set -eu
# --no-zstd: hide the host zstd so install.sh has to use the static one.
if [ "${2:-}" = --no-zstd ]; then
  mv /usr/bin/zstd /usr/bin/zstd.hidden
  trap 'mv /usr/bin/zstd.hidden /usr/bin/zstd' EXIT
fi
archive="$(ls -t /dist/*-"${1:?usage: client_install_test.sh TARGET}".tar.zst 2>/dev/null | head -1)"
[ -n "$archive" ] || { echo "no bundle in /dist; run: bosn run --task bundle" >&2; exit 1; }
# bosn keeps this container warm: earlier runs' gateways would answer /health for us.
pkill -f 'gateway run' 2>/dev/null || true
export HOME="$(mktemp -d)" HERMES_HOME="$(mktemp -d)"
start=$(date +%s)
sh /repo/slim/install.sh --archive "$archive" -- --non-interactive --first-name Ada --last-name Lovelace \
  --openrouter-key "sk-or-v1-$(printf '0%.0s' $(seq 64))" --skip-online-checks --json
echo "install.sh took $(( $(date +%s) - start ))s"
"$HOME/.local/bin/hermes" --version
# A new login shell finds `hermes` on PATH.
env -i HOME="$HOME" HERMES_HOME="$HERMES_HOME" PATH=/usr/bin:/bin sh -lc 'command -v hermes' >/dev/null ||
  { echo "hermes is not on PATH in a new shell" >&2; exit 1; }
# Upgrade in place while the first install's gateway still runs: the new code must serve.
old_pid="$(pgrep -f "$HOME/.hermes-slim/.*gateway run" | head -1)"
sh /repo/slim/install.sh --archive "$archive" -- --non-interactive --first-name Ada --last-name Lovelace \
  --openrouter-key "sk-or-v1-$(printf '0%.0s' $(seq 64))" --skip-online-checks --json
new_pid="$(pgrep -f "$HOME/.hermes-slim/.*gateway run" | head -1)"
echo "gateway pid before reinstall: $old_pid, after: $new_pid"
[ -n "$new_pid" ] && [ "$new_pid" != "$old_pid" ] || { echo "reinstall left the old gateway running" >&2; exit 1; }
ls /proc/"$new_pid"/cwd >/dev/null
"$HOME/.local/bin/hermes" gateway stop >/dev/null 2>&1 || true
echo CLIENT INSTALL OK
pkill -f "$HOME/.hermes-slim/" 2>/dev/null || true
# `hermes update` must move the bundle with slim's installer, keep the configuration
# and bring the gateway back; never send the user to upstream's git installer.
before="$(pgrep -f "$HOME/.hermes-slim/.*gateway run" | head -1)"
out="$("$HOME/.local/bin/hermes" update --archive "$archive" 2>&1)" || { echo "$out"; echo "hermes update failed" >&2; exit 1; }
echo "$out" | tail -3
case "$out" in *nousresearch*|*"Not a git repository"*) echo "hermes update fell through to upstream" >&2; exit 1 ;; esac
grep -q "first_name: Ada" "$HERMES_HOME/config.yaml" || { echo "update lost the configuration" >&2; exit 1; }
after="$(pgrep -f "$HOME/.hermes-slim/.*gateway run" | head -1)"
[ -n "$after" ] && [ "$after" != "$before" ] || { echo "update did not restart the gateway" >&2; exit 1; }
pkill -f "$HOME/.hermes-slim/" 2>/dev/null || true
echo HERMES UPDATE OK
