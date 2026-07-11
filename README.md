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
  `claude` CLI), `stub.py` (canned responses for token-free testing).
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
- `validate`: runs each task's `exit_criteria` — `cmd` via subprocess, `judgment`
  via a critic `run()` call. Failures reset those tasks to `pending` and loop
  back to `dispatch`, bounded by `MAX_RETRIES=2`.
- Retry exhaustion on either backward edge sets `gate: blocked` with a
  `halt_reason` — a human must intervene (edit artifacts, then `driver resume`).

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
