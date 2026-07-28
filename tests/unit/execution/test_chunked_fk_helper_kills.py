"""TQ mutation-kill oracles for `execution/_chunked_fk.py` HELPER functions:
`_dtype_family`, `_col_index_from_config`, `fk_passthrough_columns_for_table`,
and `reject_lossy_chunked_fk_passthrough`. The big `gate_fk_child_edges`
admission gate is graded by its own kill file. These drive the leaf helpers
directly (pure functions / raw configs / hand-built chunks) and assert the
machine fields each decides on -- the resolved dtype family, the FK column set,
and the fail-closed reject code / offending name.
"""

from __future__ import annotations

import pyarrow as pa
import pytest

from decoy_engine.execution import ExecutionError
from decoy_engine.execution._chunked_fk import (
    _DECIMAL_UNPROVABLE_FAMILY,
    _col_index_from_config,
    _dtype_family,
    fk_passthrough_columns_for_table,
    reject_lossy_chunked_fk_passthrough,
)
from decoy_engine.execution._fk_keys import FK_KEY_DTYPE_UNSUPPORTED_CODE

_BOUND = 2**53  # _EXACT_FLOAT_INT_BOUND: the largest exactly-float64-representable int


class TestDtypeFamily:
    """`_dtype_family` maps a dtype string to a coarse RI-equivalence family.
    Each case exercises exactly one prefix branch / return so a string-literal
    XX-wrap or case mutation of that branch is observable (the mutated prefix
    stops matching, so the value falls through to the lowercased passthrough).
    """

    @pytest.mark.parametrize(
        ("dtype", "expected"),
        [
            # int family: "int" and the DISTINCT "uint" prefix (uint does not
            # start with int), plus the return literal.
            ("int64", "int"),
            ("uint32", "int"),
            # float family: "float" and the distinct "double" prefix.
            ("float64", "float"),
            ("double", "float"),
            # bool: a bare "bool" cannot kill the PREFIX mutation (its passthrough
            # lowercases to "bool" == the family), so "boolean" makes the prefix
            # load-bearing; "bool" still pins the return literal.
            ("bool", "bool"),
            ("boolean", "bool"),
            # string family: "str" (matches "string" too), and the members that
            # do NOT start with "str" so their own prefix is load-bearing.
            ("str", "string"),
            ("object", "string"),
            ("utf8", "string"),
            ("large_string", "string"),
            # timestamp/datetime (checked BEFORE date; "datetime" is a superset
            # prefix of "date").
            ("timestamp[us]", "timestamp"),
            ("datetime64[ns]", "timestamp"),
            # date (only after timestamp/datetime ruled out).
            ("date32", "date"),
            # bytes family: each distinct prefix. "bytes32" (not a bare "bytes")
            # keeps the "bytes" prefix load-bearing; "binary" pins the return.
            ("bytes32", "bytes"),
            ("binary", "bytes"),
            ("large_binary", "bytes"),
            ("fixed_size_binary[16]", "bytes"),
            # unknown -> lowercased passthrough (only matches its own exact string).
            ("Geometry", "geometry"),
        ],
    )
    def test_family_for_each_branch(self, dtype: str, expected: str) -> None:
        assert _dtype_family(dtype) == expected

    def test_decimal_scale_keyed_precision_irrelevant(self) -> None:
        """decimal/numeric parse to a SCALE-keyed family (precision + width are
        irrelevant to RI); pins the regex scale group and both keyword prefixes."""
        assert _dtype_family("decimal128(5, 2)") == "decimal(scale=2)"
        assert _dtype_family("decimal256(3, 2)") == "decimal(scale=2)"  # width irrelevant
        assert _dtype_family("decimal128(9, 2)") == "decimal(scale=2)"  # precision irrelevant
        assert _dtype_family("numeric(10, 0)") == "decimal(scale=0)"

    def test_bare_decimal_is_the_unprovable_sentinel(self) -> None:
        """A scale-less decimal/numeric cannot be proven interchangeable (the
        chunked route sees one side at a time) -> the distinct sentinel family."""
        assert _dtype_family("decimal") == _DECIMAL_UNPROVABLE_FAMILY
        assert _dtype_family("numeric") == _DECIMAL_UNPROVABLE_FAMILY
        # The sentinel differs from a concrete scale family (so a bare vs scaled
        # declaration is NOT admitted as equal at the compile gate).
        assert _DECIMAL_UNPROVABLE_FAMILY != "decimal(scale=0)"


class TestColIndexFromConfig:
    """`_col_index_from_config` builds a (table, col) -> entry lookup, skipping
    non-dict table entries and non-str table names with `continue` (NOT `break`,
    which would drop every later valid table)."""

    def test_nondict_table_entry_does_not_stop_indexing(self) -> None:
        # A non-dict entry precedes a valid table: `continue` keeps indexing it,
        # `break` would skip it entirely.
        config = {
            "tables": [
                "not-a-dict",
                {"name": "orders", "columns": [{"name": "customer_id"}]},
            ]
        }
        idx = _col_index_from_config(config)
        assert ("orders", "customer_id") in idx

    def test_nonstr_table_name_does_not_stop_indexing(self) -> None:
        config = {
            "tables": [
                {"name": 123, "columns": [{"name": "x"}]},
                {"name": "orders", "columns": [{"name": "customer_id"}]},
            ]
        }
        idx = _col_index_from_config(config)
        assert ("orders", "customer_id") in idx


def _passthrough_rel_config(*, child_dtype: str = "int64") -> dict:
    """customers.id -> orders.customer_id, both passthrough."""
    return {
        "tables": [
            {"name": "customers", "columns": [{"name": "id", "strategy": "passthrough"}]},
            {
                "name": "orders",
                "columns": [
                    {"name": "customer_id", "strategy": "passthrough", "dtype": child_dtype}
                ],
            },
        ],
        "relationships": [
            {
                "parent": {"table": "customers", "columns": ["id"]},
                "children": [{"table": "orders", "columns": ["customer_id"]}],
                "orphan_policy": "remap",
            }
        ],
    }


class TestFkPassthroughColumnsForTable:
    """`fk_passthrough_columns_for_table` collects the passthrough FK columns on
    a table across BOTH roles of every relationship edge."""

    def test_child_query_excludes_the_parent_key(self) -> None:
        # Querying the CHILD table: the parent-role branch guard is
        # `isinstance(parent, dict) AND parent.table == table`. The `and`->`or`
        # mutant folds in the OTHER table's parent key columns. To make that
        # observable past the `& passthrough_columns` intersection, the parent
        # key name (`shared`) must ALSO be a passthrough column on the queried
        # child table -- then the mutant leaks `shared` into the result.
        config = {
            "tables": [
                {"name": "customers", "columns": [{"name": "shared", "strategy": "passthrough"}]},
                {
                    "name": "orders",
                    "columns": [
                        {"name": "customer_id", "strategy": "passthrough"},
                        {"name": "shared", "strategy": "passthrough"},
                    ],
                },
            ],
            "relationships": [
                {
                    "parent": {"table": "customers", "columns": ["shared"]},
                    "children": [{"table": "orders", "columns": ["customer_id"]}],
                }
            ],
        }
        # `shared` is a passthrough column of orders but NOT an FK-relevant column
        # of orders, so it must not appear; the `or` mutant wrongly adds it.
        assert fk_passthrough_columns_for_table(config, "orders") == {"customer_id"}

    def test_mismatched_child_entry_is_skipped(self) -> None:
        # A child_info dict whose table != the queried table must be skipped
        # (`not isinstance OR table != table`); the `or`->`and` mutant stops
        # skipping it. As with the parent-role kill, the mismatched child's
        # column name (`ghost`) must also be a passthrough column of the queried
        # table for the leak to survive the `& passthrough_columns` intersection.
        config = {
            "tables": [
                {"name": "customers", "columns": [{"name": "id", "strategy": "passthrough"}]},
                {
                    "name": "orders",
                    "columns": [
                        {"name": "customer_id", "strategy": "passthrough"},
                        {"name": "ghost", "strategy": "passthrough"},
                    ],
                },
            ],
            "relationships": [
                {
                    "parent": {"table": "customers", "columns": ["id"]},
                    "children": [
                        {"table": "elsewhere", "columns": ["ghost"]},
                        {"table": "orders", "columns": ["customer_id"]},
                    ],
                }
            ],
        }
        assert fk_passthrough_columns_for_table(config, "orders") == {"customer_id"}

    def test_leading_mismatched_child_does_not_stop_later_match(self) -> None:
        # A non-matching child precedes the matching one in the children LIST;
        # `continue` reaches the match, `break` would drop it.
        config = _passthrough_rel_config()
        config["relationships"][0]["children"].insert(0, {"table": "elsewhere", "columns": ["c"]})
        assert fk_passthrough_columns_for_table(config, "orders") == {"customer_id"}

    def test_leading_nondict_relationship_does_not_stop_later_match(self) -> None:
        # A non-dict relationship entry precedes the real one in the LIST;
        # `continue` reaches the real edge, `break` would drop it.
        config = _passthrough_rel_config()
        config["relationships"].insert(0, "not-a-dict")
        assert fk_passthrough_columns_for_table(config, "orders") == {"customer_id"}

    def test_fk_column_with_no_table_entry_returns_empty_not_raises(self) -> None:
        # The FK edge names `orders` but config.tables has no `orders` entry, so
        # `next((...), None)` must default to None -> return set(). Dropping the
        # `None` default makes `next` raise StopIteration.
        config = {
            "tables": [
                {"name": "customers", "columns": [{"name": "id", "strategy": "passthrough"}]}
            ],
            "relationships": [
                {
                    "parent": {"table": "customers", "columns": ["id"]},
                    "children": [{"table": "orders", "columns": ["customer_id"]}],
                }
            ],
        }
        assert fk_passthrough_columns_for_table(config, "orders") == set()


def _chunk(**cols: pa.Array) -> pa.Table:
    return pa.table(cols)


def _int(values: list) -> pa.Array:
    return pa.array(values, type=pa.int64())


class TestRejectLossyChunkedFkPassthrough:
    """The per-chunk fail-closed guard for a null-bearing passthrough FK column
    carrying a value beyond exact float64 precision. The loop accepts any
    iterable, so an ordered LIST forces the deterministic column order the
    `continue`->`break` kills need (production passes a set; the loop logic is
    what is under test)."""

    def test_above_bound_raises_with_code_and_names_the_column(self) -> None:
        chunk = _chunk(customer_id=_int([1, None, _BOUND + 1]))
        with pytest.raises(ExecutionError) as exc:
            reject_lossy_chunked_fk_passthrough(
                chunk, table="orders", passthrough_fk_columns={"customer_id"}
            )
        assert exc.value.code == FK_KEY_DTYPE_UNSUPPORTED_CODE
        # the offending table.column is DATA in the message, pinned separately
        # from the explanatory prose.
        assert "orders.customer_id" in exc.value.message

    def test_max_exactly_at_bound_is_admitted(self) -> None:
        # col_max == 2**53 is exactly representable, so `> bound` (strict) admits
        # it; the `>=` mutant would wrongly reject.
        chunk = _chunk(customer_id=_int([1, None, _BOUND]))
        reject_lossy_chunked_fk_passthrough(
            chunk, table="orders", passthrough_fk_columns={"customer_id"}
        )

    def test_min_exactly_at_negative_bound_is_admitted(self) -> None:
        # col_min == -2**53 is exactly representable, so `< -bound` (strict)
        # admits it; the `<=` mutant would wrongly reject.
        chunk = _chunk(customer_id=_int([-_BOUND, None, 1]))
        reject_lossy_chunked_fk_passthrough(
            chunk, table="orders", passthrough_fk_columns={"customer_id"}
        )

    def test_absent_column_before_lossy_still_rejects(self) -> None:
        # ordered list: an absent column (`continue`) must not `break` the loop
        # before the later lossy column is checked.
        chunk = _chunk(lossy=_int([1, None, _BOUND + 1]))
        with pytest.raises(ExecutionError):
            reject_lossy_chunked_fk_passthrough(
                chunk, table="t", passthrough_fk_columns=["ghost", "lossy"]
            )

    def test_noninteger_column_before_lossy_still_rejects(self) -> None:
        chunk = _chunk(label=pa.array(["a", "b", "c"]), lossy=_int([1, None, _BOUND + 1]))
        with pytest.raises(ExecutionError):
            reject_lossy_chunked_fk_passthrough(
                chunk, table="t", passthrough_fk_columns=["label", "lossy"]
            )

    def test_null_free_column_before_lossy_still_rejects(self) -> None:
        chunk = _chunk(clean=_int([1, 2, 3]), lossy=_int([1, None, _BOUND + 1]))
        with pytest.raises(ExecutionError):
            reject_lossy_chunked_fk_passthrough(
                chunk, table="t", passthrough_fk_columns=["clean", "lossy"]
            )

    def test_all_null_column_before_lossy_still_rejects(self) -> None:
        chunk = _chunk(allnull=_int([None, None, None]), lossy=_int([1, None, _BOUND + 1]))
        with pytest.raises(ExecutionError):
            reject_lossy_chunked_fk_passthrough(
                chunk, table="t", passthrough_fk_columns=["allnull", "lossy"]
            )
