"""F22 A11: every publishing route returns the one commit truth, with data oracles intact."""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine import publication as pub
from decoy_engine.execution import ParquetTransactionalSink
from decoy_engine.execution._transactional_sink import publish_tables
from decoy_engine.generation._plan_entry import generate_tables, generate_tables_streamed
from tests.parity import _f0_fixtures as fx
from tests.parity._f2_mixed_support import CHILD, PARENT, child_table, mixed_config, oracle, run
from tests.parity._fr0_support import fingerprints, plan_of
from tests.unit.execution import _r3_scenarios as sc
from tests.unit.publication import _support as sup

pytestmark = pytest.mark.filterwarnings("ignore")


def _check(receipt: pub.CommitReceipt | None, sink_dir: Path) -> dict[str, object]:
    assert receipt is not None and receipt.outcome == "published"
    facts = sup.oracle(sink_dir)
    assert facts["descriptor_digest"] == receipt.descriptor_digest
    assert facts["result_id"] == receipt.result_id
    bound = {a.path for a in receipt.artifacts}
    assert bound == {p.name for p in sink_dir.glob("*.parquet")}
    return facts


@pytest.mark.parametrize(
    "scenario",
    ["ff_streamed_sink", "ff_streamed_sink_native_lane", "seq_fk_sink", "ooc_fk_forced_sink"],
)
def test_pipeline_routes_carry_the_receipt_and_bind_the_published_tables(
    scenario: str, tmp_path: Path
) -> None:
    run_ = getattr(sc, scenario)(tmp_path)
    assert run_.result.outputs == {}  # streamed: the receipt, not outputs, proves the commit
    _check(run_.result.publication_receipt, run_.sink_dir)


def test_the_receipt_survives_the_isolated_result_reconstruction(tmp_path: Path) -> None:
    from decoy_engine.execution._isolated_run import run_pipeline_isolated

    cfg, tables = sc.two_tables(tmp_path)
    for isolate in (False, True):
        out = tmp_path / f"iso_{isolate}"
        result = run_pipeline_isolated(
            cfg,
            tables,
            engine_version=sc.ENGINE_VERSION,
            output_dir=out,
            isolate=isolate,
            operation_id=f"op-{isolate}",
        )
        assert result.outcome == "completed" and result.isolated is isolate
        receipt = result.publication_receipt
        assert receipt is not None and receipt.outcome == "published"
        facts = sup.oracle(out)
        assert facts["descriptor_digest"] == receipt.descriptor_digest
        assert facts["descriptor"]["operation_id"] == f"op-{isolate}"  # type: ignore[index]


def test_isolated_retry_with_the_same_operation_is_already_committed(tmp_path: Path) -> None:
    from decoy_engine.execution._isolated_run import run_pipeline_isolated

    cfg, tables = sc.two_tables(tmp_path)
    out = tmp_path / "out"
    kw = dict(engine_version=sc.ENGINE_VERSION, output_dir=out, operation_id="op-retry")
    first = run_pipeline_isolated(cfg, tables, **kw)  # type: ignore[arg-type]
    before = sup.tree_digest(out)
    again = run_pipeline_isolated(cfg, tables, **kw)  # type: ignore[arg-type]
    assert again.outcome == "completed"
    assert again.publication_receipt.outcome == "already_committed"  # type: ignore[union-attr]
    assert (
        again.publication_receipt.descriptor_digest == first.publication_receipt.descriptor_digest
    )  # type: ignore[union-attr]
    assert sup.tree_digest(out) == before
    assert not list(tmp_path.glob("_decoy_isolated_stage_*"))


def test_isolated_child_output_never_resolves_as_committed(tmp_path: Path) -> None:
    from decoy_engine.execution._isolated_run import run_pipeline_isolated
    from decoy_engine.execution._isolated_worker import ParquetPreparationSink

    # The child's private intermediate directory carries no descriptor, whatever it holds.
    sink = ParquetPreparationSink(tmp_path / "child_stage")
    sink.write("t", sup.table_a())
    envelope = sink.commit()
    assert isinstance(envelope, pub.ChildPreparationEnvelope) and not hasattr(envelope, "outcome")
    assert not (envelope.staging_dir / "_decoy_publication.json").exists()
    assert pub.inspect_destination(envelope.staging_dir).kind == "legacy"
    assert pub.lookup_committed(pub.PublicationRequest(destination=envelope.staging_dir)) is None
    del run_pipeline_isolated


def test_isolated_cancellation_between_child_completion_and_publication(tmp_path: Path) -> None:
    from decoy_engine.execution._isolated_run import run_pipeline_isolated

    cfg, tables = sc.two_tables(tmp_path)
    out = tmp_path / "out"
    result = run_pipeline_isolated(
        cfg,
        tables,
        engine_version=sc.ENGINE_VERSION,
        output_dir=out,
        isolate=True,
        cancel_check=lambda: True,
    )
    assert result.outcome == "crashed" and result.committed_output_dir is None
    assert result.publication_receipt.outcome == "cancelled_before_commit"  # type: ignore[union-attr]
    assert not out.exists() and not list(tmp_path.glob("_decoy_isolated_stage_*"))


def test_mixed_generate_and_mask_publish_one_bound_unit(tmp_path: Path) -> None:
    child = child_table(pa.array([str(i % 5) for i in range(40)], pa.string()))
    config = mixed_config(tmp_path, child)
    target = tmp_path / "published"
    result = run(config, {CHILD: child}, sink=ParquetTransactionalSink(target))
    facts = _check(result.publication_receipt, target)
    assert {a["path"] for a in facts["descriptor"]["artifacts"]} == {  # type: ignore[index]
        f"{CHILD}.parquet",
        f"{PARENT}.parquet",
    }
    want = oracle(config, {CHILD: child}).outputs
    got = {name: pq.read_table(target / f"{name}.parquet") for name in want}
    assert fingerprints(got) == fingerprints(want)


def test_streamed_generation_returns_the_receipt(tmp_path: Path) -> None:
    plan = plan_of(
        fx.config(
            [
                fx.table(
                    "child",
                    200,
                    [
                        {"name": "cid", "type": "sequence", "start": 1},
                        {
                            "name": "pid",
                            "type": "reference",
                            "reference_table": "parent",
                            "reference_column": "id",
                        },
                    ],
                ),
                fx.table("parent", 50, [{"name": "id", "type": "sequence", "prefix": "P"}]),
            ]
        )
    )
    target = tmp_path / "out"
    result = generate_tables_streamed(plan, sink=ParquetTransactionalSink(target), batch_rows=64)
    facts = _check(result.publication_receipt, target)
    rows = {a["path"]: a["rows"] for a in facts["descriptor"]["artifacts"]}  # type: ignore[index]
    assert rows == {"parent.parquet": 50, "child.parquet": 200}
    committed = {p.stem: pq.read_table(p) for p in target.glob("*.parquet")}
    assert fingerprints(committed) == fingerprints(generate_tables(plan))  # values unchanged


def test_resident_results_publish_only_through_the_explicit_contract(tmp_path: Path) -> None:
    run_ = sc.ff_resident_sink_untouched(tmp_path)
    assert run_.result.publication_receipt is None  # a resident run publishes nothing itself
    assert not (tmp_path / "sink_out").exists()
    target = tmp_path / "explicit"
    receipt = publish_tables(
        run_.result.outputs, pub.PublicationRequest(destination=target, operation_id="resident-1")
    )
    _check(receipt, target)
    again = publish_tables(
        run_.result.outputs, pub.PublicationRequest(destination=target, operation_id="resident-1")
    )
    assert (
        again.outcome == "already_committed"
        and again.descriptor_digest == receipt.descriptor_digest
    )
    got = {p.stem: pq.read_table(p) for p in target.glob("*.parquet")}
    assert fingerprints(got) == fingerprints(run_.result.outputs)


def test_publish_tables_aborts_on_a_failed_write(tmp_path: Path) -> None:
    target = tmp_path / "explicit"
    with pytest.raises(ValueError):
        publish_tables(
            {"ok": sup.table_a(), "../bad": sup.table_b()},
            pub.PublicationRequest(destination=target),
        )
    assert not target.exists() and sup.staging_dirs(tmp_path) == []
