# Handoff: universal hermes-slim macOS installer (.pkg)

Branch: `slim/macos-universal-pkg` (talos-hq/hermes-slim). Goal: one installer that
works on **both Intel (x86_64) and Apple Silicon (arm64) Macs**, signed and notarized
with our Developer ID credentials, verified nightly on real GitHub-hosted Macs.

## Design

The slim bundles are per-architecture (each carries a native CPython), so "universal"
means one `.pkg` that carries **both** `darwin-arm64` and `darwin-x64` bundles and
installs the right one:

| File | Role |
|---|---|
| `slim/macos/build_pkg.sh DIST OUT.pkg` | `pkgbuild --nopayload --scripts` + `productbuild`; optional `productsign`, `notarytool`, `stapler` driven by env (`PKG_SIGN_IDENTITY`, `NOTARY_KEY_PATH/ID/ISSUER`). |
| `slim/build_zstd.sh OUT_DIR` | Builds a standalone native decompressor (macOS links only system libraries) from hash-pinned sources. Both macOS binaries and checksums are embedded with the archives: Apple's tar needs an external zstd, which is absent from Installer's minimal PATH. |
| `slim/macos/distribution.xml.in` | `hostArchitectures="arm64,x86_64"` so Installer runs natively on both (no Rosetta prompt). |
| `slim/macos/postinstall` | Runs as root. Picks `darwin-arm64` if `sysctl hw.optional.arm64` = 1 (true even under Rosetta), else `darwin-x64`. Resolves the user (`SUDO_USER`, else console owner), stages the archive in the system temporary directory, runs `install.sh --archive … --no-setup` **as that user**. Log: `/var/log/hermes-slim-install.log`. |
| `slim/install.sh` | New `--no-setup` flag: stop after linking `~/.local/bin/hermes` (an installer has no TTY for setup prompts). On stock macOS without zstd, fetch and verify the native standalone decompressor published beside the bundles, also supporting offline adjacent files. |
| `slim/macos/verify_pkg.sh PKG` | Real-machine checks T1–T7 (below). |
| `.github/workflows/slim-macos-installer.yml` | Nightly 07:17 UTC + `workflow_dispatch` + PRs touching these paths. Jobs: `bundle` (arm64 on `macos-15`, x64 on `macos-15-intel`) → `pkg` (env `release-signing`) → `verify` (both runners). |

The user explicitly asked for this workflow file; it is not covered by the
"no new workflows" rule.

## Your job (Codex)

1. Push the branch, open a PR to `main`, and drive `slim-macos-installer` to green
   on **both** `verify` legs via remote CI (`gh run watch`, `gh run view --log-failed`).
   Use `workflow_dispatch` (`gh workflow run slim-macos-installer.yml --ref slim/macos-universal-pkg`)
   when the PR trigger does not fire or the `release-signing` environment blocks PR runs.
2. Fix whatever breaks, in these files. Keep `slim/install.sh` POSIX `sh`.
3. Do **not** weaken a test to make it pass; if a check is wrong, fix the check and say why.
4. Report signing status honestly: whether the `CSC_LINK` p12 contains a
   **Developer ID Installer** identity. If it holds only "Developer ID Application",
   `productsign` cannot work: report that a `CSC_INSTALLER_LINK` /
   `CSC_INSTALLER_KEY_PASSWORD` secret is needed, and leave the unsigned path green.
   Never print secrets.
5. Post progress on the clud board (`board_post`, to `all`), and finish with the PR URL,
   the run URL, and pass/fail for every test below on each arch.

## Expected tests (all must pass on arm64 AND x86_64)

`verify_pkg.sh` on `macos-15` (arm64) and `macos-15-intel` (x86_64):

| ID | Check | Pass condition |
|---|---|---|
| T1 | Runner arch | `uname -m` = `arm64` / `x86_64` respectively |
| T2 | Universal package | `Distribution` has `hostArchitectures="arm64,x86_64"`; both `darwin-arm64` and `darwin-x64` `.tar.zst` in the expanded Scripts |
| T3 | Signature | Signed builds: `pkgutil --check-signature` shows Developer ID Installer, `spctl --assess --type install` accepts, `stapler validate` OK. Unsigned builds: warning only |
| T4 | Install | `sudo installer -pkg … -target /` exits 0; `~/.hermes-slim/current/bin/hermes` executable, owned by the runner user (not root); `~/.local/bin/hermes` is a symlink |
| T5 | Native bundle | install log has `target=darwin-arm64` / `target=darwin-x64`; `lipo -archs` of the bundled `python3` contains the host arch |
| T6 | Runs | `hermes --version` exits 0; `hermes-slim-setup --help` exits 0 |
| T7 | Reinstall | second `installer -pkg` exits 0, no `hermes-slim.new` left, `hermes --version` still works |

Plus, unchanged and still green:
- `python3 -m unittest discover -s slim/tests -v`
- `slim-bundle` workflow (the `--no-setup` change must not alter default `install.sh` behaviour).
- `sh -n slim/install.sh slim/macos/postinstall slim/macos/build_pkg.sh`, `bash -n slim/macos/verify_pkg.sh`.

Add a unit test in `slim/tests/` for `install.sh --no-setup` against a fake archive
(it must link `hermes` and not exec setup), runnable on Linux and macOS.

## Known risks to check first

- `release-signing` environment may require approval or disallow PR refs: dispatch on the branch instead.
- Notarization: Apple's notary service may reject the package if it can look inside the
  bundles and finds unsigned Mach-O files. If so, report the log (`notarytool log <id>`).
  The fix is to codesign the bundle's Mach-O files with hardened runtime in the
  `bundle` job. Do it only if notarization actually fails.
- `bundle` job: `build_bundle.py` flags are copied from `slim-bundle.yml`; keep them in sync.
- `postinstall` under `installer` gets a minimal env; `dscl` / `stat -f` are macOS-only by design.

## Done means

PR open against `main`, latest `slim-macos-installer` run green on both `verify` legs,
`slim-bundle` green, the signing outcome stated plainly. Do not merge; report back.
