PHASE:reconcile

A worktree merge-back tripwire fired for this task. Fix the worktree in
place so the merge can be retried — do not expand the task's scope beyond
its declared `owns`.

## Violation

{violation}

## Declared owns (hard boundary — do not touch anything outside this)

{owns}

## Uncovered / out-of-bounds files

{uncovered}

## Task branch diff (vs base)

{diff}

## Merge attempt output (only non-empty on a textual conflict)

{merge_output}
