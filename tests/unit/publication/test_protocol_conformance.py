"""F22 A10: declarations, admission, and the reusable conformance suite."""

from __future__ import annotations

from pathlib import Path

import pyarrow.parquet as pq
import pytest

from decoy_engine import publication as pub
from decoy_engine.connectors import S3FileSink
from decoy_engine.execution._transactional_sink import (
    ManagedDatasetSink,
    ParquetPreparationSink,
    ParquetTransactionalSink,
    _CallableSinkAdapter,
)
from decoy_engine.publication import conformance as conf
from decoy_engine.sdk import FileSink


def _immutable_reader(path: Path) -> dict[str, int]:
    return {p.stem: pq.read_metadata(p).num_rows for p in path.glob("*.parquet")}


def _managed_reader(path: Path) -> dict[str, int]:
    pin = pub.resolve_current(path)
    assert pin is not None
    return _immutable_reader(pin.path)


def test_local_directory_unit_passes_the_conformance_suite(tmp_path: Path) -> None:
    conf.run_conformance(
        conf.Adapter(
            "immutable_directory",
            lambda r: ParquetTransactionalSink(r.destination, request=r),
            _immutable_reader,
            pub.UNIT_IMMUTABLE_DIRECTORY,
        ),
        tmp_path,
    )


def test_managed_dataset_unit_passes_the_conformance_suite(tmp_path: Path) -> None:
    conf.run_conformance(
        conf.Adapter(
            "managed_dataset",
            lambda r: ManagedDatasetSink(r.destination, request=r),
            _managed_reader,
            pub.UNIT_MANAGED_DATASET,
        ),
        tmp_path,
    )


def test_the_suite_rejects_a_non_conforming_adapter(tmp_path: Path) -> None:
    class Lax(ParquetTransactionalSink):
        def commit(self):  # type: ignore[no-untyped-def]
            r = super().commit()
            self.abort()
            return r

        def write_batches(self, table, batches, *, schema):  # type: ignore[no-untyped-def]
            # swallows the producer failure: the contract says it must propagate
            try:
                super().write_batches(table, batches, schema=schema)
            except RuntimeError:
                return

    with pytest.raises(conf.ConformanceError):
        conf.run_conformance(
            conf.Adapter(
                "lax",
                lambda r: Lax(r.destination, request=r),
                _immutable_reader,
                pub.UNIT_IMMUTABLE_DIRECTORY,
            ),
            tmp_path,
        )


def test_a_write_commit_abort_lookalike_cannot_claim_descriptor_support(tmp_path: Path) -> None:
    class Lookalike:
        def write(self, table, data): ...  # type: ignore[no-untyped-def]
        def write_batches(self, table, batches, *, schema): ...  # type: ignore[no-untyped-def]
        def commit(self): ...  # type: ignore[no-untyped-def]
        def abort(self): ...  # type: ignore[no-untyped-def]

    from decoy_engine.execution._transactional_sink import TransactionalSink

    assert isinstance(Lookalike(), TransactionalSink)  # method shape matches...
    with pytest.raises(pub.PublicationRefusedError) as exc:
        pub.admit(Lookalike())
    assert exc.value.receipt.outcome == "unsupported"
    assert exc.value.receipt.reason_code == "not_a_publishing_protocol"
    assert pub.declaration_for(Lookalike()).name == "undeclared"  # ...but it is undeclared


@pytest.mark.parametrize(
    ("sink", "name"),
    [
        (_CallableSinkAdapter(lambda t, d: None), "callable_sink"),
        (ParquetPreparationSink(Path("x")), "parquet_preparation_only"),
    ],
)
def test_non_publishing_sinks_are_refused_before_writing(sink: object, name: str) -> None:
    assert pub.declaration_for(sink).name == name
    with pytest.raises(pub.PublicationRefusedError):
        pub.admit(sink)


def test_transfer_primitives_are_declared_honestly_not_qualified() -> None:
    assert issubclass(S3FileSink, FileSink)
    s3 = S3FileSink.__new__(S3FileSink)
    assert pub.declaration_for(s3).name == "file_sink_transfer"
    with pytest.raises(pub.PublicationRefusedError):
        pub.admit(s3)
    from decoy_engine.connectors import GCSFileSink, SFTPFileSink

    for cls in (GCSFileSink, SFTPFileSink):
        assert pub.declaration_for(cls.__new__(cls)).publishes is False


def test_local_sinks_are_admitted_and_replace_is_unit_checked(tmp_path: Path) -> None:
    dest = tmp_path / "out"
    assert pub.admit(ParquetTransactionalSink(dest)).unit == pub.UNIT_IMMUTABLE_DIRECTORY
    managed = pub.PublicationRequest(destination=dest, unit=pub.UNIT_MANAGED_DATASET)
    assert pub.admit(ManagedDatasetSink(dest), managed).supports_backup
    replace_immutable = pub.PublicationRequest(destination=dest, overwrite=pub.Replace("a" * 64))
    with pytest.raises(pub.PublicationRefusedError) as exc:
        pub.admit(ParquetTransactionalSink(dest), replace_immutable)
    assert exc.value.receipt.reason_code == "replace_unsupported_for_unit"
    needs_backup = pub.PublicationRequest(
        destination=dest,
        overwrite=pub.Replace("a" * 64, pub.ProtectedBackup("p")),
        unit=pub.UNIT_MANAGED_DATASET,
    )
    assert "backup_unsupported" not in pub.violations(
        pub.declaration_for(ManagedDatasetSink(dest)), needs_backup
    )


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        (dict(atomic_scope="none"), "no_atomic_scope"),
        (dict(staging_isolation=False), "no_staging_isolation"),
        (dict(descriptor_lookup=False), "no_descriptor_lookup"),
        (dict(durability="none"), "no_durability"),
        (dict(fencing="none"), "multi_object_release_without_cas"),
        (dict(fencing="lease_check"), "lease_check_without_storage_fencing"),
    ],
)
def test_negative_declarations_are_each_rejected(changes: dict[str, object], reason: str) -> None:
    from dataclasses import replace

    bad = replace(pub.declaration_for(ManagedDatasetSink(Path("x"))), **changes)  # type: ignore[arg-type]
    assert reason in pub.violations(bad)


def test_required_backup_without_protection_is_rejected() -> None:
    from dataclasses import replace

    bad = replace(pub.declaration_for(ManagedDatasetSink(Path("x"))), supports_backup=False)
    request = pub.PublicationRequest(
        destination=Path("x"),
        unit=pub.UNIT_MANAGED_DATASET,
        overwrite=pub.Replace("a" * 64, pub.ProtectedBackup("p")),
    )
    assert "backup_unsupported" in pub.violations(bad, request)


def test_the_generated_capability_table_is_the_declarations() -> None:
    rows = dict(pub.capability_rows())
    assert set(rows) == {
        "parquet_immutable_directory",
        "parquet_managed_dataset",
        "parquet_preparation_only",
        "callable_sink",
        "file_sink_transfer",
    }
    assert {n for n, d in rows.items() if d.publishes} == {
        "parquet_immutable_directory",
        "parquet_managed_dataset",
    }
    assert all(not pub.violations(d) for n, d in rows.items() if d.publishes)
    doc = (Path(__file__).parents[3] / "docs" / "capability-matrix.md").read_text()
    for name in rows:
        assert f"`{name}`" in doc
