<!-- labels: wayfinder:grilling, wayfinder:closed -->
# Adapter Protocol for the spine: Claude Code + stub only

Assignee: PinetheApple
Blocked by: #001
Status: closed

## Question

What is the minimal `run()` Protocol the spine calls — the inputs it's handed (composed
prompt, allowed tools, worktree) and the structured result it must return — such that the
Claude Code adapter (shells out to `claude -p`) and the `stub` adapter (canned responses
for token-free unit tests) both satisfy it, and OpenCode graduates later without changing
the Protocol?

Keep it to claude + stub. One fold from Missions (see the reference): the Protocol should
let a role name its own **model/tier** (planning=careful reasoning, execution=fast
fluency, validation=precise instruction-following, possibly a different provider) so
model-per-role graduates without touching the Protocol — even if the minimal core wires
one model everywhere for now. Grill the boundary. Parallelizable after #001.

## Resolution

The minimal-core adapter boundary is **two methods**, `run()` + `resolve_model()`, over
a **thin** `RunResult`. Four decisions settled with the user (grilling):

```python
Role = Literal["planning", "execution", "validation"]
ExitStatus = Literal["ok", "error"]

@dataclass(frozen=True)
class RunResult:
    text: str
    exit_status: ExitStatus

class Adapter(Protocol):
    def run(
        self,
        prompt: str,
        cwd: Path,
        *,
        role: Role = "execution",
        allowed_tools: list[str] | None = None,
        on_event: Callable[[str], None] | None = None,
    ) -> RunResult: ...

    def resolve_model(self, role: Role) -> str: ...
```

1. **Model axis = role, not task-size tier.** The spine hands the adapter a `role`
   (planning / execution / validation), derived 1:1 from the phase; the adapter maps
   `role → model` via harness config (`role_model` in `harness.toml`). Missions' "right
   model per role" graduates as a config change — the minimal core may point all three
   roles at one model. The old `ads/` `TaskTier` (task-*size*) axis is a **separate**
   knob, **deferred** — the old code conflated size and role; the new boundary carries
   only role.

2. **Driver parses; `RunResult = {text, exit_status}`.** The adapter owns **only** its
   transport envelope (unwrap `claude -p --output-format json`'s result wrapper → the
   model's plain answer text). The **phase-shaped** payload (`spec`/`design`/`tasks`/
   `status`/`verdict`) is the core's own contract, identical across harnesses, so a
   single driver-side `parse_phase_payload(text)` owns it — no `structured` field on
   `RunResult`, no per-adapter re-implementation. Bitter-lesson aligned (dumb transport).
   The `stub` emits the same JSON text the real adapter would, so there is **one** parse
   path exercised token-free.

3. **Protocol = `run()` + `resolve_model()`.** `resolve_model(role)` stays public so the
   #005 footer can display which model each role will use before a run. **Dropped** from
   the old Protocol: `capabilities()` (drove the deferred native-sandbox branch) and
   `sync()` (a claude no-op / deferred session-reconcile) — both are deferred-feature
   hooks, out of the minimal boundary.

4. **Streaming crosses via an `on_event` sink callback**, not a file path. The adapter
   renders each harness event to a compact line and calls `on_event(line)`; the driver
   wires that sink to the #005 live feed **and** the `events.jsonl` tee. In-process and
   testable — the `stub` can drive synthetic events; no file-tailer, no feed↔path
   coupling. (Old code's `activity_log: Path` file-append shape is discarded.)

**Kept from old `ads/` as raw material:** `AdapterName` literal set (`claude-code` /
`stub` / `opencode`), `ExitStatus`, and the `claude -p` envelope-unwrap logic
(`_extract_result_envelope`) — but the phase-JSON parse moves out of the adapter to the
driver. `StructuredPayload` (the giant per-phase union on the old `RunResult`) is
discarded from the boundary; its per-phase shapes are owned by #002/#003/#004.

**Inputs settled without a gate:** `allowed_tools` is an adapter-interpreted list (claude
maps it to space-variadic `--allowedTools`; harnesses without tool-gating, and the stub,
treat it as a no-op). `cwd` is the task worktree. The **hard invariant** that the adapter
never emits `--dangerously-skip-permissions` / `--allow-dangerously-skip-permissions`
(agents never self-grant a permission bypass) carries forward as an adapter implementation
constraint for #009 — it does not shape the Protocol signature.

**Cross-ticket seams this fixes (no new tickets):**
- **#002** now owns `parse_phase_payload` and the per-phase JSON shapes (moved out of the
  adapter). The driver-side parser is core, not adapter.
- **#005** must consume the `on_event: Callable[[str], None]` sink — that is the seam the
  live feed + `events.jsonl` tee attach to.
- **OpenCode** (deferred) satisfies the same two methods: map its own transport envelope
  to `text`, its own event stream to `on_event`, its own `role → model` config to
  `resolve_model`. No Protocol change — harness-agnosticism proven by construction.

No fog graduates and nothing is ruled out of scope: the boundary shape is settled;
building it against the stub is #008, proving it on claude is #009.
