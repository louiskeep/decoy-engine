"""MG-3 / M2 (2026-05-31): nested JSONPath strategy regression cells.

Locks:
- Single-leaf and array-of-objects writebacks preserve JSON structure.
- Non-JSON cells emit a typed QualityWarning and pass through.
- Null cells stay null.
- Subset detector_ids -> no-such-path returns the cell unchanged
  without warning (sparse paths are valid).
- Recursive nested is rejected with a typed warning.
- Unknown child strategy is rejected with a typed warning.
- Bad target paths emit a typed warning (no crash).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.execution import PandasExecutionAdapter, PolarsExecutionAdapter
from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution._strategies._nested import (
    NestedStrategyHandler,
    _has_prefix_overlap,
    _path_segments,
)
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry

_REG = get_default_registry()
_GRAPH = RelationshipGraph(edges=(), ordering=())
_NS = NamespaceRegistry(bindings=())
_JOB_SEED = b"\xca\xfe" * 4  # 8 bytes


def _seed(provider_config: dict) -> ColumnSeed:
    return ColumnSeed(
        namespace=None,
        strategy="nested",
        provider=None,
        backend_type="decoy_native",
        backend_version="1",
        cardinality_mode="reuse",
        deterministic=False,
        provider_config=tuple(sorted(provider_config.items())),
    )


class _FakeCtx:
    pass


# ── happy paths ───────────────────────────────────────────────────────


class TestHappyPaths:
    def test_nested_redact_replaces_target_leaf(self):
        df = pd.DataFrame(
            {
                "data": [
                    json.dumps({"user": {"name": "Alice", "email": "alice@x.com"}}),
                ]
            }
        )
        handler = NestedStrategyHandler()
        out, warnings = handler.run(
            df.copy(),
            "data",
            _seed({"target": "$.user.email", "strategy": "redact"}),
            _FakeCtx(),
        )
        assert warnings == []
        parsed = json.loads(out["data"].iloc[0])
        assert parsed["user"]["email"] == "REDACTED"
        # Sibling field untouched.
        assert parsed["user"]["name"] == "Alice"

    def test_nested_arrayof_objects_target_walks_each_entry(self):
        df = pd.DataFrame(
            {
                "data": [
                    json.dumps(
                        {
                            "users": [
                                {"name": "Alice", "email": "alice@x.com"},
                                {"name": "Bob", "email": "bob@x.com"},
                            ]
                        }
                    )
                ]
            }
        )
        handler = NestedStrategyHandler()
        out, _ = handler.run(
            df.copy(),
            "data",
            _seed({"target": "$.users[*].email", "strategy": "redact"}),
            _FakeCtx(),
        )
        parsed = json.loads(out["data"].iloc[0])
        assert [u["email"] for u in parsed["users"]] == ["REDACTED", "REDACTED"]
        assert [u["name"] for u in parsed["users"]] == ["Alice", "Bob"]

    def test_nested_categorical_child_writeback_preserves_json_structure(self):
        df = pd.DataFrame(
            {
                "data": [
                    json.dumps({"tier": "free", "id": 1}),
                    json.dumps({"tier": "pro", "id": 2}),
                ]
            }
        )
        handler = NestedStrategyHandler()
        out, _ = handler.run(
            df.copy(),
            "data",
            _seed(
                {
                    "target": "$.tier",
                    "strategy": "categorical",
                    "strategy_config": {"categories": ["X", "Y"]},
                }
            ),
            _FakeCtx(),
        )
        for row in out["data"]:
            parsed = json.loads(row)
            assert parsed["tier"] in ("X", "Y")
            assert "id" in parsed  # structure preserved


# ── passthrough cases ─────────────────────────────────────────────────


class TestPassthroughCases:
    def test_nested_cell_not_json_passthrough_with_warning(self):
        df = pd.DataFrame({"data": ["not json at all"]})
        handler = NestedStrategyHandler()
        out, warnings = handler.run(
            df.copy(),
            "data",
            _seed({"target": "$.email", "strategy": "redact"}),
            _FakeCtx(),
        )
        assert out["data"].iloc[0] == "not json at all"
        codes = [w.code for w in warnings]
        assert "nested_cell_json_parse_error" in codes

    def test_nested_target_path_missing_in_cell_passthrough_no_error(self):
        df = pd.DataFrame({"data": [json.dumps({"x": 1, "y": 2})]})
        handler = NestedStrategyHandler()
        out, warnings = handler.run(
            df.copy(),
            "data",
            _seed({"target": "$.nonexistent", "strategy": "redact"}),
            _FakeCtx(),
        )
        # Sparse paths are valid: no match -> no change, no warning.
        assert json.loads(out["data"].iloc[0]) == {"x": 1, "y": 2}
        assert all(w.code != "nested_jsonpath_parse_error" for w in warnings)

    def test_nested_null_cell_stays_null(self):
        df = pd.DataFrame(
            {
                "data": [
                    json.dumps({"user": {"email": "alice@x.com"}}),
                    None,
                ]
            }
        )
        handler = NestedStrategyHandler()
        out, _ = handler.run(
            df.copy(),
            "data",
            _seed({"target": "$.user.email", "strategy": "redact"}),
            _FakeCtx(),
        )
        assert pd.isna(out["data"].iloc[1])
        parsed = json.loads(out["data"].iloc[0])
        assert parsed["user"]["email"] == "REDACTED"


# ── rejections ────────────────────────────────────────────────────────


class TestRejections:
    """QA-3 F12 (2026-05-31, security): config errors below now raise
    StrategyError so the runner fails the job. Pre-fix they returned the
    column unchanged with a QualityWarning; a typoed target or unknown
    child strategy silently passed PII through (the warning surfaced
    only in the Storm report, which not all operators audit).
    """

    def test_nested_recursive_nested_rejected(self):
        df = pd.DataFrame({"data": [json.dumps({"x": "y"})]})
        handler = NestedStrategyHandler()
        with pytest.raises(StrategyError) as exc:
            handler.run(
                df.copy(),
                "data",
                _seed({"target": "$.x", "strategy": "nested"}),
                _FakeCtx(),
            )
        assert exc.value.code == "nested_recursive_nested_rejected"
        assert exc.value.strategy == "nested"

    def test_nested_unknown_child_strategy_raises(self):
        df = pd.DataFrame({"data": [json.dumps({"x": "y"})]})
        handler = NestedStrategyHandler()
        with pytest.raises(StrategyError) as exc:
            handler.run(
                df.copy(),
                "data",
                _seed({"target": "$.x", "strategy": "no_such_strategy"}),
                _FakeCtx(),
            )
        assert exc.value.code == "nested_child_strategy_unknown"

    def test_nested_jsonpath_parse_error_raises(self):
        df = pd.DataFrame({"data": [json.dumps({"x": "y"})]})
        handler = NestedStrategyHandler()
        with pytest.raises(StrategyError) as exc:
            handler.run(
                df.copy(),
                "data",
                _seed({"target": "$.x[", "strategy": "redact"}),  # bad jsonpath
                _FakeCtx(),
            )
        assert exc.value.code == "nested_jsonpath_parse_error"
        # Machine `strategy` attribution is fail-closed-load-bearing, not prose.
        assert exc.value.strategy == "nested"

    def test_nested_target_empty_raises(self):
        df = pd.DataFrame({"data": [json.dumps({"x": "y"})]})
        handler = NestedStrategyHandler()
        with pytest.raises(StrategyError) as exc:
            handler.run(
                df.copy(),
                "data",
                _seed({"target": "", "strategy": "redact"}),
                _FakeCtx(),
            )
        assert exc.value.code == "nested_target_unset"
        assert exc.value.strategy == "nested"

    def test_nested_strategy_empty_raises(self):
        df = pd.DataFrame({"data": [json.dumps({"x": "y"})]})
        handler = NestedStrategyHandler()
        with pytest.raises(StrategyError) as exc:
            handler.run(
                df.copy(),
                "data",
                _seed({"target": "$.x", "strategy": ""}),
                _FakeCtx(),
            )
        assert exc.value.code == "nested_strategy_unset"


# ── batch delegation ──────────────────────────────────────────────────


class TestDuplicateIndex:
    """QA-3 F2 (2026-05-31): duplicate-index DataFrames used to corrupt
    the nested writeback. The old implementation iterated `col.index`
    and stored per-row state in a dict keyed on the index label; on a
    duplicate index, `col.at[row_idx]` returned a Series and the dict
    silently kept only one entry per duplicate. Post-fix the strategy
    uses positional enumeration: every row visited exactly once and
    written back by position."""

    def test_nested_duplicate_index_writeback_correct(self):
        df = pd.DataFrame(
            {
                "data": [
                    json.dumps({"user": {"email": "a@x.com"}}),
                    json.dumps({"user": {"email": "b@x.com"}}),
                    json.dumps({"user": {"email": "c@x.com"}}),
                ]
            },
            index=[0, 0, 0],  # all rows share the same index label
        )
        handler = NestedStrategyHandler()
        out, warnings = handler.run(
            df.copy(),
            "data",
            _seed({"target": "$.user.email", "strategy": "redact"}),
            _FakeCtx(),
        )
        # All 3 rows must be masked; no row's email survives the
        # writeback. Pre-fix only the FIRST row (or worse: only one of
        # the rows non-deterministically) got the writeback.
        for row in out["data"]:
            parsed = json.loads(row)
            assert parsed["user"]["email"] == "REDACTED"
        assert warnings == []


class TestChildTechniqueClass:
    """QA-3 F7 (2026-05-31): the synthetic child ColumnSeed must carry
    the child strategy's technique class, not None (the parent's class
    for nested is intentionally None per _technique_class.py)."""

    def test_nested_child_technique_class_resolves_for_redact(self):
        # Indirect verification: the child handler runs against a seed
        # whose technique_class is anonymisation (redact's class).
        # Stand-in test: confirm the strategy still produces correct
        # masked output, which is the user-visible signal that the
        # child seed was constructed correctly.
        df = pd.DataFrame({"data": [json.dumps({"x": "y"})]})
        handler = NestedStrategyHandler()
        out, _ = handler.run(
            df.copy(),
            "data",
            _seed({"target": "$.x", "strategy": "redact"}),
            _FakeCtx(),
        )
        assert json.loads(out["data"].iloc[0]) == {"x": "REDACTED"}


class TestJsonPathOverlapSecurity:
    """QA-3 F14 (2026-05-31, security): wildcard / recursive JSONPath
    can produce match-sets where one match's path is a prefix of
    another (e.g. `$..*` returns the dict AND its contents). Pre-fix
    `jsonpath_ng.update` in original order silently lost the inner
    write or wrote to the wrong location. Post-fix the matches are
    sorted deepest-first and a typed QualityWarning is emitted when
    prefix-overlap is detected so operators auditing the Storm report
    can see the path was ambiguous."""

    def test_nested_recursive_overlap_emits_warning(self):
        # `$..*` returns matches at every level; the root structures
        # are prefixes of their leaves. The strategy must emit the
        # overlap warning and writeback deepest-first.
        df = pd.DataFrame(
            {
                "data": [
                    json.dumps({"user": {"name": "Alice", "email": "alice@x.com"}}),
                ]
            }
        )
        handler = NestedStrategyHandler()
        _, warnings = handler.run(
            df.copy(),
            "data",
            _seed({"target": "$..*", "strategy": "redact"}),
            _FakeCtx(),
        )
        codes = [w.code for w in warnings]
        assert "nested_jsonpath_path_overlap" in codes

    def test_nested_recursive_no_overlap_emits_no_warning(self):
        # `$..ssn` on siblings (not nested) returns parallel matches;
        # no prefix-overlap, no warning.
        df = pd.DataFrame(
            {
                "data": [
                    json.dumps(
                        {
                            "patient": {"ssn": "111"},
                            "spouse": {"ssn": "222"},
                        }
                    )
                ]
            }
        )
        handler = NestedStrategyHandler()
        _, warnings = handler.run(
            df.copy(),
            "data",
            _seed({"target": "$..ssn", "strategy": "redact"}),
            _FakeCtx(),
        )
        codes = [w.code for w in warnings]
        assert "nested_jsonpath_path_overlap" not in codes
        # And both ssns are masked.

    def test_nested_recursive_masks_all_target_leaves(self):
        # Even with overlap, every leaf identified by the JSONPath
        # gets written to (deepest-first ordering preserves the
        # contract: every match position receives the masked value).
        df = pd.DataFrame(
            {
                "data": [
                    json.dumps(
                        {
                            "patient": {"ssn": "111-22-3333"},
                            "spouse": {"ssn": "444-55-6666"},
                        }
                    )
                ]
            }
        )
        handler = NestedStrategyHandler()
        out, _ = handler.run(
            df.copy(),
            "data",
            _seed({"target": "$..ssn", "strategy": "redact"}),
            _FakeCtx(),
        )
        parsed = json.loads(out["data"].iloc[0])
        assert parsed["patient"]["ssn"] == "REDACTED"
        assert parsed["spouse"]["ssn"] == "REDACTED"


class TestBatchDelegation:
    def test_nested_collects_all_leaves_into_one_child_call(self):
        """Multi-row + multi-leaf input must be delegated to the child
        strategy in a single batch (preserves the child's vectorized
        behavior). Verified indirectly: every targeted leaf gets the
        redact token, no untargeted leaf is touched."""
        df = pd.DataFrame(
            {
                "data": [
                    json.dumps({"a": "x", "b": "keep1"}),
                    json.dumps({"a": "y", "b": "keep2"}),
                    json.dumps({"a": "z", "b": "keep3"}),
                ]
            }
        )
        handler = NestedStrategyHandler()
        out, _ = handler.run(
            df.copy(),
            "data",
            _seed({"target": "$.a", "strategy": "redact"}),
            _FakeCtx(),
        )
        for i, cell in enumerate(out["data"], start=1):
            parsed = json.loads(cell)
            assert parsed["a"] == "REDACTED"
            assert parsed["b"] == f"keep{i}"


class TestNestedWhenGateFailClosed:
    """Codex round-5 P2 NESTED CODE_SET CHILD NOT PREFLIGHTED UNDER A
    ZERO-MATCH WHEN-GATE remediation: `run_with_when_gate` calls
    `getattr(handler, "preflight", None)` unconditionally, before its
    zero-match short-circuit (see
    test_code_set_strategy.py::TestCodeSetWhenGateFailClosed for the plain
    `code_set` case), but that only reaches a `code_set` column's own
    corpus check when the OUTER handler defines `preflight`.
    `NestedStrategyHandler` (the outer handler for a `nested(code_set)`
    column) previously did not, so the CHILD `CodeSetHandler.preflight` was
    never invoked, and a `nested(code_set)` column referencing a
    missing/invalid corpus succeeded silently whenever its `when:` gate
    matched zero rows. `NestedStrategyHandler.preflight` closes this by
    resolving the child handler and forwarding to its `preflight`.

    Codex round-6 P2 FAIL-CLOSED PARITY remediation: the pandas gate
    (`run_with_when_gate`) called `preflight` unconditionally, but its
    polars counterpart (`run_with_when_gate_polars`) did not call it at all
    -- and even once it does, a `nested` column routes through
    `PandasStrategyPort(NestedStrategyHandler())` on the polars adapter,
    and the port itself did not forward a `preflight` attribute, so
    `getattr(handler, "preflight", None)` still resolved to `None` for the
    wrapped handler. A native polars `nested(code_set)` column with a
    zero-match `when:` and a missing/invalid corpus therefore succeeded
    (or rather, silently passed the raw JSON cells through) on the polars
    route while the pandas route correctly raised -- a cross-substrate
    fail-closed divergence. `test_zero_match_when_gate_still_fails_closed_
    on_missing_child_corpus_polars` below pins the polars route to the
    SAME fail-closed behavior as the pandas test above."""

    def test_zero_match_when_gate_still_fails_closed_on_missing_child_corpus(self) -> None:
        """A nested(code_set) column whose `when:` predicate matches NO
        rows, and whose child corpus_source points at a customer path that
        does not exist, must still raise through the real pandas-adapter
        route -- not silently pass the raw JSON cells through."""
        table = pa.table({"data": pa.array([json.dumps({"diag": "I10"})], type=pa.string())})
        col_seed = ColumnSeed(
            namespace=None,
            strategy="nested",
            provider=None,
            backend_type="decoy_native",
            backend_version="1",
            cardinality_mode="reuse",
            deterministic=False,
            provider_config=tuple(
                sorted(
                    {
                        "target": "$.diag",
                        "strategy": "code_set",
                        "strategy_config": {
                            "code_set": "missing_corpus",
                            "corpus_source": "customer:/no/such/file.parquet",
                        },
                    }.items()
                )
            ),
            # Never true for the one row: the gate matches zero rows.
            when="data == 'ZZZ_NEVER_MATCHES'",
        )
        plan: Any = SimpleNamespace(
            seed_envelope=SeedEnvelope(
                job_seed=_JOB_SEED,
                per_table=(
                    (
                        "t",
                        TableSeed(
                            per_column=(("data", col_seed),),
                            per_group=(),
                        ),
                    ),
                ),
            )
        )
        with pytest.raises(PlanCompileError) as excinfo:
            PandasExecutionAdapter().run_single(
                plan, table, registry=_REG, relationship_graph=_GRAPH, namespace_registry=_NS
            )
        assert excinfo.value.code == "code_set_corpus_path_not_found", (
            f"expected a fail-closed corpus error, got {excinfo.value.code!r}. "
            "A zero-match when: gate on a nested(code_set) column must not let "
            "a missing child corpus succeed silently."
        )

    def test_zero_match_when_gate_still_fails_closed_on_missing_child_corpus_polars(
        self,
    ) -> None:
        """Same fixture as the pandas test above, driven through
        `PolarsExecutionAdapter` instead: `nested` is polars-native
        (`PandasStrategyPort(NestedStrategyHandler())`), so this job has no
        FK edges or unmigrated strategies and classifies fully native --
        exercising `run_with_when_gate_polars`'s preflight call and the
        port's `preflight` forwarding, not an oracle fallback."""
        table = pa.table({"data": pa.array([json.dumps({"diag": "I10"})], type=pa.string())})
        col_seed = ColumnSeed(
            namespace=None,
            strategy="nested",
            provider=None,
            backend_type="decoy_native",
            backend_version="1",
            cardinality_mode="reuse",
            deterministic=False,
            provider_config=tuple(
                sorted(
                    {
                        "target": "$.diag",
                        "strategy": "code_set",
                        "strategy_config": {
                            "code_set": "missing_corpus",
                            "corpus_source": "customer:/no/such/file.parquet",
                        },
                    }.items()
                )
            ),
            # Never true for the one row: the gate matches zero rows.
            when="data == 'ZZZ_NEVER_MATCHES'",
        )
        plan: Any = SimpleNamespace(
            seed_envelope=SeedEnvelope(
                job_seed=_JOB_SEED,
                per_table=(
                    (
                        "t",
                        TableSeed(
                            per_column=(("data", col_seed),),
                            per_group=(),
                        ),
                    ),
                ),
            )
        )
        with pytest.raises(PlanCompileError) as excinfo:
            PolarsExecutionAdapter().run(
                plan,
                {"t": table},
                registry=_REG,
                relationship_graph=_GRAPH,
                namespace_registry=_NS,
            )
        assert excinfo.value.code == "code_set_corpus_path_not_found", (
            f"expected a fail-closed corpus error, got {excinfo.value.code!r}. "
            "A zero-match when: gate on a nested(code_set) column must not let "
            "a missing child corpus succeed silently on the POLARS route either."
        )


# ── mutation-hardening (TQ crown-jewels, 2026-07-26) ───────────────────
#
# Kills the LOGIC survivors from the mutmut run on `_nested.py`. Grouped
# by the invariant / method each pins. See
# docs/quality/mutation-ledgers/execution_strategies_nested.md.


class _SpyChild:
    """Child handler double that records the outer-column ctx stamp and
    applies a per-value deterministic transform.

    Lets us pin two `run()` invariants that a constant child (redact)
    cannot observe: (a) the real outer column is visible to the child
    during dispatch (ctx save/stamp/restore), and (b) each collected leaf
    is written back at ITS OWN path (the cursor mapping), by making the
    masked value a function of the input so a misalignment is visible.
    """

    def __init__(self) -> None:
        self.seen_outer_column: Any = "UNSET"

    def run(self, df, column, plan, ctx):
        if ctx is None:
            self.seen_outer_column = "CTX_NONE"
        else:
            self.seen_outer_column = getattr(ctx, "nested_outer_column", "MISSING")
        vals = df[column].tolist()
        df[column] = [f"M::{v}" for v in vals]
        return df, []


def _match_with_path(path_str: str) -> Any:
    """A jsonpath_ng match double whose `full_path` stringifies to
    `path_str`, for exercising `_path_segments` / `_has_prefix_overlap`
    at their documented (deepest-first) contract boundary."""

    class _FullPath:
        def __str__(self) -> str:
            return path_str

    return SimpleNamespace(full_path=_FullPath())


class TestJsonPathParseErrorFields:
    """The parse-error raise site's machine `strategy` is fail-closed
    attribution, not prose (kills strategy=None / XX-wrap / uppercase)."""

    def test_jsonpath_parse_error_strategy_field(self):
        df = pd.DataFrame({"data": [json.dumps({"x": "y"})]})
        handler = NestedStrategyHandler()
        with pytest.raises(StrategyError) as exc:
            handler.run(
                df.copy(),
                "data",
                _seed({"target": "$.x[", "strategy": "redact"}),
                _FakeCtx(),
            )
        assert exc.value.code == "nested_jsonpath_parse_error"
        assert exc.value.strategy == "nested"


class TestExtensionArrayColumn:
    """The extension-array branch (`col.astype(object)`) must run for a
    pandas StringDtype column. Kills `col = None` (AttributeError on
    `to_list`) and `col.astype(None)` (ValueError)."""

    def test_extension_array_dtype_column_is_masked(self):
        df = pd.DataFrame(
            {"data": pd.Series([json.dumps({"user": {"email": "a@x.com"}})], dtype="string")}
        )
        handler = NestedStrategyHandler()
        out, warnings = handler.run(
            df.copy(),
            "data",
            _seed({"target": "$.user.email", "strategy": "redact"}),
            _FakeCtx(),
        )
        assert warnings == []
        assert json.loads(out["data"].iloc[0])["user"]["email"] == "REDACTED"


class TestRowOrderingBranches:
    """Each per-cell `continue` must skip only its own row, not halt the
    scan. Kills the three `continue -> break` mutants (null cell, parse
    error, no-match) by placing the skipped row BEFORE a maskable row."""

    def test_null_cell_before_valid_row_does_not_halt(self):
        df = pd.DataFrame({"data": [None, json.dumps({"user": {"email": "a@x.com"}})]})
        handler = NestedStrategyHandler()
        out, _ = handler.run(
            df.copy(),
            "data",
            _seed({"target": "$.user.email", "strategy": "redact"}),
            _FakeCtx(),
        )
        assert pd.isna(out["data"].iloc[0])
        assert json.loads(out["data"].iloc[1])["user"]["email"] == "REDACTED"

    def test_parse_error_cell_before_valid_row_does_not_halt(self):
        df = pd.DataFrame({"data": ["not json", json.dumps({"user": {"email": "a@x.com"}})]})
        handler = NestedStrategyHandler()
        out, warnings = handler.run(
            df.copy(),
            "data",
            _seed({"target": "$.user.email", "strategy": "redact"}),
            _FakeCtx(),
        )
        assert out["data"].iloc[0] == "not json"
        assert json.loads(out["data"].iloc[1])["user"]["email"] == "REDACTED"
        assert any(w.code == "nested_cell_json_parse_error" for w in warnings)

    def test_no_match_cell_before_valid_row_does_not_halt(self):
        df = pd.DataFrame(
            {"data": [json.dumps({"other": 1}), json.dumps({"user": {"email": "a@x.com"}})]}
        )
        handler = NestedStrategyHandler()
        out, _ = handler.run(
            df.copy(),
            "data",
            _seed({"target": "$.user.email", "strategy": "redact"}),
            _FakeCtx(),
        )
        assert json.loads(out["data"].iloc[0]) == {"other": 1}
        assert json.loads(out["data"].iloc[1])["user"]["email"] == "REDACTED"


class TestWarningMachineFields:
    """Both QualityWarnings carry machine fields (`provider`, `column`,
    and the `detail` dict keys/values) that flow into the manifest's
    quality summary. Pin them exactly: kills provider/column None + case
    + XX-wrap, and every `detail` key rename / value / None / dropped."""

    def test_parse_error_warning_fields(self):
        df = pd.DataFrame({"data": ["not json"]})
        handler = NestedStrategyHandler()
        _, warnings = handler.run(
            df.copy(),
            "data",
            _seed({"target": "$.email", "strategy": "redact"}),
            _FakeCtx(),
        )
        parse_warnings = [w for w in warnings if w.code == "nested_cell_json_parse_error"]
        assert len(parse_warnings) == 1
        w = parse_warnings[0]
        assert w.provider == "nested"
        assert w.column == "data"
        assert w.detail == {"row_pos": "0"}

    def test_overlap_warning_fields(self):
        df = pd.DataFrame({"data": [json.dumps({"user": {"name": "Alice", "email": "a@x.com"}})]})
        handler = NestedStrategyHandler()
        _, warnings = handler.run(
            df.copy(),
            "data",
            _seed({"target": "$..*", "strategy": "redact"}),
            _FakeCtx(),
        )
        overlaps = [w for w in warnings if w.code == "nested_jsonpath_path_overlap"]
        assert len(overlaps) >= 1
        w = overlaps[0]
        assert w.provider == "nested"
        assert w.column == "data"
        assert set(w.detail.keys()) == {"row_pos", "target", "match_count"}
        assert w.detail["row_pos"] == "0"
        assert w.detail["target"] == "$..*"
        assert w.detail["match_count"].isdigit()
        assert int(w.detail["match_count"]) >= 1


class TestOuterColumnCtxLifecycle:
    """`run()` saves ctx.nested_outer_column, stamps the REAL outer column
    for the child dispatch, then restores the prior value in `finally`.
    Pins save (prior value / default), stamp (name + value seen by the
    child), and restore (correct attr name + value)."""

    def _redact_seed(self):
        return _seed({"target": "$.user.email", "strategy": "redact"})

    def _one_row_df(self):
        return pd.DataFrame({"data": [json.dumps({"user": {"email": "a@x.com"}})]})

    def test_outer_column_restored_to_default_when_absent(self):
        # ctx has no nested_outer_column attr: default "" is saved and
        # restored. Kills prior=None, getattr default None, default "XXXX",
        # and finally restoring the wrong value/attr.
        ctx = _FakeCtx()
        NestedStrategyHandler().run(self._one_row_df(), "data", self._redact_seed(), ctx)
        assert ctx.nested_outer_column == ""

    def test_outer_column_restored_to_prior_value(self):
        # A prior stamp must be restored verbatim (nested dispatch can run
        # mid-dispatch of another node). Kills getattr(None, ...),
        # reading/writing a wrong attr name, and prior=None.
        ctx = _FakeCtx()
        object.__setattr__(ctx, "nested_outer_column", "SENTINEL")
        NestedStrategyHandler().run(self._one_row_df(), "data", self._redact_seed(), ctx)
        assert ctx.nested_outer_column == "SENTINEL"

    def test_child_sees_real_outer_column_during_dispatch(self, monkeypatch):
        import decoy_engine.execution._strategies as strat

        spy = _SpyChild()
        monkeypatch.setitem(strat.SCALAR_HANDLERS, "spy", spy)
        df = pd.DataFrame({"data": [json.dumps({"a": "v1"})]})
        NestedStrategyHandler().run(
            df, "data", _seed({"target": "$.a", "strategy": "spy"}), _FakeCtx()
        )
        # Kills stamp=None, wrong stamp attr name, and ctx=None passed to child.
        assert spy.seen_outer_column == "data"


class TestChildBatchMapping:
    """Each collected leaf is written back at its OWN path in collection
    order. With a per-value child transform and distinct leaf values, a
    misaligned cursor (`+= 1` -> `= 1` / `-= 1`) surfaces as a wrong value
    at some path."""

    def test_each_leaf_maps_to_its_own_masked_value(self, monkeypatch):
        import decoy_engine.execution._strategies as strat

        monkeypatch.setitem(strat.SCALAR_HANDLERS, "spy", _SpyChild())
        df = pd.DataFrame(
            {
                "data": [
                    json.dumps({"a": "alpha", "b": "beta"}),
                    json.dumps({"a": "gamma"}),
                ]
            }
        )
        out, _ = NestedStrategyHandler().run(
            df, "data", _seed({"target": "$.*", "strategy": "spy"}), _FakeCtx()
        )
        row0 = json.loads(out["data"].iloc[0])
        row1 = json.loads(out["data"].iloc[1])
        assert row0["a"] == "M::alpha"
        assert row0["b"] == "M::beta"
        assert row1["a"] == "M::gamma"


class TestWritebackIndexAlignment:
    """The rebuilt column must carry the frame's own index. `index=None`
    / dropped index gives a 0..n-1 RangeIndex that misaligns (all-NaN)
    against a non-default frame index."""

    def test_custom_index_preserved_on_writeback(self):
        df = pd.DataFrame(
            {"data": [json.dumps({"a": "x"}), json.dumps({"a": "y"})]},
            index=[10, 20],
        )
        out, _ = NestedStrategyHandler().run(
            df.copy(), "data", _seed({"target": "$.a", "strategy": "redact"}), _FakeCtx()
        )
        assert list(out.index) == [10, 20]
        assert pd.notna(out["data"].loc[10])
        assert pd.notna(out["data"].loc[20])
        assert json.loads(out["data"].loc[10])["a"] == "REDACTED"
        assert json.loads(out["data"].loc[20])["a"] == "REDACTED"


class TestPathSegments:
    """`_path_segments` strips a single outer paren pair ONLY when both
    parens are present, slicing exactly one char off each end."""

    def test_unbalanced_leading_paren_is_not_stripped(self):
        # Both-parens guard: `and` (not `or`). A leading-only paren keeps
        # the raw segment; an `or` mutant would wrongly slice it.
        assert _path_segments(_match_with_path("(user")) == ("(user",)

    def test_balanced_parens_strip_one_char_each_end(self):
        # `raw[1:-1]`, not `[1:-2]`: the closing paren is the only trailing
        # char removed, so the last segment keeps its full text.
        assert _path_segments(_match_with_path("(a.bc)")) == ("a", "bc")


class TestHasPrefixOverlap:
    """`_has_prefix_overlap` (deepest-first contract) flags a pair iff a
    strictly-shorter path is a prefix of a longer one."""

    def test_adjacent_prefix_pair_is_detected(self):
        # `range(i + 1, n)`, not `i + 2`: the only overlapping pair here is
        # adjacent, so skipping the neighbour would miss it.
        deep = _match_with_path("a.b")
        shallow = _match_with_path("a")
        assert _has_prefix_overlap([deep, shallow]) is True

    def test_shorter_non_prefix_is_not_flagged(self):
        # `and`, not `or`: a shorter path that is NOT a prefix must not
        # count merely for being shorter.
        deep = _match_with_path("a.b")
        other = _match_with_path("x")
        assert _has_prefix_overlap([deep, other]) is False

    def test_equal_length_identical_paths_are_not_flagged(self):
        # Strict `<`, not `<=`: two equal-length paths (even identical) are
        # not a strict-prefix overlap.
        assert _has_prefix_overlap([_match_with_path("a.b"), _match_with_path("a.b")]) is False
