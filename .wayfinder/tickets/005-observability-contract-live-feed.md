<!-- labels: wayfinder:grilling, wayfinder:closed -->
# Observability contract: loop_fmt-style live feed + pinned status footer + jsonl tee

Assignee: PinetheApple
Blocked by: #001
Status: closed

## Question

What is the core observability surface — modelled on `music_app`'s `loop_fmt.py`: a
colored tag per event streamed live to the terminal, plus a **pinned footer showing
current project/task status**, with raw events tee'd to `events.jsonl`? What events does
the loop emit, what does the footer show, and do we adapt `loop_fmt.py` or write fresh?

Basic observability is a *core* pillar (not deferred). The interactive curses TUI is the
deferred #009 feature — this is the lighter always-on feed. Missions' "mission control"
(see the reference) says the footer must answer two glance-questions for a long run: **how
much is complete** and **how much budget/tokens is burned** — fold token/budget burn into
the footer, not just current task. Parallelizable after #001.

## Resolution

The observability surface = **one live feed process** the driver drives, plus the
`events.jsonl` tee. `loop_fmt.py` is **adapted, not rewritten** — its `render()`
tag-per-line dispatch and pinned footer are lifted and extended.

**Two event layers, one feed:**
- **Loop events** (coarse) — ADS's own audit, appended to `events.jsonl` (invariant #2).
- **Adapter stream** (fine) — Claude's `stream-json` tool-chatter, piped live **only**
  during an in-flight `run()`; **ephemeral**, never persisted (no per-task raw log —
  post-mortem of fine detail = re-run).
- The feed interleaves both live; the durable audit is the coarse skeleton only.

**1. Feed source & merge (Q1, Q6).** Both layers interleaved live. The **driver wraps
Claude's stream and re-emits each line into a single feed pipe** — one process, natural
ordering. `render()` dispatches by shape: ADS envelope → ADS tag line; Claude `type` →
existing loop_fmt tag line.

**2. Crash / resume (Q2).** On resume the feed **replays `events.jsonl`** to redraw the
coarse skeleton, then continues. Display-only replay — does **not** violate "loop never
reads `events.jsonl` for state" (`state.json` still owns reconstruction).

**3. Event line schema (Q3).** Frozen 5-field envelope, free-form `data`:
```json
{"ts":"2026-07-22T14:03:01Z","seq":42,"phase":"execute","type":"task:start","task":"core-executor","data":{}}
```
- `seq` = monotonic counter **persisted in `state.json`** (resume-safe, gap-free).
  **Cross-ticket:** #002 froze a 9-field `state.json`; this adds a 10th field
  (`event_seq`). It must live there — `state.json` is the *only* reconstruct-state
  file (invariant #1), so the counter can't sit in a side-file.
- `data` is per-type free-form, but each `type` documents which keys the feed renderer
  reads (e.g. `validate:verdict` → `data.pass`, `data.cited_paths`). Airtight where
  consumed, flexible in the tail.
- **Taxonomy (open to grow):** `run:start`, `phase:enter`, `plan:done`, `review:gate`,
  `review:verdict`, `task:start`, `task:done`, `validate:cmd`, `validate:critic`,
  `validate:verdict`, `done`, `error`.

**4. Footer (Q4).** Pins: `elapsed · phase + active task · N/total tasks · token/context
stats · $ cost`. Progress = **leaf-count `N/total`** (from the `state.json` task graph).
Cost/tokens **display-only** — no budget ceiling / enforcement (that's a deferred
control-verb, SPEC §6). Token/context/cost harvested from Claude's stream usage (as
loop_fmt does); task-count from `state`.

**5. Renderer & the §7 exception (Q5).** The feed is core (invariant #7) but the pinned
live-ticking footer is best served by a **well-tested lib, not a hand-rolled TUI**.
**User relaxed SPEC §7's zero-runtime-dependency rule: `rich` is a blessed exception for
the observability feed** (also informs the deferred TUI #009). Per-line tags stay raw
ANSI SGR. **Non-TTY** (redirected/piped) → **skip footer, print lines only**; the
`events.jsonl` tee is independent of the feed.

Assets: extends the `loop_fmt.py` reference; SPEC §7 updated to name the `rich` exception.
