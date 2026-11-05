# Shared Brain integration

Hermes can use the separately installed `brain` CLI for knowledge shared across profiles
and delegated agents. This is additive to profile-local `MEMORY.md`; it does not activate
or replace an external memory provider. `hermes memory status` continues to report the
provider configuration independently.

When `brain` is on PATH, the first user message (up to 2,000 characters) and the session's
working-directory name form a `brain search ... -k 12 --json` query. Only matching entries
are injected. Recall is capped at 12 whole entries and 1,500 UTF-8 bytes, including its
`<shared_brain>` framing. This conservative byte budget also bounds byte-level tokenizers
to 1,500 tokens. Oversized entries are skipped; later smaller entries can still fit.
Content is JSON-escaped and identified as untrusted reference data.

The snapshot is frozen into the session's system prompt, persisted with it, and never
queried again during a continuation. A resumed session retains its original snapshot;
new lessons become visible to new sessions. Missing CLI, empty results, malformed output
or searches exceeding 300 ms do not block the conversation. The process-start overhead
and successful searches can be measured with the benchmark below.

To disable recall and capture guidance for a profile, set this in its `config.yaml`:

```yaml
shared_brain:
  enabled: false
```

The existing CLI owns the location and schema. `HERMES_BRAIN_DIR` is passed unchanged to
all CLI subprocesses; set it to a disposable directory for testing. With no override,
the CLI uses its existing shared store. Do not set it to a profile-local memory directory.
Sharing across profiles is intentional here: do not put private profile data into this store.

## Capture an explicit outcome

Agents with terminal access, including delegated agents, receive capture guidance even
when search returns no matches. They can run `brain add --kind fact --scope repo:NAME --
'Reusable discovery'`. The exclusion of the built-in `memory` tool for children remains:
that tool writes profile-local `MEMORY.md`, not the brain.

After a confirmed failed command and verified fix, an explicit post-task step can record both:

```bash
python -m agent.shared_brain --scope repo:widget \
  --failure 'Widget compilation failed because the include directory was missing' \
  --solution 'Adding the include directory fixed widget compilation; verified build passed'
```

Use the Hermes interpreter for this command. Omit `--solution` until the fix is verified.
The helper invokes `brain add` for each supplied outcome. CLI content-hash dedupe makes
retries safe, including interruption between the two writes. Store concise patterns and
confirmed causes, never secrets, raw output or guesses. This is an explicit capture path
and model guidance, not an automatic classifier of arbitrary terminal output. Whether a
live model follows that guidance requires a separate live evaluation.

## Validate and measure

```bash
scripts/run_tests.sh tests/agent/test_shared_brain.py
python scripts/benchmark_shared_brain.py
```

Use the prepared Hermes test interpreter for the benchmark. Tests require the existing
CLI on PATH and use a temporary `HERMES_BRAIN_DIR`. They cover bounded formatting, dedupe,
parallel cold-start writes, frozen snapshots, disabled/failure paths, and two separate
real `AIAgent` processes: A writes through terminal; B sees the lesson in its API prompt.
The API client is scripted locally. No external API credentials are needed.

The benchmark runs the same fixed task through `AIAgent` with integration off/on and a
scripted model. It reports actual prompt characters, model tool calls and real CLI search
latency. Model token usage is unavailable. It measures integration overhead and prompt
growth, not a reduction in live model tool calls or improvement in task success.
