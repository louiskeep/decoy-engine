"""scan_sources and the scan-set/v1 document: the S1a acceptance tests."""

from __future__ import annotations

import gc
import json
import weakref
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from decoy_engine import fileaccess, reasons
from decoy_engine.config import PipelineConfigError
from decoy_engine.errors import PipelineValidationError
from decoy_engine.execution import ExecutionError
from decoy_engine.execution._transforms import TransformError
from decoy_engine.fileaccess import FileAccessDenied
from decoy_engine.profile import _readers
from decoy_engine.profiling import (
    ScanSetCore,
    ScanSetCoreRead,
    ScanSourceError,
    passes_through,
    scan_set_to_dict,
    scan_sources,
)
from decoy_engine.profiling import scan_set as scan_module
from decoy_engine.profiling._scan_text_checks import GUARD_DETECTORS
from decoy_engine.profiling.scan_set import TableCore
from decoy_engine.reason_adapter import reason_from_exception

CANARY = "CANARY-9f3a71"


def _csv(tmp_path: Path, name: str, text: str) -> dict[str, Any]:
    path = tmp_path / f"{name}.csv"
    path.write_text(text)
    return {"type": "file", "format": "csv", "path": str(path), "has_header": True}


def _table(scan: Any, name: str) -> Any:
    return next(t for t in scan.tables if t.name == name)


def test_order_follows_tables_not_sources(tmp_path: Path) -> None:
    sources = {n: _csv(tmp_path, n, "id\n1\n2\n") for n in ("a", "b", "c")}
    scan = scan_sources(sources, tables=["c", "a"])
    assert [t.name for t in scan.tables] == ["c", "a"]


def test_default_selects_every_source_in_order(tmp_path: Path) -> None:
    sources = {n: _csv(tmp_path, n, "id\n1\n") for n in ("b", "a")}
    assert [t.name for t in scan_sources(sources).tables] == ["b", "a"]


@pytest.mark.parametrize(
    "kwargs",
    [{"tables": ["nope"]}, {"tables": ["a", "a"]}, {"sample_rows": 0}, {"strategy": "x"}],
)
def test_bad_arguments_are_refused(tmp_path: Path, kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        scan_sources({"a": _csv(tmp_path, "a", "id\n1\n")}, **kwargs)


def test_invalid_descriptor_is_a_config_failure_naming_the_table(tmp_path: Path) -> None:
    bad = {"type": "file", "format": "csv", "path": CANARY, "bogus": CANARY}
    with pytest.raises(PipelineConfigError) as info:
        scan_sources({"t": bad})
    assert "'t'" in str(info.value)
    assert CANARY not in str(info.value) and CANARY not in repr(info.value)
    assert info.value.__cause__ is None and info.value.__context__ is None


def test_every_source_is_validated_selected_or_not(tmp_path: Path) -> None:
    sources = {"a": _csv(tmp_path, "a", "id\n1\n"), "b": {"type": "file", "bogus": 1}}
    with pytest.raises(PipelineConfigError):
        scan_sources(sources, tables=["a"])


def test_scan_source_error_has_its_own_runtime_identity() -> None:
    record = reason_from_exception(ScanSourceError("t", "BoomError"))
    assert (record.namespace, record.code) == ("runtime", "scan_source_failed")


def test_bounded_head_reads_one_batch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "big.parquet"
    pd.DataFrame({"id": range(50_000)}).to_parquet(path, row_group_size=5_000)
    consumed = [0]
    real = _readers.head_frame_from_batches

    def spy(batches: Any, sample_rows: int, schema: Any) -> pd.DataFrame:
        def counted() -> Any:
            for batch in batches:
                consumed[0] += batch.num_rows
                yield batch

        return real(counted(), sample_rows, schema)

    monkeypatch.setattr(_readers, "head_frame_from_batches", spy)
    scan = scan_sources(
        {"big": {"type": "file", "format": "parquet", "path": str(path)}}, sample_rows=1000
    )
    table = scan.tables[0]
    assert table.profile.row_count == 1000
    assert table.row_count == 50_000 and table.row_count_exact
    assert 0 < consumed[0] <= 2000


def test_one_table_frame_alive_at_a_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources = {n: _csv(tmp_path, n, "id\n1\n2\n") for n in ("a", "b", "c")}
    seen: list[weakref.ref[pd.DataFrame]] = []
    alive_at_call: list[int] = []
    real = scan_module.profile_columns

    def probe(frame: pd.DataFrame, *args: Any, **kwargs: Any) -> Any:
        gc.collect()
        alive_at_call.append(sum(1 for r in seen if r() is not None))
        seen.append(weakref.ref(frame))
        return real(frame, *args, **kwargs)

    monkeypatch.setattr(scan_module, "profile_columns", probe)
    scan_sources(sources)
    assert alive_at_call == [0, 0, 0]


def test_explicit_header_profiles_first_row_as_data(tmp_path: Path) -> None:
    src = _csv(tmp_path, "t", "1,x\n2,y\n3,z\n")
    src.update(has_header=False, column_names=["num", "tag"])
    table = scan_sources({"t": src}).tables[0]
    assert [f.name for f in table.profile.fields] == ["num", "tag"]
    assert table.profile.row_count == 3
    assert table.warnings == ()


def test_header_guard_flags_data_row_without_renaming(tmp_path: Path) -> None:
    src = _csv(tmp_path, "t", "jane@example.com,42\nbob@example.org,7\n")
    table = scan_sources({"t": src}).tables[0]
    assert table.warnings == (reasons.warning.SCAN_HEADER_SUSPECT.spec.ref,)
    assert [f.name for f in table.profile.fields] == ["jane@example.com", "42"]
    summary = reasons.warning.SCAN_HEADER_SUSPECT.spec.summary
    assert "@" not in summary and "jane" not in summary


@pytest.mark.parametrize("header", ["first_name,email,phone", "Name,City,State,Zip"])
def test_header_of_words_is_clean(tmp_path: Path, header: str) -> None:
    src = _csv(tmp_path, "t", header + "\n" + ",".join(["x"] * len(header.split(","))) + "\n")
    assert scan_sources({"t": src}).tables[0].warnings == ()


def test_all_numeric_header_is_flagged(tmp_path: Path) -> None:
    src = _csv(tmp_path, "t", "2024-01-02,10,3.5\n2024-01-03,11,4.5\n")
    assert len(scan_sources({"t": src}).tables[0].warnings) == 1


def test_header_with_a_name_is_a_documented_miss(tmp_path: Path) -> None:
    # Known limit: a person name in the first record is not detected, so no warning.
    src = _csv(tmp_path, "t", "Jane Doe,42,NY\nBob Roe,7,CA\n")
    assert scan_sources({"t": src}).tables[0].warnings == ()
    assert frozenset({"email", "ssn", "us_phone", "pan", "iban", "ipv4"}) == GUARD_DETECTORS


def test_guard_labels_cells_neutrally(monkeypatch: pytest.MonkeyPatch) -> None:
    # The detector allow-list already drops name-hinted detectors; the neutral label is the
    # second layer, so a header such as `Name,City` can never supply a hint.
    from decoy_engine.profiling import _scan_text_checks as checks

    labels: list[str] = []
    real = checks.run_all_detectors

    def spy(series: pd.Series, label: str) -> Any:
        labels.append(label)
        return real(series, label)

    monkeypatch.setattr(checks, "run_all_detectors", spy)
    checks.header_looks_like_data(["Name", "City", "State"])
    assert labels == ["c0", "c1", "c2"]


def test_parquet_never_runs_the_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "p.parquet"
    pd.DataFrame({"a": [1, 2]}).to_parquet(path)
    calls: list[int] = []
    monkeypatch.setattr(scan_module, "header_looks_like_data", lambda cells: calls.append(1))
    scan_sources({"p": {"type": "file", "format": "parquet", "path": str(path)}})
    assert calls == []
    scan_sources({"c": _csv(tmp_path, "c", "a\n1\n")})
    assert calls == [1]


@pytest.mark.parametrize("strategy", ["head", "full"])
def test_leading_zero_count_is_a_count_on_text(tmp_path: Path, strategy: str) -> None:
    # The probe value is longer than any digit run in a timestamp or epoch, so it cannot
    # match the document's own metadata by chance.
    probe = "0098765432101"
    src = _csv(tmp_path, "t", f"code,other\n{probe},{CANARY}\n012,{CANARY}\n5,0.5\n")
    scan = scan_sources({"t": src}, strategy=strategy)  # type: ignore[arg-type]
    table = scan.tables[0]
    assert table.leading_zero_counts == {"code": 2, "other": 0}
    assert {f.name: f.inferred_type for f in table.profile.fields}["code"] == "integer"
    assert probe not in json.dumps(scan_set_to_dict(scan, detail=False))


def test_leading_zero_is_unchecked_for_random(tmp_path: Path) -> None:
    src = _csv(tmp_path, "t", "code\n007\n012\n5\n")
    table = scan_sources({"t": src}, strategy="random", sample_rows=2).tables[0]
    assert table.leading_zero_counts == {"code": None}
    assert table.profile.row_count == 2


def test_core_model_is_the_document(tmp_path: Path) -> None:
    scan = scan_sources({"t": _csv(tmp_path, "t", "id,name\n1,a\n2,b\n")})
    doc = scan_set_to_dict(scan, detail=False)
    assert ScanSetCore.model_validate(doc).model_dump() == doc
    extra = {**doc, "added_later": 1}
    with pytest.raises(ValueError):
        ScanSetCore.model_validate(extra)
    nested = json.loads(json.dumps(doc))
    nested["tables"][0]["fields"][0]["added_later"] = 1
    with pytest.raises(ValueError):
        ScanSetCore.model_validate(nested)
    ScanSetCoreRead.model_validate(extra)
    ScanSetCoreRead.model_validate(nested)
    bad = json.loads(json.dumps(doc))
    bad["tables"][0]["row_count"] = True
    with pytest.raises(ValueError):
        ScanSetCore.model_validate(bad)


def test_empty_table_scans_with_null_rates(tmp_path: Path) -> None:
    scan = scan_sources({"t": _csv(tmp_path, "t", "id,name\n")})
    doc = scan_set_to_dict(scan, detail=False)
    json.dumps(doc, allow_nan=False)
    table = doc["tables"][0]
    assert table["reid_risk_score"] is None
    assert all(f["null_rate"] is None and f["pii_score"] is None for f in table["fields"])
    assert table["key_candidates"] == []


def test_core_carries_no_cell_value_and_detail_does(tmp_path: Path) -> None:
    rows = "\n".join(f"{CANARY}-{i},{CANARY}@x.org" for i in range(5))
    scan = scan_sources({"t": _csv(tmp_path, "t", "ident,mail\n" + rows + "\n")})
    assert CANARY not in json.dumps(scan_set_to_dict(scan, detail=False))
    assert CANARY in json.dumps(scan_set_to_dict(scan, detail=True))


def test_schema_tag_and_detail_member(tmp_path: Path) -> None:
    scan = scan_sources({"t": _csv(tmp_path, "t", "id\n1\n")})
    core, detailed = scan_set_to_dict(scan, detail=False), scan_set_to_dict(scan, detail=True)
    assert core["scan_schema"] == detailed["scan_schema"] == "scan-set/v1"
    assert "detail" not in core["tables"][0]
    assert detailed["tables"][0]["detail"]["row_count"] == 1


def test_key_candidates_and_null_counts(tmp_path: Path) -> None:
    src = _csv(tmp_path, "t", "id,grp,gap\n1,a,\n2,a,x\n3,b,y\n")
    table = scan_sources({"t": src}).tables[0]
    assert table.key_candidates == ("id",)
    assert table.null_counts == {"id": 0, "grp": 0, "gap": 1}


def test_duplicate_field_names_are_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Double:
        def row_count(self) -> Any:
            return _readers.RowCount(1, exact=True)

        def sample_frame(self, n: int) -> pd.DataFrame:
            return pd.DataFrame([[1, 2]], columns=[CANARY, CANARY])

    from decoy_engine import profile

    monkeypatch.setattr(profile, "build_declared_source", lambda *a, **k: Double())
    with pytest.raises(TransformError) as info:
        scan_sources({"t": _csv(tmp_path, "t", "a\n1\n")})
    assert info.value.code == reasons.runtime.DUPLICATE_SOURCE_FIELD_NAMES
    assert info.value.code == "duplicate_source_field_names"
    assert CANARY not in str(info.value)


def test_denied_file_access_raises_before_any_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[str] = []
    profiled: list[int] = []
    completed_reads: list[int] = []

    class Deny:
        def open_read(self, request: Any) -> Any:
            opened.append(request.field_key)
            raise FileAccessDenied(request.field_key)

    from decoy_engine import profile

    real_read = profile.read_csv_source

    def counted_read(*args: Any, **kwargs: Any) -> Any:
        out = real_read(*args, **kwargs)
        completed_reads.append(1)
        return out

    monkeypatch.setattr(profile, "read_csv_source", counted_read)
    monkeypatch.setattr(scan_module, "profile_columns", lambda *a, **k: profiled.append(1))
    with pytest.raises(FileAccessDenied) as info:
        scan_sources({"t": _csv(tmp_path, "t", "a\n1\n")}, file_access=Deny())  # type: ignore[arg-type]
    assert opened
    assert completed_reads == [] and profiled == []
    record = reason_from_exception(info.value)
    assert (record.namespace, record.code) == ("decoy", "file_access_denied")


def test_headerless_width_refusal_passes_through(tmp_path: Path) -> None:
    src = _csv(tmp_path, "t", "1,2,3\n")
    src.update(has_header=False, column_names=["a", "b"])
    with pytest.raises(PipelineValidationError):
        scan_sources({"t": src})


def test_reader_error_text_never_reaches_the_raised_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BoomError(Exception):
        pass

    class Double:
        def row_count(self) -> Any:
            raise BoomError(CANARY)

    from decoy_engine import profile

    monkeypatch.setattr(profile, "build_declared_source", lambda *a, **k: Double())
    with pytest.raises(ScanSourceError) as info:
        scan_sources({"t": _csv(tmp_path, "t", "a\n1\n")})
    assert "'t'" in str(info.value) and "Exception" in str(info.value)
    assert CANARY not in str(info.value) and CANARY not in repr(info.value)
    assert info.value.__cause__ is None and info.value.__context__ is None


def _raising_double(monkeypatch: pytest.MonkeyPatch, exc: BaseException) -> None:
    class Double:
        def row_count(self) -> Any:
            raise exc

    from decoy_engine import profile

    monkeypatch.setattr(profile, "build_declared_source", lambda *a, **k: Double())


def test_coded_engine_error_is_not_flattened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raised = TransformError(code=reasons.runtime.DROP_COLUMN_MISSING, message="table 't' x")
    _raising_double(monkeypatch, raised)
    with pytest.raises(TransformError) as info:
        scan_sources({"t": _csv(tmp_path, "t", "a\n1\n")})
    assert info.value is raised
    record = reason_from_exception(info.value)
    assert (record.namespace, record.code) == ("runtime", "drop_column_missing")


def test_scan_source_error_from_a_layer_below_is_not_rewrapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raised = ScanSourceError("inner", "Inner")
    _raising_double(monkeypatch, raised)
    with pytest.raises(ScanSourceError) as info:
        scan_sources({"t": _csv(tmp_path, "t", "a\n1\n")})
    assert info.value is raised


def test_core_is_built_and_dumped_through_the_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scan = scan_sources({"t": _csv(tmp_path, "t", "id,name\n1,a\n2,b\n")})
    real = ScanSetCore.model_dump
    dumped: list[dict[str, Any]] = []

    def spy(self: ScanSetCore, *args: Any, **kwargs: Any) -> dict[str, Any]:
        out = real(self, *args, **kwargs)
        dumped.append(out)
        return out

    monkeypatch.setattr(ScanSetCore, "model_dump", spy)
    doc = scan_set_to_dict(scan, detail=False)
    assert len(dumped) == 1
    assert doc == dumped[0]


def test_header_suspect_table_never_carries_the_header_text(tmp_path: Path) -> None:
    src = _csv(tmp_path, "t", f"{CANARY}@example.com,42\nbob@example.org,7\n")
    scan = scan_sources({"t": src})
    table = scan.tables[0]
    assert table.warnings == (reasons.warning.SCAN_HEADER_SUSPECT.spec.ref,)
    assert [f.name for f in table.profile.fields] == [f"{CANARY}@example.com", "42"]
    doc = scan_set_to_dict(scan, detail=False)
    assert CANARY not in json.dumps(doc)
    core = doc["tables"][0]
    assert [f["name"] for f in core["fields"]] == ["c0", "c1"]
    assert all(name in {"c0", "c1"} for name in core["key_candidates"])
    assert all(name in {"c0", "c1"} for group in core["quasi_identifier_groups"] for name in group)
    assert [w["code"] for w in core["warnings"]] == ["scan_header_suspect"]
    assert CANARY in json.dumps(scan_set_to_dict(scan, detail=True))


def test_clean_header_keeps_its_names_in_the_core(tmp_path: Path) -> None:
    scan = scan_sources({"t": _csv(tmp_path, "t", "id,city\n1,a\n2,b\n")})
    core = scan_set_to_dict(scan, detail=False)["tables"][0]
    assert [f["name"] for f in core["fields"]] == ["id", "city"]


def test_closed_sets_are_types() -> None:
    doc = {
        "name": "t",
        "descriptor": {},
        "row_count": 0,
        "row_count_exact": True,
        "sample_strategy": "head",
        "sample_row_cap": None,
        "fields": [],
        "key_candidates": [],
        "warnings": [],
        "reid_risk_score": None,
        "quasi_identifier_groups": [],
    }
    TableCore.model_validate(doc)
    with pytest.raises(ValueError):
        TableCore.model_validate({**doc, "sample_strategy": "stratified"})
    ok = {"namespace": "warning", "code": "scan_header_suspect"}
    TableCore.model_validate({**doc, "warnings": [ok]})
    with pytest.raises(ValueError):
        TableCore.model_validate({**doc, "warnings": [{"namespace": "warning", "code": "nope"}]})
    # An older reader still accepts a reason code a newer registry added; the strategy set is
    # closed on both sides.
    relaxed = {"scan_schema": "scan-set/v1", "engine_version": "9", "scanned_at": "x"}
    ScanSetCoreRead.model_validate(
        {
            **relaxed,
            "tables": [{**doc, "warnings": [{**ok, "code": "z"}]}],
            "relationships": [],
        }
    )


def test_entry_point_is_registered() -> None:
    assert "decoy_engine.profiling.scan_set.scan_sources" in fileaccess.ENTRY_POINTS


def lazy_source_changed(table: str, detail: str) -> ExecutionError:
    return ExecutionError(code=reasons.runtime.LAZY_SOURCE_CHANGED, message=f"{table} {detail}")


def _declared() -> list[dict[str, Any]]:
    return [
        {
            "parent": {"table": "p", "columns": ["id"]},
            "children": [{"table": "c", "columns": ["pid"]}],
            "orphan_policy": "warn",
        }
    ]


def test_table_read_passes_a_coded_error_through_and_wraps_an_uncoded_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Characterization of the per-table site: behavior pinned before the rule became shared."""
    sources = {"t": _csv(tmp_path, "t", "a\n1\n")}
    coded = lazy_source_changed("t", "gone")

    def refuse(*args: Any) -> Any:
        raise coded

    monkeypatch.setattr(scan_module, "_read_and_profile", refuse)
    with pytest.raises(type(coded)) as info:
        scan_sources(sources)
    assert info.value is coded

    def crash(*args: Any) -> Any:
        raise TypeError(CANARY)

    monkeypatch.setattr(scan_module, "_read_and_profile", crash)
    with pytest.raises(ScanSourceError) as wrapped:
        scan_sources(sources)
    assert wrapped.value.cause_class == "TypeError"
    assert CANARY not in str(wrapped.value)


def test_relationship_facts_pass_a_coded_or_config_error_through_and_wrap_an_uncoded_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Characterization of the relationship site, which also lets a config error through."""
    sources = {"p": _csv(tmp_path, "p", "id\n1\n"), "c": _csv(tmp_path, "c", "pid\n1\n")}
    coded = lazy_source_changed("p", "gone")
    config = PipelineConfigError("bad")
    for err in (coded, config):

        def refuse(*args: Any, **kwargs: Any) -> Any:
            raise err

        monkeypatch.setattr(scan_module._scan_integrity, "relationship_facts", refuse)
        with pytest.raises(type(err)) as info:
            scan_sources(sources, relationships=_declared())
        assert info.value is err

    def crash(*args: Any, **kwargs: Any) -> Any:
        raise TypeError(CANARY)

    monkeypatch.setattr(scan_module._scan_integrity, "relationship_facts", crash)
    with pytest.raises(ScanSourceError) as wrapped:
        scan_sources(sources, relationships=_declared())
    assert (wrapped.value.table, wrapped.value.cause_class) == ("relationships", "TypeError")


class _Local(Exception):
    pass


def test_the_pass_through_rule_is_one_public_predicate() -> None:
    assert passes_through(lazy_source_changed("t", "x"))
    assert passes_through(PipelineValidationError("x"))
    assert passes_through(FileAccessDenied("x"))
    assert passes_through(ScanSourceError("t", "TypeError"))
    assert not passes_through(TypeError("x"))
    assert not passes_through(_Local("x"))
