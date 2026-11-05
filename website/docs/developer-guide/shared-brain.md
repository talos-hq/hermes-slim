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

When the CLI is installed and `shared_brain.enabled` is true (the default), main and
delegated agents can call `brain_add(kind, scope, text)`. It invokes the existing `brain add`
CLI with an argument vector, never a shell. Text is capped at 2,048 characters and scope
at 128; binary values, control codes and invalid Unicode are rejected. The CLI alone owns
content-hash dedupe and WAL concurrency. A cold-start SQLite lock failure is retried through
the CLI up to three times, within a five-second total deadline. Other errors are not retried.
Errors (including a five-second write timeout)
return a non-fatal tool result. Operators inspect the same entries with `brain search`.

Delegated agents inherit this capability even when a task narrows its other toolsets,
provided the parent has it enabled. The exclusion of the built-in `memory` tool for children
remains: that tool writes curated profile-local `MEMORY.md`, not the brain. Terminal callers
can still use `brain add --kind fact --scope repo:NAME -- 'Reusable discovery'`.

Every delegation completion, including background and lifecycle API calls, also reuses
`agent.shared_brain.record_outcome`. A failed task with a known error/failure reason records
`kind=failure`. A completed task records `kind=solution` when its summary contains a compact
`Solution:`, `Fix:` or `Verified command:` line identifying a confirmed fix or command. Ordinary `Fixed`/`Resolved` lines that include verification also qualify, as do string
`solution`, `fix` or `verified_command` fields in schema-validated JSON output. No extra model
call is made. Generic completion, interruption, budget exhaustion and unknown
failure causes do not manufacture lessons. Capture uses forced secret redaction, bounds the
entry, and fails open without changing the child's result. Scope is `repo:<child cwd name>`.
As with explicit writes, lesson text is untrusted reference data. A running parent's prompt
stays frozen; a new session's recall snapshot can retrieve the child's entry.

After a confirmed failed command and verified fix, an explicit post-task step can record both:

```bash
python -m agent.shared_brain --scope repo:widget \
  --failure 'Widget compilation failed because the include directory was missing' \
  --solution 'Adding the include directory fixed widget compilation; verified build passed'
```

Use the Hermes interpreter for this command. Omit `--solution` until the fix is verified.
The helper invokes `brain add` for each supplied outcome. CLI content-hash dedupe makes
retries safe, including interruption between the two writes. Store concise patterns and
confirmed causes, never secrets, raw output or guesses. This helper remains an explicit capture path; delegation completion uses the same writer.
Neither path classifies arbitrary terminal output. Whether a live model reports a verified
lesson requires a separate live evaluation.

## Validate and measure

```bash
scripts/run_tests.sh tests/agent/test_shared_brain.py tests/tools/test_brain_add.py
python scripts/benchmark_shared_brain.py
python scripts/benchmark_brain_delegation.py --pairs 20
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

The delegation benchmark alternates 20 paired runs through real `AIAgent` and delegation
lifecycle code, with a scripted API and the real CLI in an isolated brain. Both arms retain
recall; only automatic capture differs. After warming imports and the database, the measured
medians were 169.770 ms off and 262.768 ms on; the median paired overhead was **97.512 ms**.
Baseline median absolute deviation was 13.554 ms: capture was **not below measured noise**
on this short offline task. These numbers do not measure live model latency or quality.
[Raw timing receipt](./shared-brain-results/2026-11-05-delegation.json) contains all samples.
The benchmark prints the same fields for reproduction on the operator's host.

## Live bounded A/B results (2026-11-05)

The fixed experiment used OpenRouter `deepseek/deepseek-v4.1-flash-1017`,
temperature 0, three independent trials per arm per task (18 sessions), 45 seconds
per session, at most three model calls and 256 completion tokens per call.
The tasks were fixed after searching the existing 51-entry brain, before seeing
any model results. Searches for cmake and signing returned no matches; those tasks
were not selected. The selected knowledge concerned soldr PY-003, himalaya SMTP,
and the existing multiplexed Telegram gateway.

Every session ran in a fresh worker with a fresh `HERMES_HOME` and
`HERMES_BRAIN_DIR`. ON was seeded with a frozen set of relevant existing entries
plus an explicit, manually seeded PY-003 failure lesson; OFF used a genuinely
empty brain directory, with the integration enabled in both arms. Both received
the same task prompt, tool schema, model, limits and capture guidance. Trial order
alternated OFF/ON and ON/OFF. No trial was discarded or rerun.

This harness calls the production `agent.shared_brain.prepare_session` adapter,
then a live OpenAI-compatible tool loop with a constrained `check` tool. It does
**not** run the full `AIAgent` terminal loop. The tool writes `candidate.json` and
checks one of these explicit predicates:

- **soldr:** reject `capture_output=True` with PY-003; accept file redirection only
  after an actual Python subprocess exits successfully and its temporary stdout
  file contains exactly `probe-ok`. This is a controlled policy trap, not execution
  of soldr's full lint suite.
- **smtp:** exact outgoing configuration: SMTP, `127.0.0.1:1025`, no encryption,
  account `proton`. No mail is sent and no credentials are accessed by the task.
- **gateway:** exact plan: `hermes-gateway.service`, multiplexing enabled,
  no per-profile unit or host restart, channel enabled. No service is changed.

All numeric cells below are **mean ± sample standard deviation**, N=3 per row.
Tokens are actual provider-reported cumulative prompt/completion tokens across
all calls in each session, including retries and recall. They are not character
estimates. Wall time includes recall and the tool loop, excluding worker startup,
credential resolution and seed creation. Zero tool calls means no candidate was
submitted, not a successful task.

| Task | Brain | Success | Input tokens | Output tokens | Tool calls | Wall ms | Known-bad runs |
| --- | --- | --- | --- | --- | --- | --- | --- |
| gateway | OFF | 0/3 | 552.7 ± 2.9 | 256.0 ± 0.0 | 0.0 ± 0.0 | 2557 ± 320 | 0/3 |
| gateway | ON | 3/3 | 853.3 ± 2.9 | 164.0 ± 19.1 | 1.0 ± 0.0 | 1599 ± 334 | 0/3 |
| smtp | OFF | 0/3 | 545.3 ± 16.1 | 256.0 ± 0.0 | 0.0 ± 0.0 | 3067 ± 1800 | 0/3 |
| smtp | ON | 3/3 | 865.7 ± 2.9 | 218.0 ± 31.6 | 1.0 ± 0.0 | 1430 ± 227 | 0/3 |
| soldr | OFF | 3/3 | 1279.7 ± 19.4 | 319.7 ± 101.6 | 2.0 ± 0.0 | 3774 ± 543 | 3/3 |
| soldr | ON | 3/3 | 749.7 ± 2.9 | 88.3 ± 5.5 | 1.0 ± 0.0 | 1464 ± 609 | 0/3 |

[Per-run receipt](./shared-brain-results/2026-11-05.json) contains all 18 outcomes,
provider response IDs and reported usage/cost, candidates, hashes, and the fixed
prompts and limits. Prompt transcripts and local source paths are omitted.
Provider-reported total cost was **$0.003512799** (not an independent billing audit).

**PROVEN within this experiment:** seeded recall improved bounded task success
from 3/9 to 9/9. For PY-003, OFF repeated the known-bad approach in 3/3 sessions
before correcting it; ON avoided it in 3/3. Both arms ultimately passed soldr
(3/3 each), so that task had **no final-success improvement**. ON used one check
instead of two and mean total tokens fell from 1599.3 to 838.0 (47.6%). This
supports failure avoidance and token efficiency on this controlled task.

**NOT PROVEN:** overall token reduction. SMTP and gateway ON inputs cost more;
mean total tokens were 1083.7 versus 801.3 for SMTP, and 1017.3 versus 808.7 for
gateway (ON versus OFF). OFF produced no tool call in any SMTP/gateway trial,
consuming its 256 completion-token allowance. The success difference is therefore
specific to this strict response budget; larger budgets could change the outcome.
Across all tasks, mean total tokens were 979.7 ON versus 1069.8 OFF (8.4% lower),
but this mixes solved and unsolved tasks and is not a general efficiency result.
N=3 is descriptive evidence, not a significance claim; provider routing and
caching were not controlled, and latency variance is reported rather than hidden.

**NOT PROVEN:** autonomous self-learning (criterion 3), long-term retention,
unseen-task generalization, real SMTP delivery, live gateway provisioning, or
full-agent coding/build performance. The failure lesson was manually supplied;
the harness does not demonstrate autonomous failure capture, lesson verification,
and subsequent improvement. Criterion 2 is supported only for the controlled
PY-003 trap. Criterion 4 has bounded task-level evidence, not universal proof.

Reproduce with a prepared Hermes interpreter and configured provider credentials:

```bash
python scripts/shared_brain_ab.py --dry-run
python scripts/shared_brain_ab.py --live --output .shared-brain-ab/new-run
scripts/run_tests.sh tests/scripts/test_shared_brain_ab.py
```

Dry-run is the default and does not read the brain, resolve credentials, create
artifacts, or call a model. Live execution writes a plan before trials and updates
`results.json` after every session. Unknown usage stays null (serialized request
and completion character counts remain available); failed sessions remain in the
success denominator. Per-session stores prevent trial-to-trial learning or writes
to the real brain. Treat raw local artifacts as potentially private and review
before publishing. Adjust limits explicitly for a separate follow-up experiment.
