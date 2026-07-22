"""Cross-version compatibility corpus gate (compatibility-contract section 3.1/3.2).

The corpus locks persisted artifacts written by an earlier engine version. This
test asserts the CURRENT engine still reads them back. A reader regression on a
frozen `/vN` format (e.g. someone changes `decoy-vault/v1` without keeping a v1
reader) turns this red, which is exactly the alarm the contract wants.

Phase behaviour keys off `decoy_engine.RELEASE_PHASE` (the 1.8 switch):
- Pre-GA: the baseline is SYNTHETIC. The round-trip still runs (it proves the
  harness bites), but a synthetic-only corpus is acceptable.
- At GA: the corpus MUST contain at least one real artifact captured from a
  released version; synthetic-only is a failure (test_corpus_has_real_artifacts).

To re-baseline or capture a real artifact, see regenerate.py in this directory.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from decoy_engine.release import is_pre_ga
from decoy_engine.vault import VaultError, load_vault

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "manifest.json"


def _artifacts() -> list[dict]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return manifest["artifacts"]


def _vault_mapping_as_manifest_keys(path: Path, seed: bytes) -> dict[str, str]:
    mapping, _ambiguous = load_vault(path, seed)
    # JSON has no tuple keys; the manifest joins (namespace, masked) with US (\x1f).
    return {f"{ns}\x1f{masked}": source for (ns, masked), source in mapping.items()}


def test_manifest_exists() -> None:
    assert MANIFEST.exists(), (
        f"{MANIFEST} is missing. Run `python {HERE.name}/regenerate.py` to mint the baseline."
    )


@pytest.mark.parametrize("artifact", _artifacts(), ids=lambda a: a["tag"])
def test_corpus_artifact_round_trips(artifact: dict) -> None:
    """Every locked artifact must still read back to its recorded content."""
    if artifact["kind"] == "vault":
        path = HERE / artifact["path"]
        seed = bytes.fromhex(artifact["job_seed_hex"])
        got = _vault_mapping_as_manifest_keys(path, seed)
        assert got == artifact["expected_mapping"], (
            f"{artifact['tag']} written by engine "
            f"{artifact['produced_by_engine_version']} no longer reads back to its "
            f"recorded mapping. A frozen-format reader regressed "
            f"(compatibility-contract section 3.1/3.2)."
        )
    elif artifact["kind"] == "distribution_snapshot":
        # Round-trip through the REAL reader (load_spec), not a re-json.loads,
        # so a regression in the kind dispatch / joint lookup / schema guard
        # is caught. One col_cfg per reader branch (numeric, categorical,
        # conditioned-joint). load_spec no longer reads snapshot_file itself
        # (DPS Scope B, guide 4.7): the compiler reads each path exactly once
        # up front, so this harness reads it the same way via _load_snapshot.
        from decoy_engine.generation.statistical import load_spec
        from decoy_engine.generation.statistical._spec import _load_snapshot

        path = HERE / artifact["path"]
        _digest, snapshot = _load_snapshot(str(path))
        for rec in artifact["expected_specs"]:
            cfg = {**rec["col_cfg"], "snapshot_file": str(path)}
            spec = load_spec(cfg, snapshot=snapshot)
            ctx = (
                f"{artifact['tag']} written by engine "
                f"{artifact['produced_by_engine_version']}: column "
                f"{rec['col_cfg'].get('name')!r} no longer reads back. A frozen "
                f"distribution-snapshot reader regressed (compatibility-contract 3.1/3.2)."
            )
            assert spec.kind == rec["kind"], ctx
            assert spec.dtype == rec["dtype"], ctx
            assert spec.parent_first == rec["parent_first"], ctx
            assert (spec.joint is not None) == rec["has_joint"], ctx
            assert spec.stats, ctx  # the sampler needs non-empty stats
    else:  # pragma: no cover - guards against an unrecorded artifact kind
        pytest.fail(f"no round-trip check implemented for artifact kind {artifact['kind']!r}")


def test_corpus_has_real_artifacts_at_ga() -> None:
    """At GA the corpus must hold real released artifacts, not just the synthetic
    baseline. Pre-GA this is skipped; the synthetic baseline proves the harness."""
    if is_pre_ga():
        pytest.skip("pre-GA: synthetic baseline is sufficient; capture real artifacts at GA")
    real = [a for a in _artifacts() if not a.get("synthetic", False)]
    assert real, (
        "RELEASE_PHASE is 'ga' but the compat corpus contains only synthetic "
        "artifacts. Capture real artifacts from each released /vN via regenerate.py "
        "(set synthetic=False) so this gate actually protects shipped users."
    )


def test_harness_bites_on_corruption(tmp_path: Path) -> None:
    """Meta-test: a tampered vault must fail to read, proving the gate detects a
    real reader/format problem rather than passing vacuously."""
    vaults = [a for a in _artifacts() if a["kind"] == "vault"]
    assert vaults, "expected at least one vault artifact in the corpus"
    art = vaults[0]
    blob = bytearray((HERE / art["path"]).read_bytes())
    blob[-1] ^= 0xFF  # flip the last byte of the ciphertext
    tampered = tmp_path / "tampered.vault"
    tampered.write_bytes(bytes(blob))
    with pytest.raises(VaultError):
        load_vault(tampered, bytes.fromhex(art["job_seed_hex"]))


def test_harness_bites_on_corruption_snapshot(tmp_path: Path) -> None:
    """Meta-test for the snapshot kind: a tampered schema_version must make the
    reader raise, proving the gate bites for this artifact too. `load_spec` no
    longer reads `snapshot_file` itself (DPS Scope B, guide 4.7) -- the read +
    schema guard now lives in `_load_snapshot`, so this exercises that
    function directly rather than routing through `load_spec`."""
    from decoy_engine.generation.statistical import StatisticalSpecError
    from decoy_engine.generation.statistical._spec import _load_snapshot

    snaps = [a for a in _artifacts() if a["kind"] == "distribution_snapshot"]
    assert snaps, "expected at least one distribution_snapshot artifact in the corpus"
    art = snaps[0]
    data = json.loads((HERE / art["path"]).read_text(encoding="utf-8"))
    data["schema_version"] = "distribution-snapshot/v999"  # flip the guarded field
    tampered = tmp_path / "tampered-snapshot.json"
    tampered.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(StatisticalSpecError) as exc:
        _load_snapshot(str(tampered))
    assert exc.value.code == "statistical_snapshot_schema_mismatch"
