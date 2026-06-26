"""Produce the cross-version compatibility corpus baseline.

The corpus locks persisted artifacts written by one engine version and asserts a
later engine can still read them (compatibility-contract section 3.1/3.2). This
script writes the artifacts and a manifest recording how to read each one back.

Run it ONLY to mint a new baseline deliberately:
    python tests/integration/compat_corpus/regenerate.py

Pre-GA there are no released artifacts, so the baseline is SYNTHETIC: it exists
to prove the harness actually catches a reader regression (the test bites). At
GA, capture real artifacts from each released `/vN` here and set synthetic=False;
the test then becomes a blocking gate (see test_compat_corpus.py, which keys off
decoy_engine.RELEASE_PHASE).

The vault is Fernet-encrypted with a random IV, so its bytes are not
reproducible run to run. That is fine: the locked file is committed once and the
test reads THAT file. Re-running this script mints a fresh (equally valid) file;
do it only when intentionally re-baselining.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

import decoy_engine
from decoy_engine.determinism import SEED_PROTOCOL_VERSION
from decoy_engine.generation.statistical import load_spec
from decoy_engine.quality.snapshot import compute_distribution_snapshot
from decoy_engine.vault import VaultWriter

HERE = Path(__file__).resolve().parent

# A fixed synthetic job seed. The determinism layer requires exactly 8 bytes
# (the normalized job-seed envelope). Stored in the manifest so the test can
# decrypt the locked vault.
_SYNTHETIC_JOB_SEED = (0xC0FFEE0BADC0DE01).to_bytes(8, "big")

# (namespace, masked, source) triples: the re-identification rows a vault holds.
_SYNTHETIC_ENTRIES = [
    ("person_identity", "Jordan Avery", "Maria Gonzalez"),
    ("person_identity", "Riley Chen", "David Okafor"),
    ("member_id", "MBR-100001", "843-22-9981"),
    ("member_id", "MBR-100002", "771-04-3325"),
    ("email_pool", "user01@example.net", "real.person@hospital.org"),
]


# A deterministic frame covering every snapshot reader branch: a numeric
# column (salary), a categorical column (state), and a joint contingency
# (state x active) so load_spec's numeric/categorical/conditioned paths are
# all exercised by the round-trip.
def _snapshot_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "state": ["CA", "CA", "NY", "NY", "TX", "TX"] * 5,
            "active": [True, False, True, False, True, False] * 5,
            "salary": [50_000, 60_000, 70_000, 55_000, 65_000, 75_000] * 5,
        }
    )


# The col_cfgs the round-trip test feeds to load_spec (snapshot_file injected
# at test time). One per reader branch; categorical paths set
# allow_real_categories so the privacy gate does not mask a reader regression.
_SNAPSHOT_COL_CFGS = [
    {"name": "salary", "source_column": "salary"},
    {"name": "state", "source_column": "state", "allow_real_categories": True},
    {
        "name": "state_given_active",
        "source_column": "state",
        "condition_on": "active",
        "allow_real_categories": True,
    },
]


def _vault_artifact() -> dict:
    vault_path = HERE / "decoy-vault-v2__synthetic.vault"
    writer = VaultWriter(_SYNTHETIC_JOB_SEED)
    writer.add(_SYNTHETIC_ENTRIES)
    written = writer.write(vault_path)
    expected = {f"{ns}\x1f{masked}": source for ns, masked, source in _SYNTHETIC_ENTRIES}
    print(f"wrote {vault_path.name} ({written} entries)")
    return {
        "tag": "decoy-vault/v2",
        "kind": "vault",
        "synthetic": True,
        "produced_by_engine_version": decoy_engine.__version__,
        "seed_protocol_version": SEED_PROTOCOL_VERSION,
        "path": vault_path.name,
        "job_seed_hex": _SYNTHETIC_JOB_SEED.hex(),
        "entries_written": written,
        # key is "namespace\x1fmasked" -> source (JSON has no tuple keys).
        "expected_mapping": expected,
    }


def _distribution_snapshot_artifact() -> dict:
    snap = compute_distribution_snapshot(_snapshot_frame(), joint_columns=[("state", "active")])
    snap_path = HERE / "distribution-snapshot-v1__synthetic.json"
    snap_path.write_text(json.dumps(snap, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    # Capture the expected StatisticalSpec fields from the REAL reader at mint
    # time, so the round-trip test pins the reader contract, not a re-json.loads.
    expected_specs = []
    for cfg in _SNAPSHOT_COL_CFGS:
        spec = load_spec({**cfg, "snapshot_file": str(snap_path)})
        expected_specs.append(
            {
                "col_cfg": cfg,
                "kind": spec.kind,
                "dtype": spec.dtype,
                "parent_first": spec.parent_first,
                "has_joint": spec.joint is not None,
            }
        )
    print(f"wrote {snap_path.name} ({len(expected_specs)} specs)")
    return {
        "tag": "distribution-snapshot/v1",
        "kind": "distribution_snapshot",
        "synthetic": True,
        "produced_by_engine_version": decoy_engine.__version__,
        # The snapshot schema is protocol-independent (it embeds no
        # SEED_PROTOCOL_VERSION); recorded for attribution symmetry only.
        "seed_protocol_version": SEED_PROTOCOL_VERSION,
        "path": snap_path.name,
        "expected_specs": expected_specs,
    }


def main() -> None:
    # NOTE: masked CSV/Parquet output is intentionally NOT frozen here -- the
    # engine never reads its own masked output back, so there is no decoy-owned
    # reader to regress (freezing it would only re-test pandas/pyarrow).
    # Plan YAML (plan_from_yaml) and Profile JSON (profile_from_json) have real
    # cross-version readers but are in-process artifacts today; add them here
    # once the platform persists plans/profiles to disk for cross-version reuse.
    manifest = {
        "corpus_version": 2,
        "note": (
            "Cross-version compatibility corpus. Each artifact was written by "
            "produced_by_engine_version and must stay readable by every later "
            "engine within the major (compatibility-contract section 3.1/3.2). "
            "At GA, capture a real (synthetic=False) artifact of each read-back "
            "kind, not just the vault."
        ),
        "artifacts": [
            _vault_artifact(),
            _distribution_snapshot_artifact(),
        ],
    }
    (HERE / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print("wrote manifest.json")


if __name__ == "__main__":
    main()
