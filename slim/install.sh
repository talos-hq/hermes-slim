#!/bin/sh
# hermes-slim installer for macOS and Linux (glibc or musl).
#
#   sh install.sh [--version vX.Y.Z | --archive FILE] [--dir ~/.hermes-slim] [--upgrade] [--no-setup] [-- <hermes-slim-setup args>]
#
# --upgrade (what `hermes update` runs) keeps the existing configuration and only
# restarts the gateway; without it, setup asks for (or takes) every value.
#
# --no-setup stops after linking `hermes` (the macOS .pkg uses it: an installer has
# no terminal to prompt on, so the user runs hermes-slim-setup afterwards).
#
# --archive installs a bundle already on disk (offline installs, CI); its
# FILE.sha256 is checked when present.
#
# Downloads one prebuilt bundle from github.com/talos-hq/hermes-slim releases,
# verifies its sha256, unpacks it, and runs hermes-slim-setup. Nothing is
# compiled and no package manager runs.
set -eu

repo="talos-hq/hermes-slim"
version="latest"
archive=""
upgrade=false
setup=true
dir="${HERMES_SLIM_DIR:-$HOME/.hermes-slim}"
while [ $# -gt 0 ]; do
  case "$1" in
    --version) version="$2"; shift 2 ;;
    --dir) dir="$2"; shift 2 ;;
    --archive) archive="$2"; shift 2 ;;
    --upgrade) upgrade=true; shift ;;
    --no-setup) setup=false; shift ;;
    --) shift; break ;;
    *) break ;;
  esac
done

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

# Apple's bsdtar shells out to zstd. Prefer the native standalone decompressor
# published beside the bundles rather than assuming a bsdtar has built-in support.
case "$target" in
  linux-x64|linux-x64-musl) zstd_asset=zstd-linux-x64 ;;
  darwin-arm64|darwin-x64) zstd_asset="zstd-$target" ;;
  *) zstd_asset="" ;;
esac
unpack() {
  if command -v zstd >/dev/null 2>&1; then
    zstd -dcq "$1" | tar -xf - -C "$2" --strip-components 1
  elif [ -n "$zstd_asset" ]; then
    if [ -n "$base" ]; then
      echo "→ no zstd here; fetching a standalone decompressor"
      curl -fsSL -o "$tmp/zstd" "$base/$zstd_asset"
      curl -fsSL -o "$tmp/zstd.sha256" "$base/$zstd_asset.sha256"
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

sha256() { (shasum -a 256 "$1" 2>/dev/null || sha256sum "$1") | cut -d' ' -f1; }

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
base=""
if [ -n "$archive" ]; then
  [ -f "$archive" ] || { echo "hermes-slim: no such archive: $archive" >&2; exit 1; }
  if [ -f "$archive.sha256" ]; then
    [ "$(cut -d' ' -f1 "$archive.sha256")" = "$(sha256 "$archive")" ] || { echo "hermes-slim: sha256 mismatch" >&2; exit 1; }
  fi
else
  if [ "$version" = latest ]; then
    base="https://github.com/$repo/releases/latest/download"
    version="$(curl -fsSLI -o /dev/null -w '%{url_effective}' "https://github.com/$repo/releases/latest" | sed 's#.*/##')"
  else
    base="https://github.com/$repo/releases/download/$version"
  fi
  asset="$(curl -fsSL "https://api.github.com/repos/$repo/releases/tags/$version" |
    sed -n "s/.*\"name\": *\"\(hermes-slim-[^\"]*-$target\.tar\.zst\)\".*/\1/p" | head -1)"
  [ -n "$asset" ] || { echo "hermes-slim: no $target bundle in release $version" >&2; exit 1; }
  echo "→ downloading $asset"
  curl -fSL --progress-bar -o "$tmp/$asset" "$base/$asset"
  curl -fsSL -o "$tmp/$asset.sha256" "$base/$asset.sha256"
  [ "$(cut -d' ' -f1 "$tmp/$asset.sha256")" = "$(sha256 "$tmp/$asset")" ] || { echo "hermes-slim: sha256 mismatch" >&2; exit 1; }
  archive="$tmp/$asset"
fi

echo "→ installing to $dir"
mkdir -p "$dir"
rm -rf "$dir/hermes-slim.new"
mkdir "$dir/hermes-slim.new"
unpack "$archive" "$dir/hermes-slim.new" || {
  rm -rf "$dir/hermes-slim.new"
  echo "hermes-slim: could not unpack $archive (incomplete or corrupt download?); the existing install is untouched" >&2
  exit 1
}
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
