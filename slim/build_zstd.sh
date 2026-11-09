#!/bin/sh
# Build a standalone decompressor for clients without zstd. Linux builds need
# build-base (Alpine) and link musl statically; macOS links only system libraries.
#
#   sh slim/build_zstd.sh OUT_DIR
set -eu
out="$1"
case "$(uname -s)-$(uname -m)" in
  Darwin-arm64) target=darwin-arm64; jobs="$(sysctl -n hw.ncpu)"; ldflags= ;;
  Darwin-x86_64) target=darwin-x64; jobs="$(sysctl -n hw.ncpu)"; ldflags= ;;
  Linux-x86_64) target=linux-x64; jobs="$(nproc)"; ldflags=-static ;;
  *) echo "unsupported decompressor build host" >&2; exit 1 ;;
esac
sha256_file() { (shasum -a 256 "$1" 2>/dev/null || sha256sum "$1") | cut -d' ' -f1; }
version=1.5.7
sha256=eb33e51f49a15e023950cd7825ca74a4a2b43db8354825ac24fc1b7ee09e6fa3
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
curl -fsSL -o "$work/zstd.tar.gz" "https://github.com/facebook/zstd/releases/download/v$version/zstd-$version.tar.gz"
[ "$sha256" = "$(sha256_file "$work/zstd.tar.gz")" ] || { echo "zstd source sha256 mismatch" >&2; exit 1; }
tar -xzf "$work/zstd.tar.gz" -C "$work"
make -C "$work/zstd-$version/programs" -j"$jobs" zstd-decompress LDFLAGS="$ldflags" \
  HAVE_ZLIB=0 HAVE_LZMA=0 HAVE_LZ4=0 >/dev/null
mkdir -p "$out"
strip -o "$out/zstd-$target" "$work/zstd-$version/programs/zstd-decompress"
printf '%s  %s\n' "$(sha256_file "$out/zstd-$target")" "zstd-$target" > "$out/zstd-$target.sha256"
"$out/zstd-$target" -V
case "$target" in
  darwin-*)
    otool -L "$out/zstd-$target" | awk '
      NR > 1 { print; if ($1 !~ /^\/usr\/lib\// && $1 !~ /^\/System\/Library\//) bad = 1 }
      END { exit bad }'
    ;;
esac
