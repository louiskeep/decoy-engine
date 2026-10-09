"""C8-iii-d-2 acceptance: positional categorical / REUSE faker under `when:` run natively,
byte-identical to the d-1 oracle, on the chunked route.

Plan: `docs/plans/2026-10-08-c8-iii-d2-native-positional-when.md` rev 3, section 3. The
parity target is the d-1 oracle: the forced-oracle leg (`run_pair`) is position-correct
per Codex round 1, and `_full_frame` is the whole-frame pandas oracle the d-1 gate keys on.
Every byte-identity case also asserts NATIVE execution (`native_admitted`), so a silent
reroute to the oracle cannot pass an equality-only check.

Covered here: tests 1, 1a (chunked leg), 1b, 2, 3 and 4. The unified full-frame route is in
`tests/physical/test_c8_iii_d2_unified_positional_when.py`.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from decoy_engine import run_mask_chunked, run_mask_pipeline_chunked
from decoy_engine.execution._chunked import check_chunked_compatibility
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.providers_v2 import get_default_registry
from tests.native._b8_support import FORCE, FORCE_REASON, Run, identical, run_pair
from tests.native._chunked_entry_support import (
    ENGINE_VERSION,
    NEEDS_COMPANION,
    TABLE,
    key_provider,
    make_config,
    passthrough,
    split,
)
from tests.native.test_chunked_entry_values_schema import _full_frame

CAT_WHEN_CODE = "chunked_categorical_nondeterministic_when_not_supported"
FAKER_WHEN_CODE = "chunked_faker_nondeterministic_when_not_supported"
CATS = ["alpha", "beta", "gamma", "delta"]
WEIGHTS = [0.55, 0.25, 0.15, 0.05]
_REG = get_default_registry()


# ---------------------------------------------------------------------------
# column + source builders
# ---------------------------------------------------------------------------


def pos_cat(
    name: str = "c", *, weighted: bool = False, namespace: str | None = "ns_c", **extra: Any
) -> dict[str, Any]:
    """A seeded (non-deterministic) positional categorical over a string source."""
    cfg: dict[str, Any] = {"categories": list(CATS)}
    if weighted:
        cfg["weights"] = list(WEIGHTS)
    col: dict[str, Any] = {"name": name, "strategy": "categorical", "provider_config": cfg}
    if namespace is not None:
        col["namespace"] = namespace
    col.update(extra)
    return col


def pos_faker(
    name: str = "c", *, namespace: str | None = "ns_f", pool_size: int | None = 64, **extra: Any
) -> dict[str, Any]:
    """A non-deterministic REUSE faker over a string source."""
    col: dict[str, Any] = {
        "name": name,
        "strategy": "faker",
        "provider": "person_first_name",
        "deterministic": False,
    }
    if pool_size is not None:
        col["pool_size"] = pool_size
    if namespace is not None:
        col["namespace"] = namespace
    col.update(extra)
    return col


POS_BUILDERS = [
    pytest.param(lambda **kw: pos_cat(**kw), False, id="categorical"),
    pytest.param(lambda **kw: pos_cat(weighted=True, **kw), False, id="categorical_weighted"),
    pytest.param(lambda **kw: pos_faker(**kw), True, id="faker"),
]


def src(c_values: list[str | None], keep_values: list[str | None]) -> pa.Table:
    """`c` is the positional source; `keep` is a STRING predicate reference."""
    return pa.table(
        {
            "c": pa.array(c_values, pa.string()),
            "keep": pa.array(keep_values, pa.string()),
        }
    )


def _c_values(n: int) -> list[str | None]:
    # Nulls scattered so the subset carries them; the same source value repeats so the
    # draw can only vary by position.
    return [None if i % 7 == 3 else f"src_{i % 5}" for i in range(n)]


def assert_native_matches_oracle(native: Run, forced: Run, *, faker: bool, nonempty: bool) -> None:
    """Byte-identity to the forced-oracle (position-correct d-1) leg, plus proof the whole table
    ran NATIVELY and, for a nonempty selection, that the positional kernel actually ran. Unlike
    `assert_same_as_oracle` this tolerates a `pandas_read_passthrough` column, because a `when:`
    predicate that reads a passthrough column reads it through pandas on both legs."""
    assert native.ev[0].native_admitted is True, native.ev[0]
    assert native.ev[0].reroute_reason is None
    assert forced.ev[0].native_admitted is False, forced.ev[0]
    assert FORCE_REASON in (forced.ev[0].reroute_reason or "")
    assert len(native.out) == len(forced.out)
    for i, (got, want) in enumerate(zip(native.out, forced.out, strict=True)):
        assert identical(got, want.drop_columns([FORCE])), i
    if nonempty:
        ran = native.ev[0].pool_select_executed if faker else native.ev[0].compiled_kernel_executed
        assert ran is True, native.ev[0]


def assert_values_match_full_frame(got: pa.Table, whole: pa.Table) -> None:
    """Draw-for-draw equality to the WHOLE-FRAME pandas oracle. The chunked route strips the
    `b"pandas"` schema metadata the full-frame route attaches, so this compares values and Arrow
    types per column (metadata-inclusive byte-identity is already proven against the chunked
    oracle leg)."""
    assert got.column_names == whole.column_names
    for name in got.column_names:
        assert got.column(name).to_pylist() == whole.column(name).to_pylist(), name
        assert got.schema.field(name).type == whole.schema.field(name).type, name


# ---------------------------------------------------------------------------
# 1. Native == d-1 oracle, byte-identical, with native-execution evidence.
# ---------------------------------------------------------------------------

# (id, predicate, nonempty): the d-1 predicate matrix.
_MATRIX: list[tuple[str, str, bool]] = [
    ("none_selected", "keep == 'NOPE'", False),
    ("all_selected", "keep != 'NOPE'", True),
    ("every_other", "keep == 'A'", True),
    ("contiguous_block", "keep == 'BLK'", True),
    ("self_reference", "c == 'src_1'", True),
]


def _keep_for(n: int, which: str) -> list[str | None]:
    if which == "every_other":
        return ["A" if i % 2 == 0 else "B" for i in range(n)]
    if which == "contiguous_block":
        return ["BLK" if 10 <= i < 25 else "x" for i in range(n)]
    return ["k"] * n  # none/all/self do not read keep meaningfully


@NEEDS_COMPANION
@pytest.mark.parametrize("build, faker", POS_BUILDERS)
@pytest.mark.parametrize("which, predicate, nonempty", _MATRIX)
def test_1_native_chunked_is_byte_identical_to_the_d1_oracle(
    tmp_path: Path, build: Any, faker: bool, which: str, predicate: str, nonempty: bool
) -> None:
    n = 53
    source = src(_c_values(n), _keep_for(n, which))
    chunks = split(source, 11)  # multiple chunks, uneven tail
    columns = [build(when=predicate), passthrough("keep")]
    native, forced = run_pair(columns, chunks)
    # Byte-identity to the chunked oracle leg (position-correct), plus native evidence.
    assert_native_matches_oracle(native, forced, faker=faker, nonempty=nonempty)
    # Byte-identity to the WHOLE-FRAME pandas oracle the d-1 gate keys on.
    whole = _full_frame(make_config(columns), source, tmp_path)
    got = pa.concat_tables(native.out).combine_chunks() if native.out else pa.table({})
    assert_values_match_full_frame(got, whole)


# ---------------------------------------------------------------------------
# 1a. Reference chunk-stability (Codex round 1 HIGH).
# ---------------------------------------------------------------------------


def _int_ref_source(n: int) -> pa.Table:
    # A nullable int64 sibling whose per-chunk float widening can select different rows.
    big = [9007199254740993, 9007199254740992, None]
    return pa.table(
        {
            "c": pa.array([f"src_{i % 4}" for i in range(n)], pa.string()),
            "p": pa.array([big[i % 3] for i in range(n)], pa.int64()),
        }
    )


@pytest.mark.parametrize("build, code", [(pos_cat, CAT_WHEN_CODE), (pos_faker, FAKER_WHEN_CODE)])
@pytest.mark.parametrize("entry", ["run_mask_chunked", "run_mask_pipeline_chunked"])
def test_1a_numeric_reference_is_rejected_on_both_chunked_entries(
    build: Any, code: str, entry: str
) -> None:
    source = _int_ref_source(6)
    run = run_mask_chunked if entry == "run_mask_chunked" else run_mask_pipeline_chunked
    cfg = make_config([build(when="p == 9007199254740992"), passthrough("p")])
    with pytest.raises(PlanCompileError) as info:
        list(
            run(
                cfg,
                split(source, 3),
                table=TABLE,
                engine_version=ENGINE_VERSION,
                key_provider=key_provider(),
            )
        )
    assert info.value.code == code


@NEEDS_COMPANION
@pytest.mark.parametrize("build, faker", POS_BUILDERS)
def test_1a_string_reference_is_admitted_natively(tmp_path: Path, build: Any, faker: bool) -> None:
    n = 20
    source = src([f"src_{i % 3}" for i in range(n)], ["A" if i % 2 else "B" for i in range(n)])
    columns = [build(when="keep == 'A'"), passthrough("keep")]
    native, forced = run_pair(columns, split(source, 7))
    assert_native_matches_oracle(native, forced, faker=faker, nonempty=True)


# ---------------------------------------------------------------------------
# 1b. Per-chunk type drift (Codex round 2): first chunk string, later chunk numeric/null.
# ---------------------------------------------------------------------------


def _drift_chunks(first_ok: bool = True) -> list[pa.Table]:
    good = pa.table(
        {"c": pa.array(["a", "b"], pa.string()), "keep": pa.array(["A", "A"], pa.string())}
    )
    numeric_target = pa.table(
        {"c": pa.array([1, 2], pa.int64()), "keep": pa.array(["A", "A"], pa.string())}
    )
    return [good, numeric_target]


def _drift_ref_chunks() -> list[pa.Table]:
    good = pa.table(
        {"c": pa.array(["a", "b"], pa.string()), "keep": pa.array(["A", "A"], pa.string())}
    )
    numeric_ref = pa.table(
        {"c": pa.array(["c", "d"], pa.string()), "keep": pa.array([1, 2], pa.int64())}
    )
    return [good, numeric_ref]


def _null_later_chunks() -> list[pa.Table]:
    good = pa.table(
        {"c": pa.array(["a", "b"], pa.string()), "keep": pa.array(["A", "A"], pa.string())}
    )
    null_chunk = pa.table(
        {"c": pa.array([None, None], pa.null()), "keep": pa.array(["A", "A"], pa.string())}
    )
    return [good, null_chunk]


@pytest.mark.parametrize("build, code", [(pos_cat, CAT_WHEN_CODE), (pos_faker, FAKER_WHEN_CODE)])
@pytest.mark.parametrize("entry", ["run_mask_chunked", "run_mask_pipeline_chunked"])
@pytest.mark.parametrize(
    "chunks_fn", [_drift_chunks, _drift_ref_chunks], ids=["target_drift", "ref_drift"]
)
def test_1b_later_chunk_numeric_drift_is_rejected_before_it_runs(
    build: Any, code: str, entry: str, chunks_fn: Any
) -> None:
    run = run_mask_chunked if entry == "run_mask_chunked" else run_mask_pipeline_chunked
    cfg = make_config([build(when="keep == 'A'"), passthrough("keep")])
    produced: list[int] = []
    chunks = chunks_fn()

    def stream() -> Iterator[pa.Table]:
        for ch in chunks:
            yield ch
            produced.append(1)

    with pytest.raises(PlanCompileError) as info:
        for _ in run(
            cfg, stream(), table=TABLE, engine_version=ENGINE_VERSION, key_provider=key_provider()
        ):
            pass
    assert info.value.code == code
    # The drift was refused before the second (offending) chunk's output was yielded.
    assert len(produced) <= 1


@NEEDS_COMPANION
@pytest.mark.parametrize("build, faker", POS_BUILDERS)
def test_1b_later_all_null_chunk_is_accepted(tmp_path: Path, build: Any, faker: bool) -> None:
    columns = [build(when="keep == 'A'"), passthrough("keep")]
    native, forced = run_pair(columns, _null_later_chunks())
    # First chunk selects both rows; the later all-null chunk is accepted and masks nothing new.
    assert_native_matches_oracle(native, forced, faker=faker, nonempty=True)


# ---------------------------------------------------------------------------
# 2. Chunk boundaries and offsets.
# ---------------------------------------------------------------------------


@NEEDS_COMPANION
@pytest.mark.parametrize("build, faker", POS_BUILDERS)
@pytest.mark.parametrize("base_offset", [0, 37])
def test_2_boundaries_and_nonzero_base_offset(
    tmp_path: Path, build: Any, faker: bool, base_offset: int
) -> None:
    n = 41
    # keep selects a block straddling the chunk boundary at 11, and an empty-selection chunk.
    keep = ["A" if 8 <= i < 30 else ("B" if i < 33 else "C") for i in range(n)]
    source = src(_c_values(n), keep)
    columns = [build(when="keep == 'A'"), passthrough("keep")]
    native, forced = run_pair(columns, split(source, 11), base_row_offset=base_offset)
    assert_native_matches_oracle(native, forced, faker=faker, nonempty=True)


@NEEDS_COMPANION
@settings(
    max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(
    n=st.integers(min_value=1, max_value=60),
    chunk=st.integers(min_value=1, max_value=20),
    offset=st.integers(min_value=0, max_value=50),
    data=st.data(),
)
def test_2_property_native_chunked_equals_oracle(
    tmp_path: Path, n: int, chunk: int, offset: int, data: Any
) -> None:
    keep = data.draw(st.lists(st.sampled_from(["A", "B"]), min_size=n, max_size=n))
    source = src(_c_values(n), keep)
    columns = [pos_cat(when="keep == 'A'"), passthrough("keep")]
    native, forced = run_pair(columns, split(source, chunk), base_row_offset=offset)
    # Random keep may select nothing; parity holds either way, native kernel only when nonempty.
    nonempty = any(k == "A" for k in keep)
    assert_native_matches_oracle(native, forced, faker=False, nonempty=nonempty)


# ---------------------------------------------------------------------------
# 3. Nulls in the subset.
# ---------------------------------------------------------------------------


@NEEDS_COMPANION
@pytest.mark.parametrize("build, faker", POS_BUILDERS)
def test_3_null_targets_in_the_selected_subset(tmp_path: Path, build: Any, faker: bool) -> None:
    c = [None if i % 3 == 0 else f"src_{i % 4}" for i in range(24)]
    keep = ["A"] * 24  # all selected, including the null targets
    source = src(c, keep)
    columns = [build(when="keep == 'A'"), passthrough("keep")]
    native, forced = run_pair(columns, split(source, 5))
    assert_native_matches_oracle(native, forced, faker=faker, nonempty=True)
    whole = _full_frame(make_config(columns), source, tmp_path)
    got = pa.concat_tables(native.out).combine_chunks()
    assert_values_match_full_frame(got, whole)


# ---------------------------------------------------------------------------
# 4. Deferred cases keep declining with the same codes.
# ---------------------------------------------------------------------------


def _code(columns: list[dict[str, Any]]) -> str | None:
    try:
        check_chunked_compatibility(make_config(columns), table=TABLE, registry=_REG)
    except PlanCompileError as exc:
        return exc.code
    return None


def test_4_windowed_date_with_when_still_declines_at_config_time() -> None:
    col = {
        "name": "c",
        "strategy": "windowed_date",
        "namespace": "ns_w",
        "provider_config": {"start": "2020-01-01", "end": "2020-12-31"},
        "when": "keep == 'A'",
    }
    assert _code([col, passthrough("keep")]) == "chunked_windowed_date_when_not_supported"


@pytest.mark.parametrize("entry", ["run_mask_chunked", "run_mask_pipeline_chunked"])
def test_4_numeric_source_faker_with_when_declines_via_the_schema_guard(entry: str) -> None:
    # A numeric SOURCE (not reference): the target `c` is int64, so the per-chunk guard rejects.
    source = pa.table(
        {"c": pa.array([1, 2, 3, 4], pa.int64()), "keep": pa.array(["A"] * 4, pa.string())}
    )
    run = run_mask_chunked if entry == "run_mask_chunked" else run_mask_pipeline_chunked
    cfg = make_config([pos_faker(when="keep == 'A'"), passthrough("keep")])
    with pytest.raises(PlanCompileError) as info:
        list(
            run(
                cfg,
                split(source, 2),
                table=TABLE,
                engine_version=ENGINE_VERSION,
                key_provider=key_provider(),
            )
        )
    assert info.value.code == FAKER_WHEN_CODE


def test_4_config_incomplete_positional_categorical_with_when_keeps_its_code() -> None:
    # No namespace => config-incomplete => the non-deterministic code, not the when code.
    assert _code([pos_cat(namespace=None, when="keep == 'A'"), passthrough("keep")]) == (
        "categorical_nondeterministic_not_chunk_safe"
    )


def test_4_config_incomplete_positional_faker_with_when_keeps_its_code() -> None:
    assert _code([pos_faker(pool_size=None, when="keep == 'A'"), passthrough("keep")]) == (
        "chunked_strategy_conditions_unmet"
    )


# ---------------------------------------------------------------------------
# 6. No `when:` is unchanged (the `gate_positions is None` path).
# ---------------------------------------------------------------------------


@NEEDS_COMPANION
@pytest.mark.parametrize("build, faker", POS_BUILDERS)
def test_6_positional_without_when_is_byte_identical(
    tmp_path: Path, build: Any, faker: bool
) -> None:
    # No predicate: run_kernel_step_masked is never reached, so the key stays the contiguous
    # `row_offset + arange(n)` the kernels produced before this change.
    n = 37
    source = src(_c_values(n), ["k"] * n)
    columns = [build(), passthrough("keep")]
    native, forced = run_pair(columns, split(source, 9))
    assert_native_matches_oracle(native, forced, faker=faker, nonempty=True)
    whole = _full_frame(make_config(columns), source, tmp_path)
    got = pa.concat_tables(native.out).combine_chunks()
    assert_values_match_full_frame(got, whole)
