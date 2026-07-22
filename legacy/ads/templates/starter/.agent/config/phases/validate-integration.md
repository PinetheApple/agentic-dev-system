PHASE:validate-integration

Judge whether the spec below is fully satisfied by the complete merged diff
for this run, given below. This is a cross-task integration check: every
individual task's own exit criteria already passed, so look specifically
for gaps at the seams between tasks — things no single task's own review
would catch. Respond with exactly one JSON object as your entire response:
no prose before or after it, and no markdown code fences — just the raw
JSON object (the ```json fence below is only to illustrate the shape),
shaped as:

```json
{"pass": true | false, "evidence": "<what you saw>", "cited_paths": ["<path>", ...]}
```

A `pass: true` verdict with an empty `cited_paths` list is automatically
treated as a failure. On failure, cite every path where you found a gap so
the driver can attribute it back to the task(s) that own it.

## Spec

(see the Spec section above)

## Diff (full run diff, repo root -> current worktree)

{diff}
