# agentic-dev-system

Harness-agnostic driver for an external "structured Ralph" control loop: the
driver owns phase sequencing and state, a swappable adapter owns in-phase
reasoning (Claude Code today; any harness that implements `run()` tomorrow).

## Layout

- `ads/layout.py` — on-disk paths for a run under `.agent/runs/<run-id>/`.
- `ads/state.py` — `state.json` (the only thing the loop reads) with atomic
  writes, plus the write-only `events.jsonl` audit log.
- `ads/tasks.py` — task markdown frontmatter (tight YAML-subset parser, no
  pyyaml), DAG acyclicity check, disjoint-`owns` ready-set computation.
- `ads/config.py` — loads a consuming repo's `.agent/config/` (`harness.toml` +
  `base.md` + `experts/*.md` + `phases/*.md`). The package ships **no** config of
  its own — config belongs to the target repo (ticket 002). See `examples/demo/`
  for a runnable sample.
- `ads/prompt.py` — composes `base + expert + design + task` in memory.
- `ads/adapters/` — `base.py` (Protocol), `claude_code.py` (shells out to the
  `claude` CLI), `opencode.py` (shells out to the `opencode` CLI), `stub.py`
  (canned responses for token-free testing), `_json_envelope.py` (shared
  fence-stripping/phase-JSON parsing both real adapters reuse).
- `ads/driver.py` — the phase state machine.
- `ads/cli.py` — `driver start|resume|approve|reject|status`.

## Phase graph

```
intake -> plan -> review(spec) -> review(design) -> dispatch -> validate -> done
              ^________________________|                  |
              (reject, spec frozen if from design stage)   |
                                        dispatch <----------(exit criteria fail)
```

- `intake`: verbatim copy of user input to `intent.md`. No LLM call.
- `plan`: one `run()` call returns `{spec, design, tasks}` as JSON; the
  driver writes `spec.md`, `design.md`, `tasks/*.md` and validates the task
  graph is acyclic before committing anything.
- `review`: two-stage gate (`review_stage: spec|design`) inside one phase.
  `driver approve` advances spec -> design -> dispatch. `driver reject
  "reason"` appends the reason to the artifact under review and loops back to
  `plan`, bounded by `MAX_RETRIES=2`; a design-stage rejection never
  regenerates the already-approved `spec.md` (freeze-approved-upstream).
- `dispatch`: computes a ready batch (deps satisfied, pairwise-disjoint
  `owns`) and calls `run()` once per task with `base + expert + design.md +
  task body`. Re-run picks up only tasks still `pending` — this is what makes
  resume-after-crash safe.
- `validate` (`ads/validate.py`): three gates, all author-agnostic and
  forgery-proof — no agent self-report counts as done.
  1. **`cmd`** — a driver-executed subprocess (in the task's worktree if one
     is still on disk, else the target repo); the real exit code is the
     verdict.
  2. **`judgment`** — a fresh, cold critic `run()` given `spec.md` + the
     task's on-disk `owns` diff only (never the author's scratch/self-summary),
     returning a structured `{pass, evidence, cited_paths}` verdict. A
     `pass: true` with empty `cited_paths` is auto-failed by the driver — the
     anti-rubber-stamp check is structural, not honor-system. If the adapter's
     `harness.toml` advertises a `code-review` capability, the critic prompt
     drives that skill first; the inline structured-verdict contract is the
     mandatory floor either way.
  3. **integration critic** — one more critic `run()`, once per run after
     every leaf's gates pass and before `done`, over the whole `spec.md` +
     the full merged run diff. Catches cross-task seam gaps no single leaf's
     own gate would see.

  A leaf is done only when all its `cmd` criteria exit 0 and all its
  `judgment` criteria pass with non-empty citations. Failures write feedback
  into that task's `scratch/<id>.md` (ticket 005's resume read-set) and reset
  it to `pending`, looping back to `dispatch`; task-level and integration-level
  failures are each bounded by their own `MAX_RETRIES=2` counter
  (`validate_to_dispatch`, `validate_integration`). An integration failure
  whose cited paths attribute to specific tasks' `owns` retries just those
  leaves the same way; one with no attributable task ("missing work" — no
  task owns the gap) needs 003's resumptive re-split, which doesn't exist yet
  (`TODO(ticket-005-rule-5)` in `ads/driver.py`) — it halts to a human
  immediately instead of guessing. Every validate pass writes
  `validation-report.md` to the run dir (every `cmd`/`judgment` result,
  integration verdict) as a full audit trail, whether or not it blocks.
- Retry exhaustion on any backward edge (`review_to_plan`,
  `validate_to_dispatch`, `validate_integration`) sets `gate: blocked` with a
  `halt_reason` — a human must intervene (edit artifacts, then `driver resume`).
  Humans are otherwise exception-only: a clean run reaches `done` autonomously.
- **Deferred (ticket 007 follow-up):** gates currently run post-merge, in this
  separate `validate` phase — ticket 007's before-merge sequencing (a task
  never merges dirty) would require restructuring `dispatch`'s per-task merge
  step to gate on that task's own `cmd`/`judgment` criteria first; not done in
  this increment (see `ads/dispatch.py`'s module docstring). PR creation
  (ticket 007 §8) is likewise out of scope — no PR step exists yet.

## Usage

The driver operates on a target repo (`--repo`, default cwd) that carries its
own `.agent/config/`. Point it at the bundled sample:

```bash
driver --repo examples/demo --adapter claude-code start "Add a health-check endpoint."
driver --repo examples/demo status
driver --repo examples/demo approve   # spec stage -> design stage
driver --repo examples/demo approve   # design stage -> dispatch, then auto-runs to next halt
driver --repo examples/demo reject "needs more detail on X"
driver --repo examples/demo resume
```

`--adapter stub` swaps in canned responses (see `ads/adapters/stub.py`) for
running the loop without spending tokens — this is what `tests/test_driver_stub.py`
uses.

## Examples

`examples/demo/` is a **sample consuming repo**, not part of the `ads` package:
a minimal `.agent/config/` (placeholder `base`/`experts`/`phases` + a
Claude-Code `harness.toml`) that makes the loop runnable end to end. The
experts (`plan`, `python-expert`, `critic`) are illustrative stubs — a real
target repo ships its own roster (and reuses installed harness skills/agents
before authoring new experts).

### Swapping harnesses

`base.md`, `experts/*.md`, `phases/*.md` and `tasks/` are harness-agnostic —
`harness.toml` is the **only** file that names a provider, model, or run
command. `examples/demo/.agent/config/harness.opencode.toml` is a reference
OpenCode target with the identical shape (same `[tier_model]`/`[run]`/
`[capabilities]` keys, OpenCode's `provider/model` ids, `opencode run` as the
command). To retarget the demo at OpenCode:

```bash
cp examples/demo/.agent/config/harness.opencode.toml examples/demo/.agent/config/harness.toml
driver --repo examples/demo --adapter opencode start "Add a health-check endpoint."
```

Nothing else in `.agent/config/` changes. `harness.toml`'s `[capabilities]`
flags are also where a harness gap becomes visible: the Claude Code target
declares `allowedtools-cli` (it has a real `--allowedTools` flag); the
OpenCode target omits it, because OpenCode has no per-call tool-allowlist
flag — see `ads/adapters/opencode.py` for how `run()` handles that gap
honestly (falls back to `--auto` rather than faking a flag that doesn't
exist).

## Tests

```bash
python -m unittest discover -s tests -v
```

No pytest is installed for this interpreter; the test suite intentionally
uses stdlib `unittest` so it runs anywhere.

## Development

Zero runtime dependencies — `ads/` is stdlib-only. Type checking and linting
are dev-only; install them into the project's pyenv virtualenv with uv:

```bash
uv pip install -e '.[dev]'   # ruff + pyright
```

```bash
pyright ads tests   # strict mode, 0 errors required (see [tool.pyright])
ruff check ads tests   # lint (see [tool.ruff])
ruff format ads tests  # formatting
python -m unittest discover -s tests -v
```
