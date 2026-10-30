"""F06 r3: every admitted cell of the format x route x policy matrix runs on its real route.

The cases are generated from the pinned matrix (``tests/fixtures/f06/route-format-matrix-v1.json``),
so a newly admitted cell is a new test, and each case asserts the route identity the engine
reported before it compares the published rows with the catalog kernels' own result. The sentry
``tests/sentry/test_f06_route_matrix.py`` requires this module to cover every admitted cell.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.acceptance import _f06_cells as cells
from tests.acceptance import test_f06_route_parity as parity

CELLS = cells.admitted_cells()


def _runtime(tmp_path: Path, policy: str) -> Any:
    return parity._runtime(tmp_path / "q") if policy == "quarantine" else None


def _published(result: Any, table: str) -> dict[str, Any]:
    out = result.outputs[table]
    return dict(zip(out.column("id").to_pylist(), out.column("c").to_pylist(), strict=True))


def _opened_ids(result: Any, tmp_path: Path) -> list[str]:
    from decoy_engine.declared_casting import QuarantineAccessContext

    records = parity._runtime(tmp_path / "q").sink.open(
        result.quarantine_access, QuarantineAccessContext(owner="tenant-1", run_id="run-A")
    )
    return sorted(r.row["id"] for r in records)


@pytest.mark.parametrize(("route", "key", "policy"), CELLS, ids=["-".join(c) for c in CELLS])
def test_admitted_cell_runs_on_its_route(tmp_path: Path, route: str, key: str, policy: str) -> None:
    driver = cells.DRIVERS[route]
    corpus = cells.build_corpus(key, invalid=policy != "fail")
    want = cells.expected(corpus, policy)
    declined = cells.physical_decline(route, key) is not None
    result, table = driver(tmp_path, corpus, policy, _runtime(tmp_path, policy), declined)
    assert _published(result, table) == want["rows"]
    if policy == "quarantine" and want["quarantined"]:
        assert _opened_ids(result, tmp_path) == sorted(want["quarantined"])
    elif policy == "quarantine":
        assert result.quarantine_access is None, "no row was quarantined, so no artifact exists"
    counts = result.quality_metrics["declared_casting"]
    assert counts["total_failures"] == want["failures"]


@pytest.mark.parametrize(
    ("route", "key"),
    sorted({(r, k) for r, k, p in CELLS if p == "fail"}),
    ids=lambda v: str(v),
)
def test_fail_policy_cell_rejects_the_invalid_value_on_its_route(
    tmp_path: Path, route: str, key: str
) -> None:
    from decoy_engine.execution._errors import DeclaredCastValueError

    corpus = cells.build_corpus(key, invalid=True)
    want = cells.expected(corpus, "fail")
    if want["first_failure"] is None:  # every input is valid (a string never fails to parse)
        result, table = cells.DRIVERS[route](tmp_path, corpus, "fail", None)
        assert _published(result, table) == want["rows"]
        return
    with pytest.raises(DeclaredCastValueError) as raised:
        cells.DRIVERS[route](tmp_path, corpus, "fail", None)
    assert raised.value.code == want["first_failure"]
