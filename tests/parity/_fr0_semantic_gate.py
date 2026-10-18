"""Semantic re-freeze gate for the Phase F re-baseline (test-side only).

A re-frozen golden is only as trustworthy as the checks that run before it is accepted. A byte
diff proves the output moved, not that the new output is right, and a marginal histogram alone is
necessary but not sufficient: permuting a result keeps its histogram while breaking an ordering,
and shuffling one composite field keeps every marginal while breaking name/email coherence. This
module checks RELATIONS a broken algorithm cannot satisfy, plus the marginal against the
CONFIGURED distribution (not against an old sample):

- FK membership: every non-null child key is a parent key;
- parent-child cardinality: per-parent counts inside the declared min/max;
- composite coherence: email local part is the first and last name, fields null together;
- sequence rules: exact arithmetic, uniqueness, prefix/suffix/pad;
- sequential-reference order: row i is `parent_keys[i % len]`;
- null propagation: a derived value is null exactly when its input is null;
- marginal: category frequencies within a binomial z-bound of the configured weights;
- pool marginal (FR3): a pooled Faker column is uniform over its pool, so each distinct value
  appears with probability (its multiplicity in the pool) / (pool size), checked as one
  chi-square over the pool's distinct values;
- declared schema: an all-null column or zero-row table keeps its declared (string) type.

Every check raises AssertionError; `tests/parity/test_phase_f_fr0_refreeze.py` runs each against
the real fixtures and against mutants that preserve marginals but break one relation, and each
mutant must fail the gate.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable, Sequence
from typing import Any

import pyarrow as pa

Z_BOUND = 5.0  # five binomial standard deviations: a false failure is ~6e-7 per category


def check_fk_membership(child: pa.Table, child_col: str, parent: pa.Table, parent_col: str) -> None:
    parent_keys = {v for v in parent.column(parent_col).to_pylist() if v is not None}
    orphans = [
        v for v in child.column(child_col).to_pylist() if v is not None and v not in parent_keys
    ]
    assert not orphans, f"{child_col}: {len(orphans)} child key(s) are not in the parent key set"


def check_parent_child_cardinality(
    child: pa.Table, child_col: str, parent: pa.Table, parent_col: str, *, low: int, high: int
) -> None:
    parent_keys = list(
        dict.fromkeys(v for v in parent.column(parent_col).to_pylist() if v is not None)
    )
    counts = Counter(child.column(child_col).to_pylist())
    bad = {k: counts[k] for k in parent_keys if not low <= counts[k] <= high}
    assert not bad, f"{child_col}: per-parent counts outside [{low}, {high}]: {bad}"


def check_sequential_reference(
    child: pa.Table, child_col: str, parent: pa.Table, parent_col: str
) -> None:
    keys = list(dict.fromkeys(v for v in parent.column(parent_col).to_pylist() if v is not None))
    got = child.column(child_col).to_pylist()
    assert got == [keys[i % len(keys)] for i in range(len(got))], (
        f"{child_col}: sequential reference order broke"
    )


def check_sequence(
    values: Sequence[str | None],
    *,
    start: int,
    step: int,
    prefix: str = "",
    suffix: str = "",
    pad: int = 0,
) -> None:
    expected = []
    for i in range(len(values)):
        body = str(start + i * step)
        expected.append(f"{prefix}{body.zfill(pad) if pad > 0 else body}{suffix}")
    assert list(values) == expected, "sequence order/step/format broke"
    if step != 0:
        assert len(set(values)) == len(values), "sequence values are not unique"


def check_composite_coherence(bundle: pa.Table) -> None:
    rows = bundle.to_pylist()
    for i, row in enumerate(rows):
        fields = [row[k] for k in row]
        if all(v is None for v in fields):
            continue
        assert all(v is not None for v in fields), f"row {i}: a composite is partly null"
        local = row["email"].split("@")[0]
        assert local == f"{row['first_name']}.{row['last_name']}".lower(), (
            f"row {i}: email does not match the name fields"
        )


def check_null_propagation(
    derived: Sequence[Any], source: Sequence[Any], *, label: str = "derived"
) -> None:
    for i, (d, s) in enumerate(zip(derived, source, strict=True)):
        assert (d is None) == (s is None), f"{label}: row {i} null state differs from its input"


def check_null_fraction(values: Sequence[Any], probability: float, *, z: float = Z_BOUND) -> None:
    n = len(values)
    observed = sum(v is None for v in values) / n
    bound = z * math.sqrt(probability * (1 - probability) / n) + 1e-9
    assert abs(observed - probability) <= bound, (
        f"null fraction {observed:.4f} is outside {probability} +/- {bound:.4f}"
    )


def check_marginal(
    values: Sequence[Any],
    categories: Sequence[Any],
    weights: Sequence[float] | None = None,
    *,
    z: float = Z_BOUND,
) -> None:
    present = [v for v in values if v is not None]
    n = len(present)
    assert n > 0, "no values to check"
    total = float(sum(weights)) if weights else float(len(categories))
    probs = [(w if weights else 1.0) / total for w in (weights or [1.0] * len(categories))]
    counts = Counter(present)
    assert set(counts) <= set(categories), "a value outside the configured categories appeared"
    for cat, p in zip(categories, probs, strict=True):
        observed = counts[cat] / n
        bound = z * math.sqrt(p * (1 - p) / n) + 1e-9
        assert abs(observed - p) <= bound, (
            f"category {cat!r}: frequency {observed:.4f} is outside {p:.4f} +/- {bound:.4f}"
        )


def check_pool_marginal(values: Sequence[Any], pool: Sequence[Any], *, z: float = Z_BOUND) -> None:
    """Uniform selection over `pool`: chi-square of the observed value counts against the pool's
    multiplicities. Cells expecting fewer than five draws are merged into one so the statistic
    stays valid; the bound is `df + z * sqrt(2 * df)`."""
    present = [v for v in values if v is not None]
    n = len(present)
    assert n > 0, "no values to check"
    multiplicity = Counter(pool)
    counts = Counter(present)
    assert set(counts) <= set(multiplicity), "a value outside the pool appeared"
    stat = 0.0
    cells = 0
    sparse_observed = 0
    sparse_expected = 0.0
    for value, mult in multiplicity.items():
        expected = n * mult / len(pool)
        if expected < 5:
            sparse_observed += counts[value]
            sparse_expected += expected
            continue
        stat += (counts[value] - expected) ** 2 / expected
        cells += 1
    if sparse_expected >= 5:
        stat += (sparse_observed - sparse_expected) ** 2 / sparse_expected
        cells += 1
    df = max(cells - 1, 1)
    bound = df + z * math.sqrt(2 * df)
    assert stat <= bound, f"pool frequency chi-square {stat:.1f} exceeds {bound:.1f} (df={df})"


def check_declared_string_column(table: pa.Table, column: str) -> None:
    assert pa.types.is_string(table.schema.field(column).type), (
        f"{column}: declared string type was lost ({table.schema.field(column).type})"
    )


Check = Callable[[dict[str, pa.Table]], None]
