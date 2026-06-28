"""Chapter-preserve invariant (6.11) for the test-flight suite.

Split from _invariants.py to keep _invariants.py within the 600-line limit.
The invariant function is re-exported from _invariants.py so external callers
that import from there continue to work unchanged.
"""

from __future__ import annotations

from typing import Any


def check_chapter_preserve(
    job_name: str,
    spec: list[Any],
    result: Any,
    sources: dict[str, Any],
) -> str:
    """Assert code_set columns with chapter_preserve:true keep their chapter.

    For each ChapterPreserveSpec, compares the first character of every source
    code value against the first character of the corresponding output code
    value. A mismatch means the masking strategy returned a code from a
    different chapter, violating the chapter_preserve guarantee.

    Args:
        job_name: Job name for error messages.
        spec: List of ChapterPreserveSpec from the manifest invariants.
        result: ExecutionResult carrying masked output tables.
        sources: dict[table_name, pa.Table] of source frames.

    Returns:
        Short evidence string naming verified columns and row count.

    Raises:
        AssertionError: If any output code is in a different chapter than its
            source code.
    """
    checked: list[str] = []
    for cp in spec:
        src_pa = sources.get(cp.table)
        out_pa = result.outputs.get(cp.table)
        assert src_pa is not None, (
            f"[{job_name}] chapter_preserve: source table '{cp.table}' not found."
        )
        assert out_pa is not None, (
            f"[{job_name}] chapter_preserve: output table '{cp.table}' not in result.outputs."
        )
        src_vals: list[Any] = src_pa.column(cp.column).to_pylist()
        out_vals: list[Any] = out_pa.column(cp.column).to_pylist()
        assert len(src_vals) == len(out_vals), (
            f"[{job_name}] chapter_preserve: {cp.table}.{cp.column}: "
            f"source row count {len(src_vals)} != output row count {len(out_vals)}."
        )
        mismatches = [
            (i, s, o)
            for i, (s, o) in enumerate(zip(src_vals, out_vals, strict=True))
            if isinstance(s, str) and isinstance(o, str) and s[:1] != o[:1]
        ]
        assert not mismatches, (
            f"[{job_name}] chapter_preserve: {cp.table}.{cp.column}: "
            f"{len(mismatches)} chapter mismatch(es) (first 3): "
            + "; ".join(f"row {i} src={s!r} out={o!r}" for i, s, o in mismatches[:3])
            + ". The code_set strategy must preserve the chapter when "
            "chapter_preserve:true is set."
        )
        checked.append(f"{cp.table}.{cp.column}(rows={len(src_vals)})")

    return "chapter_preserve verified: " + ", ".join(checked)
