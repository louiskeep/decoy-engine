"""One fixture per chunk-admitted strategy variant for the B2 output-contract test.

The keys must cover the live admission set (`_chunked._CHUNK_ADMITTED_STRATEGIES
| CHUNK_CONDITIONAL_STRATEGIES`); `test_auto_chunk_output_contract` asserts it,
so a newly admitted strategy fails CI until it has a fixture here. A key is
`<strategy>` or `<strategy>:<variant>`. Conditional and gated strategies get an
explicit variant: faker and categorical on their deterministic value-keyed
paths, code_set and bucket_perturb on their admitted configurations, and
date_shift with an explicit `date_format`.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa

from tests.unit.execution import _auto_chunk_support as support

N = support.ROWS


def _s(fmt: str) -> pa.Array:
    return pa.array([fmt.format(i=i) for i in range(N)])


def _col(name: str, strategy: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "strategy": strategy, **extra}


# key -> (configured columns, source columns). The strategy under test is column "val".
STRATEGY_FIXTURES: dict[str, tuple[list[dict[str, Any]], dict[str, pa.Array]]] = {
    "hash": (
        [_col("val", "hash", namespace="hash_ns")],
        {"val": _s("user{i}@example.com")},
    ),
    "redact": ([_col("val", "redact")], {"val": _s("secret-{i}")}),
    "truncate": (
        [_col("val", "truncate", provider_config={"length": 3})],
        {"val": _s("{i:05d}")},
    ),
    "passthrough": ([_col("val", "passthrough")], {"val": _s("keep-{i}")}),
    "fpe": (
        [_col("val", "fpe", namespace="fpe_ns", provider_config={"charset": "digits"})],
        {"val": _s("{i:09d}")},
    ),
    "text_redact": (
        [_col("val", "text_redact")],
        {"val": _s("contact user{i}@example.com or 123-45-6789 today")},
    ),
    "text_mask": (
        [_col("val", "text_mask", namespace="tm_ns")],
        {"val": _s("contact user{i}@example.com or 123-45-6789 today")},
    ),
    "date_shift:explicit_format": (
        [
            _col(
                "val",
                "date_shift",
                namespace="dob_ns",
                provider_config={"min_days": -30, "max_days": 30, "date_format": "%Y-%m-%d"},
            )
        ],
        {"val": pa.array([f"19{60 + (i % 40):02d}-03-{1 + (i % 28):02d}" for i in range(N)])},
    ),
    "bucketize": (
        [_col("val", "bucketize", provider_config={"width": 50})],
        {"val": pa.array([i * 13 % 997 for i in range(N)])},
    ),
    "top_code": (
        [_col("val", "top_code", provider_config={"preset": "hipaa_age"})],
        {"val": pa.array([20 + i * 2 for i in range(N)])},
    ),
    "windowed_date": (
        [
            _col("start", "passthrough"),
            _col(
                "val",
                "windowed_date",
                provider_config={
                    "anchor": "start",
                    "min_days": 0,
                    "max_days": 30,
                    "distribution": "uniform",
                },
            ),
        ],
        {
            "start": pa.array([f"2020-01-{1 + (i % 28):02d}" for i in range(N)]),
            "val": pa.array([f"2020-02-{1 + (i % 28):02d}" for i in range(N)]),
        },
    ),
    "group_key": (
        [
            _col("g", "passthrough"),
            _col("val", "group_key", provider_config={"group_by": "g"}),
        ],
        {"g": pa.array([f"H-{i % 7}" for i in range(N)]), "val": _s("x{i}")},
    ),
    "faker:deterministic": (
        [
            _col(
                "val",
                "faker",
                provider="person_email",
                deterministic=True,
                namespace="contact_ns",
                cardinality_mode="reuse",
                provider_config={"pool_size": 20},
            )
        ],
        {"val": _s("person{i}@source.example")},
    ),
    "faker:deterministic_native": (
        [
            _col(
                "val",
                "faker",
                provider="person_first_name",
                deterministic=True,
                namespace="first_ns",
                cardinality_mode="reuse",
                provider_config={"pool_size": 20},
            )
        ],
        {"val": _s("name{i}")},
    ),
    "categorical:deterministic": (
        [
            _col(
                "val",
                "categorical",
                deterministic=True,
                namespace="tier_ns",
                provider_config={"categories": ["free", "pro", "team"], "weights": [0.6, 0.3, 0.1]},
            )
        ],
        {"val": pa.array([["bronze", "silver", "gold"][i % 3] for i in range(N)])},
    ),
    "code_set:mask": (
        [_col("val", "code_set", provider_config={"code_set": "icd10"})],
        {"val": pa.array([["A00", "B20", "C34", "E11"][i % 4] for i in range(N)])},
    ),
    "bucket_perturb:explicit_format": (
        [
            _col(
                "val",
                "bucket_perturb",
                namespace="bp",
                provider_config={"bucket": "month", "date_format": "%Y-%m-%d"},
            )
        ],
        {"val": pa.array([f"2021-{1 + (i % 12):02d}-15" for i in range(N)])},
    ),
}


# Strategy keys whose columns B1 runs natively when the companion is present, and
# the subset that needs the companion's kernels to do so.
NATIVE_KEYS = frozenset(
    {
        "hash",
        "redact",
        "truncate",
        "passthrough",
        "faker:deterministic_native",
        "categorical:deterministic",
    }
)
NEEDS_COMPANION_KEYS = frozenset(
    {"hash", "faker:deterministic_native", "categorical:deterministic"}
)
# Output type rule: hash, truncate and redact always yield `string` on the dispatcher lane.
STRING_OUTPUT_KEYS = frozenset({"hash", "redact", "truncate"})
# Planned backend of the column under test.
PLANNED_BACKEND: dict[str, str] = {
    "hash": "rust_companion",
    "redact": "arrow_python",
    "truncate": "arrow_python",
    "passthrough": "arrow_python",
    "faker:deterministic_native": "rust_pool_select",
    "categorical:deterministic": "rust_companion",
    # Planned for Rust (the strategy and config qualify) but refused at admission
    # because the provider has no native pool: planned `rust_pool_select`, executed on pandas.
    "faker:deterministic": "rust_pool_select",
}
# B1's refusal reason (prefix) for the keys the native route does not run, as observed on main.
REFUSAL: dict[str, str] = {
    "fpe": "fallback_policy_not_native:val",
    "text_redact": "fallback_policy_not_native:val",
    "text_mask": "fallback_policy_not_native:val",
    "bucketize": "fallback_policy_not_native:val",
    "top_code": "fallback_policy_not_native:val",
    "windowed_date": "fallback_policy_not_native:val",
    "code_set:mask": "fallback_policy_not_native:val",
    "date_shift:explicit_format": "date_shift_not_native_chunked_route:val",
    "group_key": "group_key_not_native_chunked_route:val",
    "faker:deterministic": "faker_provider_not_native:val",
    "bucket_perturb:explicit_format": "bucket_perturb_not_native_chunked_route:val",
}
