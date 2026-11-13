"""A release re-pin adds certified rows; it never retires a prior release's rows.

The certified fingerprint hashes the installed distribution set, which includes the
engine's own version string, so every release bump moves it with no proof-critical
library change. A snapshot fit under 0.17.0 records the 0.17.0 rows; replacing them
made every such snapshot fail plan compilation with `dp_snapshot_provenance_uncertified`.
"""

from __future__ import annotations

import pytest

from decoy_engine.plan import compile_plan
from decoy_engine.quality import dp_provenance as prov
from tests.unit.generation.test_generate_dp_contract import (
    _cfg_with_artifact,
    _numeric_dp_artifact,
    _profile,
)

# The two rows v0.17.0 certified (engine dev/CI profile, decoy-cli runtime profile).
_V0_17_0_ROWS = (
    "5a76ea92769d62cb0124d89d24c2ec0e4116828e34e0611d80bf5e36edef1a23",
    "a7b40d54f68488ecbcc253c35a03517eefc456625a9b0240470e765e2bbc4476",
)
# The two rows v0.18.0 certified.
_V0_18_0_ROWS = (
    "31ecf94503eff107631191426072eee757af72a2229ab594d2fc2e8e6bfac7b1",
    "b643894ded2ab84c4a0a9d9032a1de3d2330db89161f231e509024428b7533f3",
)
# The two rows v0.19.0 certified (docs/records/release-r0190-facts.md).
_V0_19_0_ROWS = (
    "7d1c1a2a2be450a59fa17d810663a7bfe5e825204cca99bfe039ba12b6226aed",
    "795732eb0808001d9c4591f3693637187637bb383a87b8caa95b8f2f269f9211",
)
# The engine dev/CI profile on 0.19.0 once pytest-xdist joined `dev` (79 dists; ENGINE-PAR P2).
_XDIST_DEV_ROW = "78af2b5a1bb11c826f1d3918f705dc7edada72a081804e74d55e37938d8300b6"
# The two rows v0.20.0 certified: the xdist lock with the engine at 0.20.0, and the CLI runtime
# profile on engine 0.20.0.
_V0_20_0_ROWS = (
    "176aff0bf8727f2fe64e0bddd4febb487aeaa05daa97547143ec6cc4230200cc",
    "725f80a30807dee263ee9404c05829d4947870b2600ab0a165c0fa7951b91268",
)
# The two rows v0.21.0 certified: the dev/CI lock with the engine at 0.21.0, and the CLI runtime
# profile on engine 0.21.0.
_V0_21_0_ROWS = (
    "4560f601e6f299e4c586ce9d795dbaea8e14ac5dc8b5d0e1310b57913e25eac9",
    "aa038ddd1d2e783042e34c75c313921f19b8e1dc580559cf8d5081ed19ea11e1",
)
# The two rows v0.22.0 certified: the dev/CI lock with the engine at 0.22.0, and the CLI runtime
# profile on engine 0.22.0.
_V0_22_0_ROWS = (
    "fcf186c32c5dd41eba586238760f12c481171e908082dbc31d3295b387be7cf0",
    "d5898d1ff084203f455907d8648e59e0156e320d0e5a12ba1e22aeba8ad6675d",
)
# The two rows this release certifies: the dev/CI lock with the engine at 0.23.0, and the CLI
# runtime profile (with the classifier stack) on engine 0.23.0.
_V0_23_0_ROWS = (
    "bdec8dd8c27cfa533b9cea788c7254b66957977f9670c7004a4dc90028929224",
    "73acf493827ce8dbd72c6da486eb4f30e72a66cf7abb90b0127cef3e14f75eec",
)
_ALL_ROWS = (
    _V0_17_0_ROWS
    + _V0_18_0_ROWS
    + _V0_19_0_ROWS
    + (_XDIST_DEV_ROW,)
    + _V0_20_0_ROWS
    + _V0_21_0_ROWS
    + _V0_22_0_ROWS
    + _V0_23_0_ROWS
)


@pytest.mark.parametrize("fingerprint", _ALL_ROWS)
def test_a_snapshot_recording_a_released_row_compiles(tmp_path, fingerprint: str) -> None:
    artifact = _numeric_dp_artifact(epsilon_total=0.5)
    artifact["dp"]["provenance"]["fingerprint"] = fingerprint
    cfg, _ = _cfg_with_artifact(tmp_path, artifact)
    plan = compile_plan(cfg, _profile(), decoy_engine_version="test")
    assert plan is not None


@pytest.mark.parametrize("fingerprint", _ALL_ROWS)
def test_recorded_provenance_admits_every_released_row(fingerprint: str) -> None:
    (plat, cpython), _ = next(iter(prov._CERTIFIED_STACKS.items()))
    prov.validate_recorded_provenance(
        {"platform": dict(plat._asdict()), "cpython": cpython, "fingerprint": fingerprint}
    )
