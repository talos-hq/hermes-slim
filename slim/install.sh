#!/bin/sh
# hermes-slim installer for macOS and Linux (glibc or musl).
#
#   sh install.sh [--version vX.Y.Z | --archive FILE|URL] [--dir ~/.hermes-slim] [--jobs N]
#                 [--upgrade] [--no-setup] [-- <hermes-slim-setup args>]
#
# --upgrade (what `hermes update` runs) keeps the existing configuration and only
# restarts the gateway; without it, setup asks for (or takes) every value.
#
# --no-setup stops after linking `hermes` (the macOS .pkg uses it: an installer has
# no terminal to prompt on, so the user runs hermes-slim-setup afterwards).
#
# --archive installs a bundle from disk (offline installs, CI) or from any HTTP(S)
# mirror; its FILE.sha256 / URL.sha256 is checked when present.
#
# --jobs sets how many byte ranges of the bundle download at once (default 8; 1
# downloads it as one stream).
#
# Downloads one prebuilt bundle from github.com/talos-hq/hermes-slim releases,
# verifies its sha256, unpacks it, and runs hermes-slim-setup. Nothing is
# compiled and no package manager runs.
#
# Every stage resumes: the download lives in <dir>/downloads until it is installed,
# each byte range continues from where it stopped, and a rerun skips a download or
# unpack that already finished for the same sha256.
set -eu

repo="talos-hq/hermes-slim"
version="latest"
archive=""
upgrade=false
setup=true
jobs=8
dir="${HERMES_SLIM_DIR:-$HOME/.hermes-slim}"
while [ $# -gt 0 ]; do
  case "$1" in
    --version) version="$2"; shift 2 ;;
    --dir) dir="$2"; shift 2 ;;
    --archive) archive="$2"; shift 2 ;;
    --jobs) jobs="$2"; shift 2 ;;
    --upgrade) upgrade=true; shift ;;
    --no-setup) setup=false; shift ;;
    --) shift; break ;;
    *) break ;;
  esac
done
case "$jobs" in ''|*[!0-9]*|0) echo "hermes-slim: --jobs needs a positive number" >&2; exit 2 ;; esac

case "$(uname -s)-$(uname -m)" in
  Darwin-x86_64) target=darwin-x64 ;;
  Darwin-arm64) target=darwin-arm64 ;;
  Linux-x86_64)
    # musl (Alpine) and glibc (everything else) need different bundles: neither's
    # interpreter or compiled wheels load on the other.
    if ls /lib/ld-musl-*.so.1 >/dev/null 2>&1; then
      target=linux-x64-musl
    else
      target=linux-x64
      glibc="$(getconf GNU_LIBC_VERSION 2>/dev/null | sed 's/^glibc //')"
      case "$glibc" in
        2.[0-9]|2.1[0-9]|2.2[0-7]) echo "hermes-slim: needs glibc 2.28 or newer, this system has $glibc" >&2; exit 1 ;;
      esac
    fi ;;
  Linux-aarch64|Linux-arm64)
    # glibc only: no musl arm64 bundle yet.
    if ls /lib/ld-musl-*.so.1 >/dev/null 2>&1; then
      echo "hermes-slim: no bundle for musl (Alpine) on arm64 yet" >&2; exit 1
    fi
    target=linux-arm64
    glibc="$(getconf GNU_LIBC_VERSION 2>/dev/null | sed 's/^glibc //')"
    case "$glibc" in
      2.[0-9]|2.1[0-9]|2.2[0-7]) echo "hermes-slim: needs glibc 2.28 or newer, this system has $glibc" >&2; exit 1 ;;
    esac ;;
  *) echo "hermes-slim: unsupported platform $(uname -s)-$(uname -m)" >&2; exit 1 ;;
esac

sha256() { (shasum -a 256 "$1" 2>/dev/null || sha256sum "$1") | cut -d' ' -f1; }
size_of() { if [ -f "$1" ]; then wc -c <"$1" | tr -d ' '; else echo 0; fi; }

# Total size when the server honours byte ranges (GitHub's release CDN does), else empty.
range_size() {
  curl -fsSL -r 0-0 -D - -o /dev/null "$1" 2>/dev/null | tr -d '\r' |
    sed -n 's#^[Cc][Oo][Nn][Tt][Ee][Nn][Tt]-[Rr][Aa][Nn][Gg][Ee]: *bytes 0-0/\([0-9][0-9]*\)$#\1#p' | tail -1
}

# Fetch byte ranges of $1 into $2.parts/<i> concurrently. A part that stopped early (network
# drop, Ctrl-C, reboot) continues from its last byte on the next round or the next run.
fetch_ranges() {
  url="$1" out="$2" size="$3"
  parts="$out.parts"
  [ "$(cat "$parts/size" 2>/dev/null)" = "$size" ] || { rm -rf "$parts"; mkdir -p "$parts"; echo "$size" >"$parts/size"; }
  chunk=$(( (size + jobs - 1) / jobs ))
  round=0
  while :; do
    pids="" missing=0 i=0
    while [ $((i * chunk)) -lt "$size" ]; do
      start=$((i * chunk)) end=$((i * chunk + chunk - 1))
      [ "$end" -lt "$size" ] || end=$((size - 1))
      part_len=$((end - start + 1)) have="$(size_of "$parts/$i")"
      [ "$have" -le "$part_len" ] || { : >"$parts/$i"; have=0; }
      if [ "$have" -lt "$part_len" ]; then
        missing=$((missing + 1))
        curl -fsSL --connect-timeout 20 -r "$((start + have))-$end" "$url" >>"$parts/$i" 2>/dev/null &
        pids="$pids $!"
      fi
      i=$((i + 1))
    done
    [ "$missing" -gt 0 ] || break
    [ "$round" -lt 5 ] || { echo "hermes-slim: download of $url keeps failing; rerun to resume it" >&2; exit 1; }
    round=$((round + 1))
    while :; do
      alive=0
      for p in $pids; do kill -0 "$p" 2>/dev/null && alive=1; done
      if [ -t 2 ]; then
        got=0
        for f in "$parts"/[0-9]*; do got=$((got + $(size_of "$f"))); done
        printf '\r  %3d%%  %d / %d MiB' $((got * 100 / size)) $((got >> 20)) $((size >> 20)) >&2
      fi
      [ "$alive" = 1 ] || break
      sleep 1
    done
    wait || true
  done
  [ ! -t 2 ] || printf '\n' >&2
  i=0
  : >"$out.tmp"
  while [ $((i * chunk)) -lt "$size" ]; do cat "$parts/$i" >>"$out.tmp"; i=$((i + 1)); done
  mv "$out.tmp" "$out"
  rm -rf "$parts"
}

# fetch URL OUT [SHA256]: skip when OUT already has SHA256, otherwise download it (in parallel
# ranges when the file is large and the server allows it) and check SHA256.
fetch() {
  url="$1" out="$2" want="${3:-}"
  if [ -n "$want" ] && [ -f "$out" ] && [ "$(sha256 "$out")" = "$want" ]; then
    echo "→ $(basename "$out") already downloaded"
    return 0
  fi
  rm -f "$out"
  size="$(range_size "$url")"
  if [ "$jobs" -gt 1 ] && [ -n "$size" ] && [ "$size" -ge 8388608 ]; then
    echo "→ downloading $(basename "$out") ($((size >> 20)) MiB, $jobs connections)"
    fetch_ranges "$url" "$out" "$size"
  else
    echo "→ downloading $(basename "$out")"
    curl -fSL --retry 3 -C - --progress-bar -o "$out.part" "$url"
    mv "$out.part" "$out"
  fi
  if [ -n "$want" ] && [ "$(sha256 "$out")" != "$want" ]; then
    rm -f "$out"
    echo "hermes-slim: sha256 mismatch for $(basename "$out"); rerun to download it again" >&2
    exit 1
  fi
}

# Apple's bsdtar shells out to zstd. Prefer the native standalone decompressor
# published beside the bundles rather than assuming a bsdtar has built-in support.
case "$target" in
  linux-x64|linux-x64-musl) zstd_asset=zstd-linux-x64 ;;
  darwin-arm64|darwin-x64) zstd_asset="zstd-$target" ;;
  *) zstd_asset="" ;;
esac
# Without a zstd here, start fetching the standalone decompressor (and its checksum) in the
# background as soon as the release location is known, so it downloads alongside the
# bundle's ranges instead of after them. unpack() waits for it and checks it.
helper_pids=""
start_helper() {
  if [ -z "$base" ] || [ -z "$zstd_asset" ] || command -v zstd >/dev/null 2>&1; then return 0; fi
  echo "→ no zstd here; fetching a standalone decompressor alongside the bundle"
  curl -fsSL --retry 3 -o "$tmp/zstd" "$base/$zstd_asset" 2>/dev/null &
  helper_pids="$!"
  curl -fsSL --retry 3 -o "$tmp/zstd.sha256" "$base/$zstd_asset.sha256" 2>/dev/null &
  helper_pids="$helper_pids $!"
}
unpack() {
  if command -v zstd >/dev/null 2>&1; then
    zstd -dcq "$1" | tar -xf - -C "$2" --strip-components 1
  elif [ -n "$zstd_asset" ]; then
    if [ -n "$base" ]; then
      # fetch_ranges' `wait` may already have reaped these; the checksum below is the real test.
      for p in $helper_pids; do wait "$p" 2>/dev/null || true; done
      if [ ! -s "$tmp/zstd" ] || [ ! -s "$tmp/zstd.sha256" ]; then
        echo "→ fetching the standalone decompressor"
        curl -fsSL --retry 3 -o "$tmp/zstd" "$base/$zstd_asset"
        curl -fsSL --retry 3 -o "$tmp/zstd.sha256" "$base/$zstd_asset.sha256"
      fi
    else
      cp "$(dirname "$1")/$zstd_asset" "$tmp/zstd" 2>/dev/null &&
        cp "$(dirname "$1")/$zstd_asset.sha256" "$tmp/zstd.sha256" 2>/dev/null ||
        { echo "hermes-slim: no zstd; install it, or put $zstd_asset(.sha256) beside the archive" >&2; exit 1; }
    fi
    [ "$(cut -d' ' -f1 "$tmp/zstd.sha256")" = "$(sha256 "$tmp/zstd")" ] || { echo "hermes-slim: zstd sha256 mismatch" >&2; exit 1; }
    chmod +x "$tmp/zstd"
    "$tmp/zstd" -dcq "$1" | tar -xf - -C "$2" --strip-components 1
  elif tar --version 2>/dev/null | grep -q bsdtar; then
    tar -xf "$1" -C "$2" --strip-components 1
  else
    echo "hermes-slim: unpacking needs zstd (apt install zstd / dnf install zstd / brew install zstd)" >&2
    exit 1
  fi
}

# The sha256 recorded in an unpacked tree marks it complete; an interrupted unpack has none.
installed() { [ -n "$expected" ] && [ "$(cat "$1/.bundle-sha256" 2>/dev/null)" = "$expected" ]; }

# Nothing to download when an earlier run already unpacked this exact bundle.
need_bundle() { ! installed "$dir/current" && ! installed "$dir/hermes-slim.new"; }

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$dir/downloads"
base=""
expected=""
case "$archive" in
  http://*|https://*)
    base="${archive%/*}"
    start_helper
    curl -fsSL -o "$tmp/archive.sha256" "$archive.sha256" 2>/dev/null && expected="$(cut -d' ' -f1 "$tmp/archive.sha256")"
    if [ -z "$expected" ] || need_bundle; then fetch "$archive" "$dir/downloads/${archive##*/}" "$expected"; fi
    archive="$dir/downloads/${archive##*/}" ;;
  ?*)
    [ -f "$archive" ] || { echo "hermes-slim: no such archive: $archive" >&2; exit 1; }
    if [ -f "$archive.sha256" ]; then
      expected="$(cut -d' ' -f1 "$archive.sha256")"
      [ "$expected" = "$(sha256 "$archive")" ] || { echo "hermes-slim: sha256 mismatch" >&2; exit 1; }
    fi ;;
  *)
    # The release manifest (slim/release_manifest.py) is served by the release CDN, which,
    # unlike api.github.com (60 unauthenticated calls per hour per IP), does not run out on
    # a shared network (#146). It names the tag, so every download pins to one release.
    if [ "$version" = latest ]; then manifest_base="https://github.com/$repo/releases/latest/download"
    else manifest_base="https://github.com/$repo/releases/download/$version"; fi
    if curl -fsSL --retry 3 -o "$tmp/manifest" "$manifest_base/hermes-slim-manifest.txt" 2>/dev/null; then
      tag="$(sed -n 's/^release \([^ ]*\)$/\1/p' "$tmp/manifest" | head -1)"
      [ -z "$tag" ] || version="$tag"
      line="$(grep "^bundle $target hermes-slim-[^ ]*\.tar\.zst " "$tmp/manifest" | head -1)"
      asset="$(echo "$line" | cut -d' ' -f3)"
      expected="$(echo "$line" | cut -d' ' -f4)"
    else
      # Releases from before the manifest: one API call gives the tag and the asset names.
      if [ "$version" = latest ]; then release_api="releases/latest"; else release_api="releases/tags/$version"; fi
      if ! curl -fsSL "https://api.github.com/repos/$repo/$release_api" >"$tmp/release.json" 2>"$tmp/api.err"; then
        if grep -qE "error: (403|429)" "$tmp/api.err"; then
          echo "hermes-slim: GitHub's API rate limit for this network is used up; retry later, or pass --archive URL" >&2
        else
          echo "hermes-slim: could not read release $version of $repo: $(cat "$tmp/api.err")" >&2
        fi
        exit 1
      fi
      tag="$(sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' "$tmp/release.json" | head -1)"
      [ -z "$tag" ] || version="$tag"
      asset="$(sed -n "s/.*\"name\": *\"\(hermes-slim-[^\"]*-$target\.tar\.zst\)\".*/\1/p" "$tmp/release.json" | head -1)"
      expected=""
    fi
    [ "$version" != latest ] || { echo "hermes-slim: could not read the latest release of $repo" >&2; exit 1; }
    [ -n "$asset" ] || { echo "hermes-slim: no $target bundle in release $version" >&2; exit 1; }
    base="https://github.com/$repo/releases/download/$version"
    start_helper
    if [ -z "$expected" ]; then
      curl -fsSL -o "$tmp/$asset.sha256" "$base/$asset.sha256"
      expected="$(cut -d' ' -f1 "$tmp/$asset.sha256")"
    fi
    if need_bundle; then fetch "$base/$asset" "$dir/downloads/$asset" "$expected"; fi
    archive="$dir/downloads/$asset" ;;
esac

if installed "$dir/current"; then
  echo "→ this bundle is already installed in $dir/current"
else
  if installed "$dir/hermes-slim.new"; then
    echo "→ reusing the bundle unpacked by an earlier run"
  else
    echo "→ installing to $dir"
    rm -rf "$dir/hermes-slim.new"
    mkdir "$dir/hermes-slim.new"
    unpack "$archive" "$dir/hermes-slim.new" || {
      rm -rf "$dir/hermes-slim.new"
      echo "hermes-slim: could not unpack $archive (incomplete or corrupt download?); the existing install is untouched" >&2
      exit 1
    }
    [ -z "$expected" ] || echo "$expected" >"$dir/hermes-slim.new/.bundle-sha256"
  fi
  rm -rf "$dir/hermes-slim.old"
  if [ -d "$dir/current" ]; then
    # Otherwise the old gateway keeps answering /health from the replaced tree, and
    # setup takes it for the new one.
    "$dir/current/bin/hermes" gateway stop >/dev/null 2>&1 || true
    mv "$dir/current" "$dir/hermes-slim.old"
  fi
  mv "$dir/hermes-slim.new" "$dir/current"
  # Unsigned bundle: clear the download quarantine so Gatekeeper does not block the interpreter.
  [ "$(uname -s)" = Darwin ] && xattr -dr com.apple.quarantine "$dir/current" 2>/dev/null || true
fi
# The cached download has served its purpose once its tree is in place.
case "$archive" in "$dir/downloads/"*) rm -f "$archive" ;; esac

mkdir -p "$HOME/.local/bin"
ln -sf "$dir/current/bin/hermes" "$HOME/.local/bin/hermes"
ln -sf "$dir/current/bin/hermes-slim-setup" "$HOME/.local/bin/hermes-slim-setup"
echo "✓ installed; 'hermes' is linked in ~/.local/bin"
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *)
    # Fresh users on Debian, Alpine and macOS do not have ~/.local/bin on PATH, and
    # `hermes` would be "command not found" after install. Login shells read ~/.profile;
    # zsh (macOS default) reads ~/.zprofile.
    line='export PATH="$HOME/.local/bin:$PATH"  # hermes-slim'
    for rc in "$HOME/.profile" "$HOME/.zprofile"; do
      { [ "$rc" = "$HOME/.profile" ] || [ -e "$rc" ] || [ "$(uname -s)" = Darwin ]; } || continue
      grep -qs "# hermes-slim" "$rc" || printf '\n%s\n' "$line" >> "$rc"
    done
    echo "  added ~/.local/bin to PATH for new shells; in this one run: export PATH=\"\$HOME/.local/bin:\$PATH\""
    ;;
esac

[ "$setup" = true ] || exit 0

# `curl ... | sh`: stdin is this script, so setup would see no terminal and refuse to
# prompt. Give it the terminal the user is sitting at, when there is one.
if [ ! -t 0 ] && (: </dev/tty) 2>/dev/null; then
  exec </dev/tty
fi
if [ "$upgrade" = true ]; then
  exec "$dir/current/bin/hermes-slim-setup" --gateway-only --service "$@"
fi
exec "$dir/current/bin/hermes-slim-setup" --service "$@"
