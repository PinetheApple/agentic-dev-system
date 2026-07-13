"""Runtime prompt composition (ticket 002): base + expert + spec + design +
task + resume, in memory.

This is the entire boundary between config-on-disk and what an adapter's
run() sees — no templating engine, just ordered concatenation with clear
section headers so the model can tell the layers apart. `spec` and `resume`
are optional (ticket 005): `spec` is baseline context alongside `design`;
`resume` is the Rule-4 read-set, only non-empty when a task is being
re-dispatched over prior work.
"""

from __future__ import annotations


def compose(
    base: str,
    expert_body: str,
    design: str,
    task_body: str,
    *,
    spec: str = "",
    resume: str = "",
) -> str:
    sections = [("Core principles", base)]
    if expert_body.strip():
        sections.append(("Expert instructions", expert_body))
    if spec.strip():
        sections.append(("Spec", spec))
    if design.strip():
        sections.append(("Design", design))
    if task_body.strip():
        sections.append(("Task", task_body))
    if resume.strip():
        sections.append(("Resume", resume))
    return "\n\n".join(f"## {title}\n\n{text.strip()}" for title, text in sections) + "\n"
