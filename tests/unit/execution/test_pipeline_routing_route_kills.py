"""Mutation kill-tests for the ROUTE + PROBE wiring functions of
`execution/_pipeline_routing_signals.py` (TQ isolated-substrate grade,
branch `tq/isolated-substrate-grade`).

Scope: `resolve_execution_route`, `resolve_probe_recovery`, and the one
`byte_estimate_full_frame_fits` survivor (#26). These three are pure
DELEGATION functions: they compute no routing verdict themselves, they
thread arguments to collaborators (`out_of_core_routing_signals`,
`decide_execution_route`, `resolve_probe_recovery`, `enforce_ooc_disk_
preflight`, the `_probe` / `_mem_estimate` primitives) in an exact,
load-bearing wiring order. Their machine contract is therefore the SET OF
ARGUMENTS each collaborator receives, in the right position -- so each
mutant (a nulled arg, a dropped keyword, a flipped boundary, a changed
default) is killed by spying the collaborator and asserting the exact value
it was handed. This mirrors the `_mem_telemetry` ledger's forwarding grade
("forwards every keyword to the delegate; each mutant drops or nulls one").

These are FAST direct unit tests: they never build a real plan/profile and
never drive the slow integration harness -- every collaborator is replaced
by an in-process spy, so nothing reads a row or spawns a probe subprocess.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution import _pipeline_routing_signals as signals


class _Spy:
    """Records every (args, kwargs) it is called with; returns a preset value."""

    def __init__(self, ret: Any = None) -> None:
        self.ret = ret
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        return self.ret

    @property
    def last(self) -> tuple[tuple[Any, ...], dict[str, Any]]:
        return self.calls[-1]


# ---------------------------------------------------------------------------
# resolve_execution_route -- the routing-signal bundling / delegation seam.
# Every collaborator is spied; the canonical call below threads distinctive,
# non-None, non-default values so a nulled OR dropped argument is observable.
# ---------------------------------------------------------------------------

# Distinctive sentinels for the wiring inputs (identity-checked where useful).
_PROFILE = SimpleNamespace(tag="profile")
_PLAN = SimpleNamespace(tag="plan")
_REGISTRY = SimpleNamespace(tag="registry")
_GRAPH = SimpleNamespace(tag="graph")
_CALLER = {"caller": "sources"}
_KINDS = {"m": "mask"}
_VAULT = SimpleNamespace(tag="vault")
_CONFIG = {"cfg": "value"}

# The 4-tuple out_of_core_routing_signals yields, unpacked into the four
# decide_execution_route size/compat kwargs -- each field distinctive.
_OOCRS_TUPLE = (True, "RC", 123, False)  # compatible, reject_code, rows, exact


def _wire(monkeypatch: pytest.MonkeyPatch, *, decide_route: str) -> dict[str, _Spy]:
    """Install spies for every resolve_execution_route collaborator and invoke
    it once with canonical inputs. `decide_route` sets the route the (spied)
    decider returns, which gates the OOC-D disk-preflight call site."""
    import decoy_engine.execution._pipeline_routing as routing_mod
    import decoy_engine.execution.out_of_core._spill_estimate as spill_mod

    oocrs = _Spy(ret=_OOCRS_TUPLE)
    rffe = _Spy(ret=False)  # full_frame_fits_estimate (non-None: pins the #34 forward)
    rpr = _Spy(ret=False)  # probe_recovers_full_frame
    decide = _Spy(ret=(decide_route, "spy_reason"))
    enforce = _Spy(ret=None)

    monkeypatch.setattr(signals, "out_of_core_routing_signals", oocrs)
    monkeypatch.setattr(signals, "resolve_full_frame_fits_estimate", rffe)
    monkeypatch.setattr(signals, "resolve_probe_recovery", rpr)
    monkeypatch.setattr(routing_mod, "decide_execution_route", decide)
    monkeypatch.setattr(spill_mod, "enforce_ooc_disk_preflight", enforce)

    route, reason = signals.resolve_execution_route(
        _PROFILE,
        plan=_PLAN,
        registry=_REGISTRY,
        graph=_GRAPH,
        caller_sources=_CALLER,
        table_kinds=_KINDS,
        has_mask_table=True,
        has_generate_table=True,
        validators=["V"],
        fidelity_report=True,
        vault_writer=_VAULT,
        execution_mode="auto",
        resolved_substrate="polars",
        out_of_core_threshold_rows=111,
        full_frame_reject_rows=222,
        out_of_core_budget_bytes=333,
        use_byte_estimate_routing=False,
        use_probe_routing=False,
        config=_CONFIG,
        engine_version="v-test",
    )
    assert (route, reason) == (decide_route, "spy_reason")
    return {"oocrs": oocrs, "rffe": rffe, "rpr": rpr, "decide": decide, "enforce": enforce}


def test_route_forwards_signals_call_profile_and_has_mask_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #2: out_of_core_routing_signals gets the real profile, not None.
    # #8: it gets has_mask_table (True here), not None.
    spies = _wire(monkeypatch, decide_route="sequential")
    args, kwargs = spies["oocrs"].last
    assert args[0] is _PROFILE  # #2
    assert kwargs["has_mask_table"] is True  # #8


def test_route_forwards_probe_recovery_ffe_and_engine_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #34: full_frame_fits_estimate is the 7th positional arg to
    # resolve_probe_recovery -- the value rffe returned (False), not None.
    # #36: engine_version keyword is forwarded, not None.
    spies = _wire(monkeypatch, decide_route="sequential")
    args, kwargs = spies["rpr"].last
    assert args[6] is False  # #34
    assert kwargs["engine_version"] == "v-test"  # #36


def test_route_forwards_every_decide_execution_route_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # decide_execution_route is the single decider; resolve_execution_route
    # must hand it EVERY signal unaltered. Each `.get(...)` catches both the
    # nulled-arg mutants and the dropped-keyword mutants in one assertion
    # (a dropped keyword is simply absent -> .get returns None != expected).
    spies = _wire(monkeypatch, decide_route="sequential")
    args, kw = spies["decide"].last
    assert args[0] is _PROFILE  # profile positional
    assert kw.get("has_generate_table") is True  # #48
    assert kw.get("validators") == ["V"]  # #50
    assert kw.get("fidelity_report") is True  # #51
    assert kw.get("vault_writer") is _VAULT  # #52
    assert kw.get("execution_mode") == "auto"  # #53
    assert kw.get("out_of_core_compatible") is True  # #56, #75
    assert kw.get("out_of_core_reject_code") == "RC"  # #57, #76
    assert kw.get("largest_table_rows") == 123  # #58, #77
    assert kw.get("largest_table_rows_exact") is False  # #59, #78
    assert kw.get("out_of_core_threshold_rows") == 111  # #60, #79
    assert kw.get("full_frame_reject_rows") == 222  # #61, #80
    assert kw.get("resolved_substrate") == "polars"  # #74
    assert kw.get("use_byte_estimate_routing") is False  # #81
    assert kw.get("use_probe_routing") is False  # #83


def test_route_runs_disk_preflight_only_on_out_of_core_with_exact_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #85/#86/#87: the `route == "out_of_core"` guard -- the preflight fires
    # exactly when the decider picked out_of_core. #88-#95: the preflight gets
    # (profile positional, graph=, table_kinds=, config=) unaltered/undropped.
    spies = _wire(monkeypatch, decide_route="out_of_core")
    assert len(spies["enforce"].calls) == 1  # #85, #86, #87 (guard fired)
    args, kw = spies["enforce"].last
    assert args and args[0] is _PROFILE  # #88, #92
    assert kw.get("graph") is _GRAPH  # #89, #93
    assert kw.get("table_kinds") is _KINDS  # #90, #94
    assert kw.get("config") is _CONFIG  # #91, #95


def test_route_skips_disk_preflight_off_out_of_core(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Complements #85: on a non-out_of_core route the preflight must NOT fire,
    # pinning the guard's polarity from the other side.
    spies = _wire(monkeypatch, decide_route="full_frame")
    assert spies["enforce"].calls == []


# ---------------------------------------------------------------------------
# resolve_probe_recovery -- the B2 probe-recovery signal. Every _probe /
# _mem_estimate primitive is spied so the two-point probe never actually runs;
# the tests pin the raw-bytes pre-filter boundary and the argument forwarding.
# ---------------------------------------------------------------------------

_PROBE_PROFILE = SimpleNamespace(
    tables=[
        SimpleNamespace(
            name="m",
            row_count=100,
            row_count_exact=True,
            columns=[SimpleNamespace(name="x", distinct_count=5)],
        )
    ]
)
_PROBE_KINDS = {"m": "mask"}


def _probe_caller() -> dict[str, Any]:
    # Resident pa.Table whose column "x" matches the profile column, so
    # _resident_column_arrays yields a NON-EMPTY sample dict (distinguishes the
    # sample-argument mutants: real dict vs None vs {}).
    return {"m": pa.table({"x": [1, 2, 3]})}


def _wire_probe(
    monkeypatch: pytest.MonkeyPatch, *, priceable: int, budget: int, k: float
) -> dict[str, _Spy]:
    """Spy every resolve_probe_recovery primitive; control the pre-filter
    inputs (priceable bytes, budget, plausible-k) exactly."""
    import decoy_engine.execution._mem_estimate as mem_mod
    import decoy_engine.execution._mem_estimate_schema as schema_mod
    import decoy_engine.execution._probe as probe_mod
    import decoy_engine.execution.out_of_core as ooc_mod

    tss = _Spy(ret="SPEC")
    probe_peak = _Spy(ret="RESULT")
    probe_fits = _Spy(ret=True)  # sentinel verdict (distinct from the None early-returns)

    monkeypatch.setattr(
        mem_mod,
        "raw_data_bytes",
        lambda specs: SimpleNamespace(priceable_bytes=priceable, is_priceable=True),
    )
    monkeypatch.setattr(schema_mod, "table_size_spec_from_profile", tss)
    monkeypatch.setattr(probe_mod, "probe_peak_bytes", probe_peak)
    monkeypatch.setattr(probe_mod, "probe_fits", probe_fits)
    monkeypatch.setattr(probe_mod, "uniqueness_saturation_risk", lambda *a, **k: [])
    monkeypatch.setattr(probe_mod, "MIN_PLAUSIBLE_K_FULL_FRAME", k)
    monkeypatch.setattr(ooc_mod, "resolve_budget", lambda b: SimpleNamespace(budget_bytes=budget))

    return {"tss": tss, "probe_peak": probe_peak, "probe_fits": probe_fits}


def test_probe_forwards_resident_sample_to_spec_builder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #20/#22/#23/#27: the per-table spec is built with the RESIDENT column
    # sample as an explicit keyword. All four mutants degrade that sample to
    # None / absent / {} -- so the killing invariant is "sample keyword present
    # AND non-empty" for the resident mask table.
    spies = _wire_probe(monkeypatch, priceable=10, budget=1000, k=2.0)
    signals.resolve_probe_recovery(
        True,
        True,
        _PROBE_PROFILE,
        _probe_caller(),
        _PROBE_KINDS,
        333,
        False,
        config=_CONFIG,
        engine_version="v-test",
    )
    assert spies["tss"].calls, "spec builder was not reached"
    _args, kwargs = spies["tss"].last
    assert "sample" in kwargs  # #22 (dropped keyword)
    assert kwargs["sample"] and len(kwargs["sample"]) > 0  # #20 (None), #23/#27 ({})


def test_probe_forwards_caller_sources_and_engine_version_to_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #48: probe_peak_bytes gets caller_sources as its 2nd positional, not None.
    # #55/#64: engine_version keyword is forwarded (nulled #55 / dropped #64).
    caller = _probe_caller()
    spies = _wire_probe(monkeypatch, priceable=10, budget=1000, k=2.0)
    signals.resolve_probe_recovery(
        True,
        True,
        _PROBE_PROFILE,
        caller,
        _PROBE_KINDS,
        333,
        False,
        config=_CONFIG,
        engine_version="v-test",
    )
    assert spies["probe_peak"].calls, "probe_peak_bytes was not reached"
    args, kwargs = spies["probe_peak"].last
    assert args[1] is caller  # #48
    assert kwargs.get("engine_version") == "v-test"  # #55, #64


def test_probe_forwards_default_error_band_to_probe_fits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #1: the error_band DEFAULT is 0.30 (mutant makes it 1.3).
    # #70: probe_fits gets error_band as an explicit keyword (mutant drops it).
    # Calling WITHOUT error_band pins both: the value forwarded must be 0.30.
    spies = _wire_probe(monkeypatch, priceable=10, budget=1000, k=2.0)
    signals.resolve_probe_recovery(
        True,
        True,
        _PROBE_PROFILE,
        _probe_caller(),
        _PROBE_KINDS,
        333,
        False,
        config=_CONFIG,
        engine_version="v-test",
    )
    assert spies["probe_fits"].calls, "probe_fits was not reached"
    _args, kwargs = spies["probe_fits"].last
    assert kwargs.get("error_band") == pytest.approx(0.30)  # #1, #70


def test_probe_prefilter_boundary_is_strictly_greater_than(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #31: the "clearly busts the budget" skip is `raw * k > budget` (strict).
    # At exact equality (50 * 2.0 == 100) the job is NOT skipped -- orig runs
    # the probe and returns its verdict; the `>=` mutant skips and returns None.
    spies = _wire_probe(monkeypatch, priceable=50, budget=100, k=2.0)
    result = signals.resolve_probe_recovery(
        True,
        True,
        _PROBE_PROFILE,
        _probe_caller(),
        _PROBE_KINDS,
        333,
        False,
        config=_CONFIG,
        engine_version="v-test",
    )
    assert result is True  # #31 (mutant returns None by skipping the probe)
    assert spies["probe_fits"].calls, "boundary should proceed to the probe"


# ---------------------------------------------------------------------------
# byte_estimate_full_frame_fits -- the B1b byte-estimate signal (survivor #26).
# ---------------------------------------------------------------------------


def test_byte_estimate_forwards_error_band_to_fits(monkeypatch: pytest.MonkeyPatch) -> None:
    # #26: `fits(...)` gets error_band as an explicit keyword (mutant drops it,
    # silently reverting to fits's own default). Passing a NON-default band
    # (0.5) makes the drop observable: fits must receive exactly 0.5.
    import decoy_engine.execution._mem_estimate as mem_mod
    import decoy_engine.execution._mem_estimate_schema as schema_mod

    fits_spy = _Spy(ret=True)
    monkeypatch.setattr(mem_mod, "fits", fits_spy)
    monkeypatch.setattr(schema_mod, "table_size_spec_from_profile", _Spy(ret="SPEC"))

    profile = SimpleNamespace(tables=[SimpleNamespace(name="m", columns=[])])
    signals.byte_estimate_full_frame_fits(
        profile,
        caller_sources={},
        table_kinds={"m": "mask"},
        budget_bytes=1000,
        error_band=0.5,
    )
    assert fits_spy.calls, "fits was not reached"
    _args, kwargs = fits_spy.last
    assert kwargs.get("error_band") == pytest.approx(0.5)  # #26
