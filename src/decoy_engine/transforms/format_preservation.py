"""Format-preservation post-pass for masking strategies.

Reads two hints off the mask rule:
  - ``format_pattern``  : either a regex shape like ``r'\\d{3}-\\d{3}-\\d{4}'``
                          (digit-with-separator templates) OR a strptime
                          format string like ``'%Y-%m-%d'`` (dates).
                          Sourced from STORM's per-column detection or the
                          user's manual override in the mask card UI.
  - ``casing_pattern``  : one of 'upper' | 'lower' | 'title' | 'digits_only'.
                          STORM-detected or user-override.

Then re-shapes the masked output to match. Two cases:
  - **Digit-template**: extract digits from each masked value and splice the
    template's separators back at the right positions.
  - **strptime**: parse the masked value as a date and ``strftime`` it with
    the template. ``date_shift`` already does this internally; the post-pass
    is a no-op for that strategy by design.

Skips silently when the strategy is structurally incompatible (hash output
is hex; redact output is a fixed string).

Pattern: strptime/strftime format inference (CPython datetime stdlib).
  https://docs.python.org/3/library/datetime.html#strftime-strptime-behavior

Pure functions, no engine state. The MaskingProcessor calls
``apply_format_preservation`` immediately after the strategy returns.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import pandas as pd

# Strategies whose output shape can't be re-formatted without breaking
# the strategy's own semantics. The post-pass is a no-op for these.
#
#   hash    — hex output, splicing dashes / uppercasing destroys reversibility
#   redact  — fixed replacement string, reformatting is meaningless
#   passthrough — output equals input, source format already preserved
#   date_shift  — strategy already handles strftime internally
SKIP_STRATEGIES: frozenset[str] = frozenset({
    "hash", "redact", "passthrough", "date_shift",
})


def apply_format_preservation(
    source: pd.Series,
    masked: pd.Series,
    rule: dict[str, Any],
) -> pd.Series:
    """Re-shape ``masked`` to match the source's surface format.

    Returns ``masked`` unchanged when:
      - ``rule['preserve_format']`` is not truthy.
      - The strategy is in ``SKIP_STRATEGIES``.
      - Neither ``format_pattern`` nor ``casing_pattern`` is present.

    Never raises. A row that can't be re-shaped (unparseable date,
    digit count mismatch) is returned untouched while other rows in the
    same column are re-shaped normally.
    """
    if not rule.get("preserve_format"):
        return masked
    strategy = rule.get("type") or rule.get("strategy")
    if strategy in SKIP_STRATEGIES:
        return masked
    format_pattern = rule.get("format_pattern")
    casing_pattern = rule.get("casing_pattern")
    if not format_pattern and not casing_pattern:
        return masked

    out = masked.copy()

    if format_pattern:
        if _is_strptime(format_pattern):
            out = _apply_strftime(out, format_pattern)
        else:
            out = _apply_digit_template(out, format_pattern)

    if casing_pattern:
        out = _apply_casing(out, casing_pattern)

    # Preserve nulls from the masked output — every transform above
    # operates row-wise + the helpers return the value unchanged on
    # error, so this should already hold; explicit safety net.
    null_mask = masked.isna()
    if null_mask.any():
        out = out.where(~null_mask, masked)

    return out


# ── helpers ──────────────────────────────────────────────────────────


_STRPTIME_HINT = re.compile(r"%[YmdHMSj]")


def _is_strptime(pattern: str) -> bool:
    """A pattern is a strftime / strptime template iff it contains any
    of the common date directives (%Y / %m / %d / %H / %M / %S / %j).
    Otherwise treat as a regex-style digit template."""
    return bool(_STRPTIME_HINT.search(pattern))


def _apply_digit_template(masked: pd.Series, template: str) -> pd.Series:
    """Splice the template's separator characters back into the masked
    output. Template is a regex shape like ``\\d{3}-\\d{3}-\\d{4}``;
    we read the literal characters between ``\\d{N}`` runs as the
    separators and the runs as digit-count slots.

    Example: template ``\\d{3}-\\d{3}-\\d{4}`` + masked ``'2322316595'``
    → ``'232-231-6595'``. If the masked value doesn't have enough
    digits to fill the slots, returns the value unchanged.
    """
    slots = _parse_digit_template(template)
    if not slots:
        return masked

    def reshape(value: Any) -> Any:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return value
        digits = re.sub(r"\D", "", str(value))
        needed = sum(width for _sep, width in slots)
        if len(digits) < needed:
            return value  # not enough digits to fill the template
        parts: list[str] = []
        idx = 0
        for sep, width in slots:
            parts.append(sep)
            parts.append(digits[idx:idx + width])
            idx += width
        return "".join(parts)

    return masked.map(reshape)


_DIGIT_RUN = re.compile(r"\\d\{(\d+)\}")


def _parse_digit_template(template: str) -> list[tuple[str, int]]:
    """Split a template like ``\\d{3}-\\d{3}-\\d{4}`` into
    ``[('', 3), ('-', 3), ('-', 4)]`` — leading separator (possibly
    empty) + width for each slot.

    Returns empty list when the template has no ``\\d{N}`` runs.
    """
    matches = list(_DIGIT_RUN.finditer(template))
    if not matches:
        return []
    slots: list[tuple[str, int]] = []
    cursor = 0
    for m in matches:
        sep = template[cursor:m.start()]
        # Strip regex-escape characters that should pass through as
        # their literal selves (``\.`` → ``.``, ``\(`` → ``(`` etc.).
        sep = re.sub(r"\\(.)", r"\1", sep)
        slots.append((sep, int(m.group(1))))
        cursor = m.end()
    return slots


def _apply_strftime(masked: pd.Series, fmt: str) -> pd.Series:
    """Parse each masked value as a date and re-emit using ``fmt``.

    Rows that don't parse (e.g. a strategy that emitted a non-date
    string) are returned unchanged.
    """
    # Try every plausible source format; the masked output likely came
    # from a clean ISO string but date_shift / faker can emit others.
    _CANDIDATES = (
        "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S", "%m/%d/%Y", "%m/%d/%y",
        "%d/%m/%Y", "%d.%m.%Y", "%d-%m-%Y", "%Y%m%d",
    )

    def reshape(value: Any) -> Any:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return value
        s = str(value)
        # Cheap fast-path: already in target format → strptime succeeds
        # and strftime returns the same string.
        for cand in _CANDIDATES:
            try:
                dt = datetime.strptime(s, cand)
                return dt.strftime(fmt)
            except ValueError:
                continue
        # pandas as last resort — handles native datetime + a few extras.
        try:
            dt = pd.to_datetime(s, errors="raise")
            return dt.strftime(fmt)
        except (ValueError, TypeError):
            return value

    return masked.map(reshape)


def _apply_casing(masked: pd.Series, casing: str) -> pd.Series:
    """Re-apply a casing class to every string in ``masked``.

    Numeric / non-string values pass through unchanged so the post-pass
    doesn't mangle an int / float strategy's output.
    """
    if casing == "upper":
        return masked.map(_safe_upper)
    if casing == "lower":
        return masked.map(_safe_lower)
    if casing == "title":
        return masked.map(_safe_title)
    if casing == "digits_only":
        return masked.map(_safe_digits_only)
    # 'mixed' is the no-op fallback — leave the output alone.
    return masked


def _safe_upper(v: Any) -> Any:
    return v.upper() if isinstance(v, str) else v


def _safe_lower(v: Any) -> Any:
    return v.lower() if isinstance(v, str) else v


def _safe_title(v: Any) -> Any:
    # Title-case but preserve single-letter tokens (middle initials).
    # 'allison s harter' → 'Allison S Harter'. The native str.title()
    # already handles this correctly; explicit guard for clarity.
    return v.title() if isinstance(v, str) else v


def _safe_digits_only(v: Any) -> Any:
    if not isinstance(v, str):
        return v
    return re.sub(r"\D", "", v)
