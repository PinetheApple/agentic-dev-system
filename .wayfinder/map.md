<!-- labels: wayfinder:map -->
# Map — Minimal ADS core loop that self-hosts

## Destination

A **from-scratch minimal ADS core loop**, driven off a preserved `SPEC.md` + `PLAN.md`,
that runs **one real task end-to-end** with **basic observability at the core** — and
whose proving ground is the loop **building out the rest of ADS itself** (curses TUI,
sandbox, further features). Scope is agreed with the user up front; **"done" is declared
by the user**. Every session orients here before choosing a ticket.

## Notes

- **Domain:** agentic dev-loop / "structured Ralph" — stateless-per-iteration control
  loop that reconstructs state from disk, runs a task, validates, persists. The mature
  reference is `music_app`'s `project-loop` skill + `scripts/run-loop.sh` +
  `scripts/loop_fmt.py`; the overbuilt prior generalization is this repo's existing
  `ads/` (5.2k LOC, never landed one real task — mine it for contract detail, discard
  the rest).
- **This effort carries execution into the map** (overrides Wayfinder's plan-don't-do
  default): the destination is a *working* loop, not a spec to hand off, so build/prove
  tickets (#008, #009) are in scope.
- **Standing preferences:** scope agreed up front; done declared by user; SPEC is
  user-owned (never silently edited — unambiguous gaps → decide + record, ambiguous →
  stop-point). Clean-code / SOLID / KISS / YAGNI, files ≤400–500 lines, minimal
  comments, zero-runtime-dep stdlib Python, pyright-strict + ruff, `uv`/`unittest`.
- **Skills to consult:** `/grilling` + `/domain-modeling` for contract tickets;
  `/prototype` where a state model needs a cheap concrete artifact; `python-expert`.
- **Key reference:** `.wayfinder/references/missions-factory-talk.md` — Factory's
  "Missions", external production-proven corroboration of this shape (three-role
  orchestrator/worker/validator, plan-time validation contract, serial-over-parallel,
  structured handoffs, model-per-role, bitter-lesson prompts-not-state-machine). Load it
  when resolving any contract ticket (#002–#007).
- **Settled while charting (frame every ticket honors):** rescope from scratch, same
  repo + new branch, keep git history + README as raw spec material; single spine
  intake→plan→execute(one task)→validate→user-sign-off; single adapter (Claude Code +
  stub); keep the task DAG + disjoint-`owns` model in the data layer even though the
  executor is serial (it's the seam parallelism graduates through); observability =
  `loop_fmt`-style live terminal feed + pinned status footer + tee to `events.jsonl`;
  first self-hosted task is the curses TUI.

## Decisions so far

<!-- index — one line per closed ticket, then zoom to the ticket for detail -->

- [Distill the canonical SPEC.md + PLAN.md for the rescoped core](tickets/001-distill-canonical-spec-plan.md) — `SPEC.md`+`PLAN.md` written on new branch `minimal-core` (user-signed-off); the frame (spine, 8 invariants, user-in-loop + adapter contracts) with exact schemas left to #002–#007; review gate is **conditional** — one for no-design work, two (spec→design) for design work.
- [Core phase-spine contract](tickets/002-core-phase-spine-contract.md) — deterministic skeleton / prompt meat (phase sequencing hard-coded, all reasoning in adapter); **9-field `state.json`** (`phase, review_stage, gate, tasks, attempts, cursor, halt_reason, adapter, updated_at`); hard-coded transition table w/ per-task interleaved validate + `attempts`-ceiling halt; uniform `{ok, payload, error}` run() envelope, per-phase payload; structured handoff `{task_id,status,commands,undone,issues}` where `blocking:true`→halt-to-user (self-unblock via research lives in the execute prompt); `events.jsonl` open-emit w/ ~11 documented core kinds. Bitter-lesson split settled here (does not become its own ticket).
- [Task data model: DAG + disjoint-owns, serial ready-set (the parallelism seam)](tickets/003-task-data-model-parallelism-seam.md) — pruned/sharpened `ads/tasks.py`. Core frontmatter **`id, status, depends_on, owns, exit_criteria`** (`status: pending→active→done|failed`, `failed` terminal-blocks-dependents); dropped `parent/critical/expert/tier` (each defers with its feature; `tier`→#007). `exit_criteria` inline (one file = task + its done; #003 owns field, #004 owns semantics). **`owns`** = repo-rel file/dir paths, overlap = **segment-aware prefix** (`src/tui` collides `src/tui/app.py`, not `src/tui-old`) — safer than exact-match's missed-conflict failure; globs YAGNI. **Seam:** ship `ready_batch()` (full disjoint computation) now; serial executor runs `[0]` + recomputes each iter; graduation = pure executor swap. **Read-only parallelism graduates first, implicitly**: `owns: []` conflicts with nothing, no `read_only` flag; "stayed in lane" enforcement pushed to #004's cold-critic diff. Acyclicity = 3-color DFS (cycle + unknown-dep, one pass), run **once as plan-commit gate** before any task file written, trusted at execute-time.
- [Validation contract: cmd + cold-critic cited_paths, forgery-proof](tickets/004-validation-contract-forgery-proof.md) — two author-agnostic gates/leaf: driver `cmd` (exit code = verdict, 300s, no sandbox) + cold-critic `judgment` (fresh context fed only `spec.md` + git `owns`-diff vs pre-exec baseline, in-place; verdict `{pass,evidence,cited_paths}`, tier-named `"validate"` on same model). Anti-rubber-stamp: empty `cited_paths` **or** any cited path absent from the diff → auto-fail. ≥1 judgment mandatory per leaf (plan-graph enforced); `cmd` optional. #004 = verdict + scratch-feedback; retry loop + bound → #002. Audit via `events.jsonl` only. Integration critic → fog.
- [Observability contract: loop_fmt-style live feed + pinned footer + jsonl tee](tickets/005-observability-contract-live-feed.md) — one driver-driven **live feed** (adapts `loop_fmt.py`, not rewrite) + `events.jsonl` tee. Two layers, one feed: **loop events** (coarse, persisted audit) interleaved with **Claude stream-json** (fine, ephemeral, never persisted) — driver **wraps the stream, re-emits one merged pipe**; `render()` dispatches by shape. Resume **replays `events.jsonl`** for coarse skeleton (display-only, not state). Line = frozen 5-field envelope `{ts,seq,phase,type,task}` + free-form `data`; `seq` = monotonic counter → **adds 10th field `event_seq` to #002's `state.json`**. Footer pins `elapsed · phase+task · N/total tasks · tokens/ctx · $cost` — progress = leaf-count, cost **display-only** (no ceiling; enforcement = deferred control-verb). **User relaxed SPEC §7**: `rich` is a blessed dep for the footer (well-tested lib > hand-rolled TUI; also informs TUI #009); non-TTY skips footer.
- [User-in-the-loop contract: scope up front, done by user](tickets/006-user-in-the-loop-contract.md) — **blocking-drain**: `start` drives foreground to a gate then halts+exits; each verb re-drains (halts = crash-safe re-entry, no daemon). **7 verbs** `init/start/approve/reject/answer/status/resume`. **State-driven single `approve`** clears whatever gate `state.json` names (`--at <gate>` = stale-approve guard). Halt-states: `awaiting_plan_approval` | `awaiting_spec_approval`→`awaiting_design_approval` (2-gate design work, approved spec frozen) | `awaiting_clarification` (agent question in state+event → `answer`=fact / `approve`=proceed-as-assumed) | `awaiting_signoff`. **Sign-off teeth**: loop has **no** path that writes `done`; validate-pass → `awaiting_signoff` only; `awaiting_signoff→done` fires solely on user `approve` (one signoff after full-graph validate). **Gap branch**: plan phase classifies (prompt-driven); unambiguous → `gap_decided` event, no halt, flows on; ambiguous → `awaiting_clarification`. Feeds #002 (halt-states + clarification field in `state.json`; gate/`gap_decided` events) and #008 (the 7-verb CLI).
- [Adapter Protocol for the spine: Claude Code + stub only](tickets/007-adapter-protocol-spine.md) — two-method Protocol `run()`+`resolve_model(role)` over thin `RunResult{text, exit_status}`; **model axis = role** (planning/execution/validation, phase-derived; adapter maps `role→model` via `harness.toml`; task-size tier deferred); **driver parses phase JSON** — adapter owns only its transport envelope (stub emits same JSON text → one parse path); `capabilities()`/`sync()` dropped as deferred-feature hooks; streaming crosses via **`on_event` sink callback** (driver wires to #005 feed + `events.jsonl` tee, not a file path). Fixes seams: #002 owns `parse_phase_payload`, #005 consumes the sink, OpenCode satisfies same 2 methods.
- [Build the minimal spine green on the stub, unit-tested](tickets/008-build-spine-green-stub.md) — from-scratch core landed at `ads/` (2282 LOC, 15 modules ≤500 lines, stdlib-only + import-guarded `rich`). **Three gates green** (pyright strict 0-err · ruff clean · 23 `unittest` OK — passes with `rich` absent, proving the token-free floor). Contracts #002–#007 realized 1:1; segment-aware `owns` overlap, structural anti-forgery (empty **and** hallucinated `cited_paths` auto-fail), `awaiting_signoff`→`done` only via user approve. Real bug caught by the suite: `owns_diff` needs `git add -N` or brand-new files vanish from the critic's diff. Working tree left uncommitted (SHA pending user review). Next: #009 swaps stub→claude for the maiden self-hosted task.

## Not yet specified

<!-- in-scope fog: graduates to tickets as the frontier advances -->

- **Deferred-feature backlog** the green spine self-hosts (each graduates once the spine
  proves out): net-sandbox / firewall containment posture; real multi-task parallelism
  (concurrent executor + scheduling on the disjoint-`owns` seam); interactive curses TUI
  beyond the maiden task; async control verbs (pause/resume/redirect/edit/replan/abort);
  escalation flow; resume/reconcile hardening after crash.
- **OpenCode (2nd adapter)** — proves the harness-agnostic claim; graduates after the
  Claude-Code spine is green.
- **How much of old `ads/` is salvaged vs discarded** per module — sharpens once the
  contracts (#002–#007) fix what the new core needs.
- **Behavioural / "user-testing" validator** (spawn the app, computer-use, verify
  functional flows end-to-end — Missions' second validator) — deferred; also gated by
  the no-auto-UI-verify preference (browser/visual verification only on request).
- **Integration critic** (once-per-run cold critic over the full merged diff +
  `attribute_paths` routing to owning tasks — Missions' cross-task seam gate, old
  `ads/validate.py`) — deferred by #004; graduates as another critic call when multi-task
  cross-seam bugs actually bite. Couples to worktree/merge hardening.

## Out of scope

<!-- ruled beyond this destination; never graduates -->

- Distribution / packaging polish (making ADS an installable product).
- Changing `music_app`'s own `project-loop` — it's the reference, a separate effort.
