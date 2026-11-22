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
# unpack that already finished for the same sha256. A bad connection (drops, stalls,
# outages, flaky small files, proxies that ignore ranges) is retried with backoff and
# never turns checksum verification off (slim/DESIGN.md, "Install entry points").
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

# A bad connection is the normal case this has to survive. Every transfer gives up on a link
# that moves under 1 KiB/s for 20 s (a dead Wi-Fi link or a wedged proxy holds the socket
# open and silent, and curl would otherwise wait on it forever); since every fetch resumes,
# abandoning one loses nothing.
curl_net="--connect-timeout 20 --speed-limit 1024 --speed-time 20"

# Sleep before retry N of a fetch that is getting nowhere: 1, 2, 4, 8, 16, then 30 s, so a
# network that is down for a minute is waited out instead of burning every retry inside it.
backoff() {
  _b=1 _n="$1"
  while [ "$_n" -gt 1 ] && [ "$_b" -lt 30 ]; do _b=$((_b * 2)); _n=$((_n - 1)); done
  [ "$_b" -le 30 ] || _b=30
  sleep "$_b"
}

# get URL OUT: fetch a small file (checksum, manifest, helper) through a bad connection.
# 0 = fetched; 2 = the server answered that it does not exist (404/410), which callers may
# accept, unlike 1 = unreachable after ~1.5 minutes of retries. A 503 is never "absent": a
# missing checksum must not be how a flaky network turns verification off.
get() {
  _t=0
  while :; do
    _c="$(curl -fsSL $curl_net -o "$2" -w '%{http_code}' "$1" 2>/dev/null)" || true
    case "$_c" in
      2??) return 0 ;;
      404|410) rm -f "$2"; return 2 ;;
      408|429) ;;
      4??) rm -f "$2"; return 1 ;;
    esac
    rm -f "$2"
    _t=$((_t + 1))
    [ "$_t" -lt 8 ] || return 1
    backoff "$_t"
  done
}

# Total size when the server honours byte ranges (GitHub's release CDN does), else empty.
range_size() {
  _t=0
  while :; do
    _c="$(curl -sSL $curl_net -r 0-0 -D "$tmp/probe.hdr" -o /dev/null -w '%{http_code}' "$1" 2>/dev/null)" || true
    case "$_c" in
      206) tr -d '\r' <"$tmp/probe.hdr" |
             sed -n 's#^[Cc][Oo][Nn][Tt][Ee][Nn][Tt]-[Rr][Aa][Nn][Gg][Ee]: *bytes 0-0/\([0-9][0-9]*\)$#\1#p' | tail -1
           return 0 ;;
      [2-4]??) return 0 ;;  # no ranges here; the one-stream download reports any error
    esac
    _t=$((_t + 1))
    [ "$_t" -lt 4 ] || return 0
    backoff "$_t"
  done
}

# Bytes on disk across a download's pieces (finished bytes plus the attempt in flight).
progress() {
  [ -t 2 ] || return 0
  printf '\r  %3d%%  %d / %d MiB' $(($1 * 100 / $2)) $(($1 >> 20)) $(($2 >> 20)) >&2
}

# Called after each round of a download: stop after ~2 minutes in which no byte arrived
# (a rerun resumes), wait before the next round while there is no progress, and retry at
# once while bytes keep coming.
no_progress() {
  if [ "$1" -gt "$last" ]; then idle=0 last="$1"; return 0; fi
  idle=$((idle + 1))
  if [ "$idle" -gt 7 ]; then
    [ ! -t 2 ] || printf '\n' >&2
    echo "hermes-slim: no download progress for about two minutes ($2); check the connection and rerun to resume" >&2
    exit 1
  fi
  backoff "$idle"
}

# Fetch byte ranges of $1 into $2.parts/<i> concurrently. Each attempt lands in <i>.got and
# joins <i> only when the server answered 206 for exactly the bytes asked, so an error page,
# a stale range or a proxy's whole-file 200 never lands in a part. A part that stopped early
# (dropped, stalled, Ctrl-C, reboot) continues from its last good byte on the next round or
# the next run. Returns 3 when the server sends whole files instead of ranges.
fetch_ranges() {
  url="$1" out="$2" size="$3"
  parts="$out.parts"
  [ "$(cat "$parts/size" 2>/dev/null)" = "$size" ] || { rm -rf "$parts"; mkdir -p "$parts"; echo "$size" >"$parts/size"; }
  chunk=$(( (size + jobs - 1) / jobs ))
  idle=0 last=-1
  while :; do
    missing=0 got=0 i=0
    while [ $((i * chunk)) -lt "$size" ]; do
      part_len=$(( i * chunk + chunk > size ? size - i * chunk : chunk )) have="$(size_of "$parts/$i")"
      [ "$have" -le "$part_len" ] || { : >"$parts/$i"; have=0; }
      got=$((got + have))
      [ "$have" -eq "$part_len" ] || missing=$((missing + 1))
      i=$((i + 1))
    done
    [ "$missing" -gt 0 ] || break
    no_progress "$got" "$url"
    pids="" i=0
    while [ $((i * chunk)) -lt "$size" ]; do
      start=$((i * chunk)) end=$((i * chunk + chunk - 1))
      [ "$end" -lt "$size" ] || end=$((size - 1))
      have="$(size_of "$parts/$i")"
      if [ "$have" -lt $((end - start + 1)) ]; then
        rm -f "$parts/$i.got" "$parts/$i.hdr"
        curl -sSL $curl_net -r "$((start + have))-$end" -D "$parts/$i.hdr" -o "$parts/$i.got" \
          -w '%{http_code}' "$url" >"$parts/$i.code" 2>/dev/null &
        pids="$pids $!"
      fi
      i=$((i + 1))
    done
    while :; do
      alive=0
      for p in $pids; do kill -0 "$p" 2>/dev/null && alive=1; done
      if [ -t 2 ]; then
        now=$got i=0
        while [ $((i * chunk)) -lt "$size" ]; do now=$((now + $(size_of "$parts/$i.got"))); i=$((i + 1)); done
        progress "$now" "$size"
      fi
      [ "$alive" = 1 ] || break
      sleep 1
    done
    wait || true
    pids=""
    whole=false i=0
    while [ $((i * chunk)) -lt "$size" ]; do
      if [ -f "$parts/$i.code" ]; then
        start=$((i * chunk + $(size_of "$parts/$i")))
        case "$(cat "$parts/$i.code")" in
          206) if tr -d '\r' <"$parts/$i.hdr" | grep -qi "^content-range: *bytes $start-[0-9]*/$size\$"; then
                 cat "$parts/$i.got" >>"$parts/$i"
               fi ;;
          200) whole=true ;;
        esac
        rm -f "$parts/$i.got" "$parts/$i.hdr" "$parts/$i.code"
      fi
      i=$((i + 1))
    done
    if [ "$whole" = true ]; then rm -rf "$parts"; return 3; fi
  done
  [ ! -t 2 ] || printf '\n' >&2
  i=0
  : >"$out.tmp"
  while [ $((i * chunk)) -lt "$size" ]; do cat "$parts/$i" >>"$out.tmp"; i=$((i + 1)); done
  mv "$out.tmp" "$out"
  rm -rf "$parts"
}

# One stream into $2.part, resumed (-C -) from its last byte after every drop or stall.
fetch_stream() {
  url="$1" out="$2" idle=0 last=-1
  if [ -t 2 ]; then show="-#"; else show="-s"; fi
  while :; do
    rc=0
    code="$(curl -fSL $show $curl_net -C - -o "$out.part" -w '%{http_code}' "$url")" || rc=$?
    if [ "$rc" = 0 ]; then mv "$out.part" "$out"; return 0; fi
    case "$code" in
      401|403|404|410) echo "hermes-slim: $url answered HTTP $code" >&2; exit 1 ;;
    esac
    # 33: the server cannot resume, so start this file over.
    [ "$rc" != 33 ] || rm -f "$out.part"
    no_progress "$(size_of "$out.part")" "$url"
  done
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
    if ! fetch_ranges "$url" "$out" "$size"; then
      echo "→ the server sends whole files, not byte ranges; downloading as one stream"
      fetch_stream "$url" "$out"
    fi
  else
    echo "→ downloading $(basename "$out")"
    fetch_stream "$url" "$out"
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
  get "$base/$zstd_asset" "$tmp/zstd" &
  helper_pids="$!"
  get "$base/$zstd_asset.sha256" "$tmp/zstd.sha256" &
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
        { get "$base/$zstd_asset" "$tmp/zstd" && get "$base/$zstd_asset.sha256" "$tmp/zstd.sha256"; } ||
          { echo "hermes-slim: could not download $zstd_asset from $base" >&2; exit 1; }
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
pids=""
trap 'rm -rf "$tmp"' EXIT
# Ctrl-C stops the background range and helper transfers too; their bytes stay for a rerun.
trap 'for p in $pids $helper_pids; do kill "$p" 2>/dev/null; done; exit 130' INT TERM
mkdir -p "$dir/downloads"
base=""
expected=""
case "$archive" in
  http://*|https://*)
    base="${archive%/*}"
    start_helper
    # A mirror may publish no checksum (404), but one it failed to serve is an error.
    rc=0
    get "$archive.sha256" "$tmp/archive.sha256" || rc=$?
    case "$rc" in
      0) expected="$(cut -d' ' -f1 "$tmp/archive.sha256")" ;;
      2) echo "→ no $archive.sha256 on this mirror; installing unverified" ;;
      *) echo "hermes-slim: could not download $archive.sha256; check the connection and rerun" >&2; exit 1 ;;
    esac
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
    if get "$manifest_base/hermes-slim-manifest.txt" "$tmp/manifest"; then
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
      get "$base/$asset.sha256" "$tmp/$asset.sha256" ||
        { echo "hermes-slim: could not download $asset.sha256; check the connection and rerun" >&2; exit 1; }
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
