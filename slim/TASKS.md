# hermes-slim task list

## Phase 1: install + gateway
- [x] Org `hermes-slim` (wheels), fork `talos-hq/hermes-slim` (moved from hermes-slim/hermes), `upstream` branch mirroring NousResearch main
- [x] Repo `hermes-slim/wheels` + cryptography 50.0.2 macOS x86_64 wheel job (static OpenSSL)
- [x] `slim/build_bundle.py`: relocatable CPython + locked deps (`--no-build`) + source + launchers
- [x] `slim/setup.py`: TUI prompts / flags / env vars, validation, OpenRouter + DeepSeek autofill, gateway health
- [x] Hide Nous Portal / login / models when `HERMES_SLIM=1`
- [x] `slim/smoke_test.py` end-to-end client test
- [x] Unit tests (`slim/tests`)
- [x] `slim-bundle.yml`: build + smoke on win32-x64, darwin-x64, darwin-arm64, linux-x64-musl; release on `v*` tags
- [x] `slim-upstream-sync.yml`: daily fast-forward of `upstream` + sync PR
- [x] `install.sh` / `install.ps1` client installers
- [x] Green CI on all four targets (PR #1)
- [x] Disable upstream's own workflows in the fork (57 disabled via the Actions API)
- [ ] Add repo secrets: `SYNC_TOKEN` (repo+workflow), optional `OPENROUTER_API_KEY` for the live test
- [x] First release tag `v0.1.0`
- [x] Pass `HERMES_SLIM=1` into the gateway service environment (written to `~/.hermes/.env`)

## Phase 2: Telegram
- [ ] Prompt for Telegram user id (numeric) and 4 bot tokens; show BotFather the username
      `hermes_<first>_<last>_<N>_bot` (N = 1..4, helper `bot_username()` exists)
- [ ] Validate tokens with `getMe`; check the returned username matches
- [ ] Decide: 4 profiles (one per bot) vs. one gateway with 4 bots

## Signing
- [x] Windows: PSF-signed CPython (NuGet) instead of unsigned python-build-standalone
- [x] Windows: CI signs every unsigned wheel binary and verifies all with Get-AuthenticodeSignature
- [ ] Create the Azure Artifact Signing account + certificate profile; add the six `AZURE_*` repo secrets
- [ ] macOS: Developer ID signing + notarization
