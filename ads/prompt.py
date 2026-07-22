"""Runtime prompt composition: base + expert + spec + design + task, in
memory. This is the entire boundary between config-on-disk and what an
adapter's `run()` sees — no templating engine, just ordered concatenation
with clear section headers so the model can tell the layers apart.
"""

from __future__ import annotations


def compose(
    base: str,
    expert_body: str,
    design: str,
    task_body: str,
    *,
    spec: str = "",
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
    return "\n\n".join(f"## {title}\n\n{text.strip()}" for title, text in sections) + "\n"
