#!/bin/sh
# Failure paths: a --dir with a space, a corrupt archive, a failed upgrade.
set -u
pkill -f 'gateway run' 2>/dev/null || true
archive="$(ls -t /dist/*-linux-x64.tar.zst | head -1)"
export HOME="$(mktemp -d)" HERMES_HOME="$(mktemp -d)"
key="sk-or-v1-$(printf '0%.0s' $(seq 64))"
setup="--non-interactive --first-name Ada --last-name Lovelace --openrouter-key $key --skip-online-checks"
dir="$HOME/My Apps/hermes"
fail=0

echo "== --dir with a space"
sh /repo/slim/install.sh --archive "$archive" --dir "$dir" -- $setup >/dev/null 2>&1 &&
  "$HOME/.local/bin/hermes" --version >/dev/null && echo ok || { echo FAIL; fail=1; }

echo "== corrupt archive is refused and the install still works"
bad="$HOME/bad/$(basename "$archive")"; mkdir -p "$HOME/bad"
head -c 50000000 "$archive" > "$bad"
if sh /repo/slim/install.sh --archive "$bad" --dir "$dir" -- $setup >"$HOME/bad.log" 2>&1; then
  echo "FAIL: truncated archive installed"; fail=1
else
  tail -2 "$HOME/bad.log"
fi
"$HOME/.local/bin/hermes" --version >/dev/null 2>&1 && echo ok || { echo "FAIL: corrupt archive broke the install"; fail=1; }
[ ! -e "$dir/hermes-slim.new" ] || { echo "FAIL: partial unpack left behind"; fail=1; }

echo "== failed upgrade setup leaves a working install"
# A port taken by a foreign server makes the upgrade's gateway step fail.
pkill -f "$dir" 2>/dev/null; sleep 1
mkdir -p "$HOME/fake" && printf '{"status":"ok"}' > "$HOME/fake/health"
"$dir/current/python/bin/python3" -m http.server 8642 --bind 127.0.0.1 --directory "$HOME/fake" >/dev/null 2>&1 &
fake=$!; sleep 1
"$HOME/.local/bin/hermes" update --archive "$archive" >/dev/null 2>&1 && echo "(update reported success)"
kill $fake
"$HOME/.local/bin/hermes" --version >/dev/null 2>&1 && echo ok || { echo "FAIL: failed upgrade broke the install"; fail=1; }
pkill -f "$HOME/" 2>/dev/null || true
[ $fail = 0 ] && echo EDGE CASES OK
exit $fail
