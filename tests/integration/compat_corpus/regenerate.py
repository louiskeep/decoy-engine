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

import decoy_engine
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


def main() -> None:
    vault_path = HERE / "decoy-vault-v2__synthetic.vault"
    writer = VaultWriter(_SYNTHETIC_JOB_SEED)
    writer.add(_SYNTHETIC_ENTRIES)
    written = writer.write(vault_path)

    expected = {f"{ns}\x1f{masked}": source for ns, masked, source in _SYNTHETIC_ENTRIES}
    manifest = {
        "corpus_version": 1,
        "note": (
            "Cross-version compatibility corpus. Each artifact was written by "
            "produced_by_engine_version and must stay readable by every later "
            "engine within the major (compatibility-contract section 3.1/3.2)."
        ),
        "artifacts": [
            {
                "tag": "decoy-vault/v2",
                "kind": "vault",
                "synthetic": True,
                "produced_by_engine_version": decoy_engine.__version__,
                "path": vault_path.name,
                "job_seed_hex": _SYNTHETIC_JOB_SEED.hex(),
                "entries_written": written,
                # key is "namespace\x1fmasked" -> source (JSON has no tuple keys).
                "expected_mapping": expected,
            }
        ],
    }
    (HERE / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {vault_path.name} ({written} entries) + manifest.json")


if __name__ == "__main__":
    main()
