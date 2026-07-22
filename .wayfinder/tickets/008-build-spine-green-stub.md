<!-- labels: wayfinder:task, wayfinder:closed -->
# Build the minimal spine green on the stub adapter, unit-tested

Assignee: PinetheApple
Blocked by: #002, #003, #004, #005, #006, #007
Status: closed

## Question

Build the from-scratch core to green: the spine (#002) over the task model (#003), the
validation gate (#004), the live observability feed (#005), the user-in-loop verbs
(#006), against the `stub` adapter (#007) — with a stdlib `unittest` suite that runs the
whole loop end-to-end token-free. Pyright-strict, ruff clean, files ≤400–500 lines.

Execution ticket (in scope per the map's Notes). Answer records the SHA, the modules
built, and the passing test evidence. No `claude` calls yet — stub only, so it's provable
without tokens.

## Resolution

From-scratch core built to green at `ads/` (2282 LOC, 15 modules, all ≤500 lines),
stdlib-only except the import-guarded `rich` feed. **Three gates green** (verified from
repo root against `pyproject.toml` strict config): `uv run pyright` → 0 errors/0 warnings;
`uv run ruff check ads tests` → clean; `uv run python -m unittest discover -s tests` →
**Ran 23 tests, OK** — and the suite passes with **`rich` not installed**, proving the
token-free floor and the §7 import-guard.

**Modules** (each maps 1:1 to a contract): `_literal.py` (validate_literal, verbatim);
`layout.py` (RunLayout, core paths only); `tasks.py` (#003 — 4-status Task, 5-key
frontmatter parser/serializer, **segment-aware `owns` overlap** replacing legacy
exact-match, `ready_batch`, 3-color-DFS `check_acyclic`); `task_io.py`;
`adapters/base.py` (#007 — 2-method `Adapter` Protocol, `RunResult{text, exit_status}`,
`Role`/`ExitStatus`, no `structured`/`capabilities`/`tier`); `adapters/stub.py`
(role-keyed canned JSON text → one driver-side parse path); `phase_json.py` (#007/#002 —
`parse_phase_payload` + typed plan/handoff/verdict extractors, moved out of the adapter);
`state.py` (#002+#005 — 10-field state incl. `event_seq` + `question`, atomic
temp+`os.replace`, open-emit `append_event`); `validate.py` (#004 — cmd + cold-critic
judgment, structural anti-forgery: empty-`cited_paths` **and** hallucinated-path auto-fail,
`check_leaf_judgment` plan-commit gate); `feed.py` (#005 — loop_fmt-adapted `render` +
pinned footer, rich import-guarded, non-TTY skips footer); `prompt.py`; `driver.py` (#002 —
hard-coded transition table, ready-set recompute, blocking-handoff halt, attempts ceiling,
`awaiting_signoff`→`done` only via user approve); `cli.py` (#006 — 7 verbs
init/start/approve/reject/answer/status/resume, blocking-drain).

**Real bug caught by tests:** `owns_diff` needed `git add -N` before `git diff <baseline>`
— untracked new files (the common case for a task's `owns`) otherwise vanish from the diff,
silently starving the cold critic of evidence.

Tests (`tests/`, real temp git repo + real subprocess, stub adapter = 0 tokens): full loop
end-to-end (init→start→approve-plan→execute→validate→awaiting_signoff→approve→done),
append-only gap-free `events.jsonl`, stale/matching `--at`, table-driven `_owns_overlap`
(incl. the `src/tui` vs `src/tui-old` false-positive trap) + `check_acyclic` + `ready_batch`,
and all four anti-forgery/plan-gate cases.

SHA: pending — working tree left uncommitted for user review (no commit without ask).
Next: **#009** swaps stub → claude-code adapter and drives the maiden self-hosted task.
