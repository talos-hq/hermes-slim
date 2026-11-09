#!/bin/sh
# Build the universal (arm64 + x86_64) hermes-slim macOS installer package.
#
#   sh slim/macos/build_pkg.sh DIST_DIR OUT.pkg
#
# DIST_DIR holds hermes-slim-<ver>-darwin-arm64.tar.zst and -darwin-x64.tar.zst (+ .sha256).
# Signing is optional and driven by the environment:
#   PKG_SIGN_IDENTITY   "Developer ID Installer: ..." -> productsign
#   NOTARY_KEY_PATH, NOTARY_KEY_ID, NOTARY_ISSUER    -> notarytool submit --wait + stapler
set -eu
dist="$1"; out="$2"
here="$(cd "$(dirname "$0")" && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

mkdir -p "$work/scripts/bundles" "$work/res"
for t in darwin-arm64 darwin-x64; do
  a="$(ls "$dist"/hermes-slim-*-"$t".tar.zst | head -1)"
  [ -f "$a" ] && [ -f "$a.sha256" ] || { echo "build_pkg: missing $t bundle or its .sha256 in $dist" >&2; exit 1; }
  cp "$a" "$a.sha256" "$work/scripts/bundles/"
done
# Version from the arm64 bundle name: hermes-slim-<version>-darwin-arm64.tar.zst
version="$(basename "$(ls "$work"/scripts/bundles/*-darwin-arm64.tar.zst)" | sed 's/^hermes-slim-//; s/-darwin-arm64\.tar\.zst$//')"
cp "$here/postinstall" "$here/../install.sh" "$work/scripts/"
chmod 755 "$work/scripts/postinstall"
cp "$here/conclusion.txt" "$work/res/"
sed "s/@VERSION@/$version/" "$here/distribution.xml.in" > "$work/distribution.xml"

pkgbuild --nopayload --scripts "$work/scripts" --identifier com.talos-hq.hermes-slim \
  --version "$version" "$work/hermes-slim-component.pkg"
productbuild --distribution "$work/distribution.xml" --resources "$work/res" \
  --package-path "$work" "$work/unsigned.pkg"

if [ -n "${PKG_SIGN_IDENTITY:-}" ]; then
  productsign --timestamp --sign "$PKG_SIGN_IDENTITY" "$work/unsigned.pkg" "$out"
  pkgutil --check-signature "$out"
  if [ -n "${NOTARY_KEY_PATH:-}" ]; then
    xcrun notarytool submit "$out" --wait --key "$NOTARY_KEY_PATH" --key-id "$NOTARY_KEY_ID" --issuer "$NOTARY_ISSUER"
    xcrun stapler staple "$out"
    xcrun stapler validate "$out"
  fi
else
  echo "build_pkg: PKG_SIGN_IDENTITY unset; writing an UNSIGNED package" >&2
  cp "$work/unsigned.pkg" "$out"
fi
echo "built $out ($version)"
