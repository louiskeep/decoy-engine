"""Conservative memory upper bounds for bounded generation.

A bound here is a figure real data cannot exceed, not an estimate: a column whose value width
cannot be bounded yields `None`, and the caller rejects the plan with `GenerationUnboundedError`
before any provider runs. Sequence and categorical widths are exact from the configuration.
A Faker provider's width is bounded from a closed table of per-type byte caps (the provider data
tables are finite); each produced batch is checked against its cap at runtime, so a value wider
than the declared bound aborts the run instead of silently exceeding what was reserved.

The per-value overhead is the same CPython `str` object cost the memory estimator prices
(`execution._mem_estimate_dtypes`: 8-byte pointer + 49-byte header), so generation and routing
agree on what a string cell costs. The per-stage multipliers (Python list + Arrow buffer + a null
or cast copy) are deliberately high; an under-priced stage would hide the failure this module
exists to prevent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from decoy_engine.generation._faker_pool import POOL_ELIGIBLE_LOCALE_PAIRS

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from faker import Faker

# CPython str object header + list pointer (execution._mem_estimate_dtypes).
STR_OBJECT_OVERHEAD_BYTES = 57
# Python list + Arrow buffer + null/cast copy live together while a batch is converted.
_BATCH_COPIES = 3
# Whole-table legacy generation also holds the finished pa.table plus pandas intermediates.
_LEGACY_COPIES = 4
_POOL_SLACK = 1.5
_POOL_FIXED_BYTES = 80 * 1024
# Dict-of-seen + list slot + str object per distinct parent key.
_KEY_POOL_PER_KEY_EXTRA = 8 + 72

_UTF8_MAX_BYTES_PER_CHAR = 4

# Per-type byte caps for built-in Faker providers called WITHOUT kwargs. Generous on purpose.
_NAME_LIKE = 256
FAKER_VALUE_CAPS: dict[str, int] = {
    "first_name": _NAME_LIKE,
    "last_name": _NAME_LIKE,
    "name": _NAME_LIKE,
    "prefix": 32,
    "suffix": 32,
    "city": _NAME_LIKE,
    "state": _NAME_LIKE,
    "country": _NAME_LIKE,
    "job": _NAME_LIKE,
    "company": _NAME_LIKE,
    "word": _NAME_LIKE,
    "email": 320,
    "safe_email": 320,
    "company_email": 320,
    "user_name": _NAME_LIKE,
    "phone_number": 64,
    "address": 1024,
    "street_address": 512,
    "street_name": _NAME_LIKE,
    "postcode": 32,
    "zipcode": 32,
    "ssn": 32,
    "uuid4": 36,
    "url": 512,
    "ipv4": 16,
    "date": 32,
    "date_of_birth": 32,
    "text": 1024,
    "sentence": 1024,
    "paragraph": 4096,
    "iban": 64,
    "credit_card_number": 32,
    "license_plate": 32,
    "currency_code": 8,
    "color_name": 64,
    "hex_color": 8,
    "pybool": 8,
}
# Locale-certified scalar types (names, places, affixes) share the name-like cap.
for _faker_type, _locale in POOL_ELIGIBLE_LOCALE_PAIRS:
    FAKER_VALUE_CAPS.setdefault(_faker_type, _NAME_LIKE)
del _faker_type, _locale


def _int_width(value: int) -> int:
    return len(str(abs(value))) + (1 if value < 0 else 0)


def faker_payload_bound(
    col: dict[str, Any], provider_snapshot: Mapping[str, Callable[[Faker], Any]] | None = None
) -> int | None:
    """Byte cap for one produced Faker value, or `None` when it cannot be defended."""
    from decoy_engine.internal.faker_setup import has_custom_faker_override

    faker_type = col.get("faker_type", "word")
    if isinstance(faker_type, str) and has_custom_faker_override(faker_type, provider_snapshot):
        return None
    raw_kwargs = col.get("faker_kwargs") or {}
    kwargs = raw_kwargs if isinstance(raw_kwargs, dict) else {}
    if faker_type == "pyint":
        low, high = kwargs.get("min_value", 0), kwargs.get("max_value", 9999)
        step = kwargs.get("step", 1)
        if not all(
            isinstance(v, int) and not isinstance(v, bool) for v in (low, high, step)
        ) or set(kwargs) - {"min_value", "max_value", "step"}:
            return None
        return max(_int_width(low), _int_width(high))
    if kwargs:
        return None
    if faker_type in FAKER_VALUE_CAPS:
        return FAKER_VALUE_CAPS[faker_type]
    # A real provider outside the table has no defensible width. A name Faker does not know
    # falls back to `word` at generation time.
    if (
        isinstance(faker_type, str)
        and not _resolves_to_provider(faker_type)
        and not has_custom_faker_override("word", provider_snapshot)
    ):
        return FAKER_VALUE_CAPS["word"]
    return None


def _resolves_to_provider(faker_type: str) -> bool:
    from decoy_engine.generation.synthesize import _get_default_faker
    from decoy_engine.internal.faker_setup import resolve_pool_provider

    _, exact_name_available, custom = resolve_pool_provider(_get_default_faker(), faker_type)
    return exact_name_available or custom


def sequence_payload_bound(col: dict[str, Any], rows: int) -> int:
    start, step = int(col.get("start", 1)), int(col.get("step", 1))
    pad = int(col.get("pad_length", 0))
    last = start + max(rows - 1, 0) * step
    digits = max(_int_width(start), _int_width(last), pad)
    affixes = len(str(col.get("prefix", ""))) + len(str(col.get("suffix", "")))
    return affixes * _UTF8_MAX_BYTES_PER_CHAR + digits


def categorical_payload_bound(col: dict[str, Any]) -> int | None:
    cats = col.get("categories", ["Category A", "Category B"])
    if not isinstance(cats, list) or not cats:
        return None
    if all(isinstance(c, str) for c in cats):
        return max(len(c.encode("utf-8")) for c in cats)
    if all(isinstance(c, (bool, int, float)) for c in cats):
        return 8
    return None


# Families that run only on the legacy path, with the bound their output can defend, or None.
def _legacy_payload_bound(col: dict[str, Any]) -> int | None:
    kind = col.get("type")
    if kind in ("derived_aggregate", "grouped_series"):
        return 64
    if kind == "windowed_date":
        return 40
    if kind == "group_key":
        length, prefix = col.get("length"), str(col.get("prefix", ""))
        if isinstance(length, int) and not isinstance(length, bool) and length >= 0:
            return len(prefix) * _UTF8_MAX_BYTES_PER_CHAR + length * _UTF8_MAX_BYTES_PER_CHAR
    return None


def compute_payload_bounds(
    config: dict[str, Any],
    provider_snapshot: Mapping[str, Callable[[Faker], Any]] | None = None,
) -> dict[tuple[str, str], int | None]:
    """Per `(table, column)` value-width upper bound in bytes; `None` means unbounded."""
    tables = {t["name"]: t for t in (config.get("tables") or []) if t.get("generate_columns")}
    out: dict[tuple[str, str], int | None] = {}

    def bound(table: str, col: dict[str, Any]) -> int | None:
        key = (table, col["name"])
        if key in out:
            return out[key]
        out[key] = None  # a reference cycle resolves to unbounded rather than recursing
        kind = col.get("type")
        rows = int(tables[table].get("row_count") or 0)
        result: int | None
        if col.get("determinism") == "fresh":
            result = None
        elif kind == "sequence":
            result = sequence_payload_bound(col, rows)
        elif kind == "categorical":
            result = categorical_payload_bound(col)
        elif kind == "faker":
            result = faker_payload_bound(col, provider_snapshot)
        elif kind == "reference":
            parent = tables.get(col.get("reference_table", ""))
            parent_col = next(
                (
                    c
                    for c in (parent or {}).get("generate_columns", [])
                    if c["name"] == col.get("reference_column")
                ),
                None,
            )
            result = (
                None if parent is None or parent_col is None else bound(parent["name"], parent_col)
            )
        else:
            result = _legacy_payload_bound(col)
        out[key] = result
        return result

    for name, table in tables.items():
        for col in table["generate_columns"]:
            bound(name, col)
    return out


def batch_buffer_bytes(payloads: list[int], batch_rows: int) -> int:
    per_row = sum(_BATCH_COPIES * (p + STR_OBJECT_OVERHEAD_BYTES) + 16 for p in payloads)
    return batch_rows * per_row


def sink_buffer_bytes(payloads: list[int], batch_rows: int) -> int:
    """One Parquet row group: the writer holds one batch of encoded column chunks."""
    return batch_rows * sum(p + 16 for p in payloads)


def accumulator_bytes(rows: int, payloads: list[int]) -> int:
    """Whole-table Arrow footprint: data + offsets + validity per value."""
    return rows * sum(p + 12 for p in payloads)


def faker_pool_bytes(pool_size: int, payload: int) -> int:
    return int(pool_size * (STR_OBJECT_OVERHEAD_BYTES + payload) * _POOL_SLACK) + _POOL_FIXED_BYTES


def key_pool_bytes(parent_rows: int, key_payload: int) -> int:
    return parent_rows * (key_payload + STR_OBJECT_OVERHEAD_BYTES + _KEY_POOL_PER_KEY_EXTRA)


def legacy_table_bytes(rows: int, payloads: list[int]) -> int:
    return rows * sum(_LEGACY_COPIES * (p + STR_OBJECT_OVERHEAD_BYTES) + 16 for p in payloads)


__all__ = [
    "FAKER_VALUE_CAPS",
    "STR_OBJECT_OVERHEAD_BYTES",
    "accumulator_bytes",
    "batch_buffer_bytes",
    "categorical_payload_bound",
    "compute_payload_bounds",
    "faker_payload_bound",
    "faker_pool_bytes",
    "key_pool_bytes",
    "legacy_table_bytes",
    "sequence_payload_bound",
    "sink_buffer_bytes",
]
