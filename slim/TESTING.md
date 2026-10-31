# hermes-slim testing plan

## Layers

| Layer | What | Where | Runs |
|---|---|---|---|
| 1. Unit | name trim/validation, bot username format + 32-char limit, OpenRouter key format, default model, non-interactive missing-value failure | `slim/tests/test_setup.py` (stdlib unittest) | every push/PR, `unit` job |
| 2. Wheel | cryptography macOS x64 wheel: links no non-system OpenSSL (`delocate-listdeps`), imports, Fernet round-trip | hermes-slim/wheels `build.yml` | wheels repo push |
| 3. Bundle build | every dependency installs from wheels with `--no-build` on the target itself | `slim/build_bundle.py` | `slim-bundle` matrix |
| 4. Client smoke | unpack the archive into a fresh dir with a throwaway `HERMES_HOME`, then run the steps below | `slim/smoke_test.py` | `slim-bundle` matrix |
| 5. Live model | `hermes -z "Reply with exactly: PONG"` through OpenRouter → DeepSeek | `slim/smoke_test.py`, only when the `OPENROUTER_API_KEY` repo secret is set | `slim-bundle` matrix |

Smoke steps (layer 4):
1. The bundled interpreter imports every compiled dependency (cryptography's Rust bindings,
   pydantic-core, jiter, Pillow, pillow-heif, psutil, aiohttp, httptools, watchfiles, ...).
2. `hermes --version` runs.
3. Setup rejects bad input non-interactively (blank first name → exit 2).
4. `hermes-slim-setup --non-interactive` with untrimmed names (`"  Ada "`, `" Love lace"`) →
   stored as `Ada` / `Lovelace`.
5. The gateway starts and `GET 127.0.0.1:8642/health` returns `{"status":"ok"}`.
6. `config.yaml` has `provider: openrouter`, `deepseek/deepseek-v4.1-flash` and the OpenRouter
   base URL; `.env` has `OPENROUTER_API_KEY`.
7. `hermes gateway stop` stops it.

## Target matrix

| Target | Runner | Notes |
|---|---|---|
| win32-x64 | `windows-latest` | `.zip`, `.cmd` launchers |
| darwin-x64 | `macos-15-intel` | the case that broke upstream; uses the hermes-slim/wheels cryptography wheel |
| darwin-arm64 | `macos-latest` | |
| linux-x64-musl | `ubuntu-latest` in `alpine:3.22` | musl Python from python-build-standalone |

## Running locally

```sh
python3 -m unittest discover -s slim/tests
# musl, in Docker:
docker run --rm -v "$PWD":/src:ro -v hs-work:/work alpine:3.22 sh -c '
  apk add -q python3 git && git config --global --add safe.directory /src &&
  python3 /src/slim/build_bundle.py --work /work --out /work/dist &&
  python3 /src/slim/smoke_test.py /work/dist/*.tar.zst'
```

## Not covered yet
- The `--service` path (launchd/systemd/scheduled task) is not exercised in CI. Hosted runners
  have no login session for systemd user units, and installing services on them is noisy.
  Manual check per release: run `install.sh` / `install.ps1` on a real machine, reboot, and confirm `/health`.
- The interactive prompts are covered only through `ask()` unit tests, not a TTY driver.
- Telegram (phase 2).
