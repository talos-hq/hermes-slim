#!/bin/bash
# Install the universal hermes-slim .pkg on this real Mac and verify the result.
#   EXPECT_ARCH=arm64|x86_64 EXPECT_TARGET=darwin-arm64|darwin-x64 bash verify_pkg.sh Hermes-Slim.pkg
set -euo pipefail
pkg="$1"
fail() { echo "::error::$*"; exit 1; }
step() { echo; echo "== $*"; }

step "T1 host is the expected architecture"
[ "$(uname -m)" = "$EXPECT_ARCH" ] || fail "runner is $(uname -m), expected $EXPECT_ARCH"

step "T2 package declares both architectures"
pkgutil --expand-full "$pkg" "$RUNNER_TEMP/expanded"
grep -q 'hostArchitectures="arm64,x86_64"' "$RUNNER_TEMP/expanded/Distribution" || fail "Distribution lacks hostArchitectures"
for t in darwin-arm64 darwin-x64; do
  ls "$RUNNER_TEMP"/expanded/*.pkg/Scripts/bundles/hermes-slim-*-"$t".tar.zst >/dev/null || fail "package lacks the $t bundle"
done

step "T3 signature (signed builds only)"
if pkgutil --check-signature "$pkg"; then
  pkgutil --check-signature "$pkg" | grep -q "Developer ID Installer" || fail "package signature lacks Developer ID Installer"
  spctl --assess --type install -vv "$pkg" || fail "Gatekeeper rejects the signed package"
  xcrun stapler validate "$pkg" || fail "notarization ticket not stapled"
else
  echo "::warning::package is unsigned"
fi

step "T4 installs with the system installer"
sudo installer -pkg "$pkg" -target / -verboseR
dir="$HOME/.hermes-slim/current"
[ -x "$dir/bin/hermes" ] || fail "$dir/bin/hermes missing"
[ -L "$HOME/.local/bin/hermes" ] || fail "~/.local/bin/hermes not linked"
[ "$(stat -f %Su "$dir")" = "$(id -un)" ] || fail "install is owned by $(stat -f %Su "$dir"), not $(id -un)"

step "T5 the native bundle was chosen"
grep -q "target=$EXPECT_TARGET" /var/log/hermes-slim-install.log || fail "postinstall did not pick $EXPECT_TARGET"
py="$(find "$dir" -path '*/bin/python3*' -type f -perm -u+x | head -1)"
[ -n "$py" ] || fail "no bundled python found"
file "$py"
lipo -archs "$py" | tr ' ' '\n' | grep -qx "$EXPECT_ARCH" || fail "bundled python is $(lipo -archs "$py"), expected $EXPECT_ARCH"

step "T6 hermes runs"
"$HOME/.local/bin/hermes" --version
"$HOME/.local/bin/hermes-slim-setup" --help >/dev/null

step "T7 reinstall over an existing install is clean"
sudo installer -pkg "$pkg" -target / >/dev/null
[ ! -e "$HOME/.hermes-slim/hermes-slim.new" ] || fail "left a .new tree behind"
"$HOME/.local/bin/hermes" --version

echo; echo "ALL PASS on $EXPECT_ARCH"
