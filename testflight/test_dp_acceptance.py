"""Test-flight DP acceptance: the shipped differential-privacy path is part of
the golden gate.

DP output is deliberately noisy (production noise is unseeded), so this asserts
the CONTRACT the ship signal depends on, not an output fingerprint: a fit over
declared typed carriers emits a `dps-marginal/v3` artifact whose released
columns are exactly the declared ones, that artifact drives generation to the
declared shape, the composed budget stays within the requested ceiling, and the
fit gate fails closed off the certified proof-stack.

The two positive checks only run on the certified proof-stack (the 77-dist
`dev+lint+vault` CI profile), because `fit_dp_snapshot` refuses everywhere else
by design; the golden gate runs in that env. The fail-closed check is
env-independent and always runs.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pandas as pd
import pytest

from decoy_engine.quality import dp_provenance
from decoy_engine.quality.dp import fit_dp_snapshot
from decoy_engine.quality.dp_provenance import ProvenanceError

pytestmark = pytest.mark.testflight

_EPSILON = 8.0
_DELTA = 1e-5
_UNCERTIFIED = "not the certified DP proof-stack; fit_dp_snapshot fails closed here by design"


def _running_fingerprint() -> str:
    return dp_provenance.compute_lock_fingerprint(dp_provenance.installed_distribution_set())


def _is_certified() -> bool:
    key = (dp_provenance.current_platform(), dp_provenance.current_cpython())
    members = dp_provenance._CERTIFIED_STACKS.get(key)
    return members is not None and _running_fingerprint() in members


def _frame() -> pd.DataFrame:
    # One column per carrier kind (number, flag, text), enough rows for a fit.
    return pd.DataFrame(
        {
            "amount": [10.5, 22.1, 9.9, 100.0, 55.2, 31.4, 18.8, 42.0] * 25,
            "is_active": [True, False, True, True, False, True, False, False] * 25,
            "state": (["CA"] * 5 + ["NY"] * 2 + ["TX"]) * 25,
        }
    )


def _schema() -> dict:
    return {
        "amount": {"kind": "numeric", "carrier": "number", "bounds": [0.0, 150.0]},
        "is_active": {"kind": "categorical", "carrier": "flag"},
        "state": {"kind": "categorical", "carrier": "text"},
    }


def test_dp_fit_emits_marginal_v3_over_only_declared_carriers() -> None:
    if not _is_certified():
        pytest.skip(_UNCERTIFIED)
    art = fit_dp_snapshot(_frame(), _schema(), epsilon=_EPSILON, delta=_DELTA)

    assert art["dp"]["schema"] == "dps-marginal/v3"

    recorded = art["dp"]["column_schema"]
    assert set(recorded) == {"amount", "is_active", "state"}
    assert recorded["amount"] == {"kind": "numeric", "carrier": "number", "bounds": [0.0, 150.0]}
    assert recorded["is_active"]["carrier"] == "flag"
    assert recorded["state"]["carrier"] == "text"

    # The artifact records the proof-stack it was produced on.
    assert art["dp"]["provenance"]["fingerprint"] == _running_fingerprint()

    # The composed loss is real and never exceeds the requested ceiling.
    assert 0.0 < art["dp"]["epsilon_total"] <= _EPSILON
    assert 0.0 < art["dp"]["delta_total"] <= _DELTA


def test_dp_snapshot_drives_generation_to_the_declared_shape(tmp_path) -> None:
    if not _is_certified():
        pytest.skip(_UNCERTIFIED)
    from decoy_engine.generation.synthesize import generate_tables
    from decoy_engine.plan import compile_plan
    from decoy_engine.profile import Profile

    art = fit_dp_snapshot(_frame(), _schema(), epsilon=_EPSILON, delta=_DELTA)
    snap = tmp_path / "dp_snapshot.json"
    snap.write_text(json.dumps(art), encoding="utf-8")

    config = {
        "global_settings": {"seed": 1, "dp": {"epsilon": _EPSILON, "delta": _DELTA}},
        "tables": [
            {
                "name": "customers",
                "row_count": 20,
                "generate_columns": [
                    {"name": "amount", "type": "statistical", "snapshot_file": str(snap)},
                    {"name": "is_active", "type": "statistical", "snapshot_file": str(snap)},
                ],
            }
        ],
    }
    profile = Profile(
        schema_version=1,
        tables=(),
        relationships=(),
        profiled_at=datetime.now(timezone.utc),
        decoy_engine_version="testflight-dp",
    )
    plan = compile_plan(config, profile, decoy_engine_version="testflight-dp")
    out = generate_tables(plan)

    amounts = out["customers"]["amount"].to_pylist()
    flags = out["customers"]["is_active"].to_pylist()
    assert len(amounts) == 20
    assert len(flags) == 20
    # The flag carrier round-trips to real bools, not "1"/"0" strings.
    assert all(isinstance(v, bool) for v in flags)


def test_dp_fit_fails_closed_off_the_certified_stack(monkeypatch) -> None:
    # Env-independent: force an uncertified fingerprint and confirm the gate
    # refuses to release rather than fitting. This is the ship-critical arm and
    # runs on every env, including the certified one.
    monkeypatch.setattr(dp_provenance, "compute_lock_fingerprint", lambda _s: "0" * 64)
    with pytest.raises(ProvenanceError) as exc:
        fit_dp_snapshot(_frame(), _schema(), epsilon=_EPSILON, delta=_DELTA)
    assert exc.value.code == "dp_stack_uncertified"
