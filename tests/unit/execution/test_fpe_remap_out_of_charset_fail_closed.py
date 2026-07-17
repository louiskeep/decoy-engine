"""Finding #42 regression: orphan_policy=REMAP + FPE + all-out-of-charset orphan.

Finding #42 was "orphan_policy:remap emits out-of-charset orphan keys in the
clear." It is resolved at source by DE-01 cluster-C: an all-out-of-charset value
raises `FpeUnencryptableError`, which `FpeStrategyHandler.run` re-raises as a
job-fatal `StrategyError(fpe_unencryptable_value)`. The existing DE-01 tests
prove that at the `fpe_encrypt_value` and single-handler levels; NONE exercised
the specific configuration #42 named -- an orphan key masked via the PARENT's
FPE strategy through the REMAP closure (`_orphan.make_remap_fn`, which calls
`handler.run` on the parent column).

This test pins that end-to-end: the parent and its non-orphan children are all
in-charset (digits) and mask normally, but the child carries one orphan whose
characters are ALL outside the digits charset. Under REMAP the orphan is routed
through the parent's FPE strategy, which must FAIL CLOSED rather than emit the
orphan verbatim (the #42 leak) or a non-invertible covering hash.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution import PandasExecutionAdapter
from decoy_engine.execution._errors import StrategyError
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry

_REG = get_default_registry()
_NS = NamespaceRegistry(bindings=())
_SEED = (0xABCD).to_bytes(8, "big")


def _fpe_digits_col() -> ColumnSeed:
    # digits charset (0-9): an uppercase/letter orphan key is entirely
    # out-of-charset, the exact shape #42 leaked verbatim pre-DE-01.
    return ColumnSeed(
        namespace="cust",
        strategy="fpe",
        provider="fpe",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=(("charset", "digits"),),
        coherent_with=(),
    )


def _plan() -> Any:
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(
                (
                    "customers",
                    TableSeed(per_column=(("customer_id", _fpe_digits_col()),), per_group=()),
                ),
                (
                    "orders",
                    TableSeed(per_column=(("customer_id", _fpe_digits_col()),), per_group=()),
                ),
            ),
        )
    )


def _graph() -> RelationshipGraph:
    edge = RelationshipEdge(
        parent_table="customers",
        parent_columns=("customer_id",),
        child_table="orders",
        child_columns=("customer_id",),
        namespace="cust",
        orphan_policy=OrphanPolicy.REMAP,
    )
    return RelationshipGraph(edges=(edge,), ordering=())


def _sources(orphan_key: str) -> dict[str, pa.Table]:
    # Parent + non-orphan children are in-charset (digits) so the normal FPE
    # path runs; `orphan_key` is the child-only value that gets REMAP'd through
    # the parent's FPE strategy.
    return {
        "customers": pa.table({"customer_id": ["100", "200", "300"]}),
        "orders": pa.table({"customer_id": ["100", "200", "100", orphan_key]}),
    }


def _run(orphan_key: str) -> Any:
    return PandasExecutionAdapter().run(
        _plan(), _sources(orphan_key), registry=_REG, relationship_graph=_graph(), namespace_registry=_NS
    )


class TestRemapOutOfCharsetOrphanFailsClosed:
    @pytest.mark.parametrize("orphan_key", ["GHOST", "N/A", "UNKNOWN", "ORPHAN"])
    def test_all_out_of_charset_orphan_fails_closed(self, orphan_key: str) -> None:
        """An all-out-of-charset REMAP orphan must fail the job closed, not leak.

        Pre-DE-01 this emitted the orphan verbatim (finding #42). It must now
        raise the fail-closed FPE error attributed to the fpe strategy.
        """
        with pytest.raises(StrategyError) as exc:
            _run(orphan_key)
        assert exc.value.code == "fpe_unencryptable_value", (
            f"expected the DE-01 fail-closed code, got {exc.value.code!r}"
        )

    def test_no_verbatim_orphan_in_any_output(self) -> None:
        """Belt-and-suspenders: the leak is a RAISE, so no masked frame is ever
        produced. If the engine ever regressed to returning a frame, the orphan
        must not survive verbatim in it."""
        orphan = "GHOST"
        try:
            res = _run(orphan)
        except StrategyError:
            return  # fail-closed: no output produced, nothing to leak
        # Should be unreachable given the raise above; assert defensively.
        child = res.outputs["orders"].column("customer_id").to_pylist()
        assert orphan not in child, "out-of-charset orphan key leaked verbatim (finding #42)"

    def test_in_charset_orphan_still_remaps_normally(self) -> None:
        """Control: an IN-charset orphan (all digits) is not a leak case -- it
        REMAPs through FPE to a fresh masked value, proving the fail-closed is
        specific to un-encryptable orphans, not REMAP itself."""
        res = _run("999")  # in-charset digits: a normal orphan
        child = res.outputs["orders"].column("customer_id").to_pylist()
        assert child[3] != "999", "in-charset orphan should be masked, not preserved"
        assert child[3] is not None
