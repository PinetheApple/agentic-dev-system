"""Shared helper for validating an arbitrary value against a closed set of
string literals at a trust boundary (parsed frontmatter, loaded JSON state).

This intentionally returns `str`, not the narrower `Literal` type: pyright's
generic constraint solver widens `Literal` TypeVar solutions back to their
bound (`str`) rather than preserving them, so a generic version of this
function can't return the precise `Literal` type anyway. Callers instead
`cast()` the validated result to their specific `Literal` alias — the runtime
check here is what makes that cast honest.
"""

from __future__ import annotations


def validate_literal(
    value: object,
    allowed: tuple[str, ...],
    *,
    field: str,
    error: type[Exception] = ValueError,
) -> str:
    if isinstance(value, str) and value in allowed:
        return value
    raise error(f"{field} must be one of {allowed}, got {value!r}")
