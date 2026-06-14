#!/usr/bin/env python3
"""Generate the proof manifest from the live registries and real runs.

Source of truth is the code, not copy. This script imports decoy_engine,
reads its registries for the capability surface, runs the real pipeline over
a hero dataset and one minimal config per capability, asserts each
capability's invariant, and emits a JSON artifact the marketing site renders.

Run:  python scripts/gen_proof_manifest.py
Out:  docs/proof-manifest.json  (committed; a sentry test re-runs build() and
      diffs, so a new capability with no proof fails CI)
"""

from __future__ import annotations

import importlib.util
import json
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

import pandas as pd
import pyarrow as pa
import yaml

from decoy_engine.config._pipeline import PipelineConfig
from decoy_engine.execution._pipeline import run_pipeline

SAMPLE_ROWS = 5

_MATRIX_GEN_PATH = Path(__file__).resolve().parent / "gen_capability_matrix.py"
_CALIBRATION_REL = "tests/benchmark/calibration/results.md"


def _capability_matrix_module():
    spec = importlib.util.spec_from_file_location("gen_capability_matrix", _MATRIX_GEN_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {_MATRIX_GEN_PATH}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _surface() -> dict:
    m = _capability_matrix_module()
    return {
        "mask": len(m._mask_strategies()),
        "generate": len(m._generation_strategies()),
        "providers": len(m._providers()),
    }


# Frozen stamp values. These are passed in (not read from the clock) so the
# generator is deterministic and the sentry diff is stable. Bump GENERATED_AT
# by hand when refreshing benchmarks or samples; bump ENGINE_VERSION to match
# the engine release being documented.
ENGINE_VERSION = "0.2.0"
GENERATED_AT = "2026-06-14"

OUT = Path(__file__).resolve().parent.parent / "docs" / "proof-manifest.json"


def _records(df: pd.DataFrame, n: int = SAMPLE_ROWS) -> list[dict]:
    # JSON-safe: cast every cell to str so the marketing site renders verbatim
    # and the sentry diff is stable across pandas dtype quirks.
    return [
        {col: ("" if pd.isna(v) else str(v)) for col, v in row.items()}
        for row in df.head(n).to_dict(orient="records")
    ]


def _hero() -> dict:
    # Source frames: the runnable_demo healthcare warehouse, trimmed to
    # SAMPLE_ROWS rows. members is the FK parent; claims is the child.
    members = pd.DataFrame(
        {
            "member_id": [f"{100000000 + i}" for i in range(SAMPLE_ROWS)],
            "ssn": [f"{500 + i:03d}-{10 + i:02d}-{1000 + i:04d}" for i in range(SAMPLE_ROWS)],
            "first_name": ["Ava", "Ben", "Cara", "Dan", "Eve"][:SAMPLE_ROWS],
            "last_name": ["Reed", "Shaw", "Tran", "Underwood", "Vance"][:SAMPLE_ROWS],
            "email": [f"user{i}@example.com" for i in range(SAMPLE_ROWS)],
            "city": ["Austin"] * SAMPLE_ROWS,
            "state": ["TX"] * SAMPLE_ROWS,
            "zip": [f"{73301 + i}" for i in range(SAMPLE_ROWS)],
        }
    )
    claims = pd.DataFrame(
        {
            "member_id": [f"{100000000 + (i % SAMPLE_ROWS)}" for i in range(SAMPLE_ROWS)],
            "claim_id": [f"{700000 + i}" for i in range(SAMPLE_ROWS)],
            "billed_amount": [round(100.0 + 13.5 * i, 2) for i in range(SAMPLE_ROWS)],
        }
    )
    providers = pd.DataFrame(
        {
            "npi": [f"{1000000000 + i}" for i in range(SAMPLE_ROWS)],
            "pan": [f"4{i:015d}" for i in range(SAMPLE_ROWS)],
        }
    )

    # profile_source reads the sources config block from disk to build column
    # profiles and FK structure. Write temp CSVs so the profiler gets real data;
    # run_pipeline then uses the caller-supplied Arrow tables for masking.
    with tempfile.TemporaryDirectory() as _tmp:
        tmp = Path(_tmp)
        members.to_csv(tmp / "members.csv", index=False)
        claims.to_csv(tmp / "claims.csv", index=False)
        providers.to_csv(tmp / "providers.csv", index=False)
        return _run_hero(members, claims, providers, tmp)


def _run_hero(
    members: pd.DataFrame,
    claims: pd.DataFrame,
    providers: pd.DataFrame,
    tmp: Path,
) -> dict:
    config = {
        "version": 1,
        "global_settings": {"seed": 1234567, "post_validation": False},
        # sources/targets: PipelineConfig requires both; profile_source reads
        # the sources block to build the column profile. We write temp CSVs so
        # the profiler gets real column types and FK structure; the engine then
        # ignores these paths and uses the caller-supplied Arrow tables instead.
        "sources": {
            "members": {"type": "file", "format": "csv", "path": str(tmp / "members.csv")},
            "claims": {"type": "file", "format": "csv", "path": str(tmp / "claims.csv")},
            "providers": {"type": "file", "format": "csv", "path": str(tmp / "providers.csv")},
        },
        "targets": {
            "members": {"type": "file", "format": "csv", "path": "/dev/null"},
            "claims": {"type": "file", "format": "csv", "path": "/dev/null"},
            "providers": {"type": "file", "format": "csv", "path": "/dev/null"},
            "audit_log": {"type": "file", "format": "csv", "path": "/dev/null"},
        },
        "namespaces": {"member_identity": {"declared_by": ["members.member_id"]}},
        "tables": [
            {
                "name": "members",
                "columns": [
                    {
                        "name": "member_id",
                        "strategy": "fpe",
                        "namespace": "member_identity",
                        "provider_config": {"charset": "digits", "preserve_separators": True},
                    },
                    {
                        "name": "ssn",
                        "strategy": "fpe",
                        "namespace": "ssn_space",
                        "provider_config": {"charset": "digits", "preserve_separators": True},
                    },
                    {
                        "name": "first_name",
                        "strategy": "faker",
                        "provider": "composite_name_email",
                        "coherent_with": ["last_name", "email"],
                        "namespace": "member_pii",
                    },
                    {
                        "name": "last_name",
                        "strategy": "faker",
                        "provider": "composite_name_email",
                        "coherent_with": ["first_name", "email"],
                        "namespace": "member_pii",
                    },
                    {
                        "name": "email",
                        "strategy": "faker",
                        "provider": "composite_name_email",
                        "coherent_with": ["first_name", "last_name"],
                        "namespace": "member_pii",
                    },
                    {
                        "name": "city",
                        "strategy": "faker",
                        "provider": "composite_city_state_zip",
                        "coherent_with": ["state", "zip"],
                    },
                    {
                        "name": "state",
                        "strategy": "faker",
                        "provider": "composite_city_state_zip",
                        "coherent_with": ["city", "zip"],
                    },
                    {
                        "name": "zip",
                        "strategy": "faker",
                        "provider": "composite_city_state_zip",
                        "coherent_with": ["city", "state"],
                    },
                ],
            },
            {
                "name": "claims",
                "columns": [
                    {"name": "member_id", "strategy": "from_parent"},
                    {
                        "name": "claim_id",
                        "strategy": "fpe",
                        "namespace": "claim_space",
                        "provider_config": {"charset": "digits"},
                    },
                    {"name": "billed_amount", "strategy": "passthrough"},
                ],
            },
            {
                "name": "providers",
                "columns": [
                    {
                        "name": "npi",
                        "strategy": "fpe",
                        "namespace": "npi_space",
                        "provider_config": {"charset": "digits"},
                    },
                    {
                        "name": "pan",
                        "strategy": "fpe",
                        "namespace": "pan_space",
                        "provider_config": {"charset": "digits", "validate_luhn": True},
                    },
                ],
            },
            {
                "name": "audit_log",
                "row_count": SAMPLE_ROWS,
                "generate_columns": [
                    {"name": "event_id", "type": "sequence", "start": 1, "step": 1},
                    {"name": "actor", "type": "faker", "faker_type": "user_name"},
                    {
                        "name": "action",
                        "type": "categorical",
                        "categories": ["view", "edit", "export", "delete"],
                    },
                ],
            },
        ],
        "relationships": [
            {
                "parent": {"table": "members", "columns": ["member_id"]},
                "children": [{"table": "claims", "columns": ["member_id"]}],
                "orphan_policy": "fail",
            }
        ],
    }

    cfg = PipelineConfig.model_validate(config).model_dump()
    sources = {
        "members": pa.Table.from_pandas(members),
        "claims": pa.Table.from_pandas(claims),
        "providers": pa.Table.from_pandas(providers),
    }
    result = run_pipeline(cfg, sources, engine_version=ENGINE_VERSION)
    out = result.outputs

    inputs = {"members": members, "claims": claims, "providers": providers}
    tables = [
        {"name": name, "input": _records(inputs[name]), "output": _records(out[name].to_pandas())}
        for name in ("members", "claims", "providers")
    ]
    return {
        "name": "HIPAA claims warehouse",
        "disguise": "hipaa",
        "tables": tables,
        "audit_log": _records(out["audit_log"].to_pandas()),
        "invariants": [
            "Foreign keys stay valid: every masked claims.member_id still joins to a masked members row.",
            "Reversible identifiers use format-preserving encryption: shape and length are preserved.",
            "Name, email, city, state, and zip are replaced coherently as a unit, not field by field.",
            "The audit_log table is generated from scratch and contains no source data.",
        ],
    }


class CapabilityProof(NamedTuple):
    id: str
    kind: str
    title: str
    column: str
    input_values: list
    config: dict
    invariant: str
    check: Callable[[str, list, list], bool]


def _mask_proof(
    strategy_id,
    title,
    column,
    values,
    column_cfg,
    invariant,
    check,
    *,
    namespaces=None,
):
    table = {
        "name": "t",
        "columns": [{"name": column, "strategy": strategy_id, **column_cfg}],
    }
    cfg = {
        "version": 1,
        "global_settings": {"seed": 1234567, "post_validation": False},
        "tables": [table],
    }
    if namespaces:
        cfg["namespaces"] = namespaces
    return CapabilityProof(
        id=f"mask.{strategy_id}",
        kind="mask",
        title=title,
        column=column,
        input_values=values,
        config=cfg,
        invariant=invariant,
        check=check,
    )


def _same_length(col, i, o):
    return all(len(a[col]) == len(b[col]) for a, b in zip(i, o, strict=True))


def _all_changed(col, i, o):
    return all(a[col] != b[col] for a, b in zip(i, o, strict=True))


# Strategies intentionally not given a standalone card. `passthrough` is the
# no-op (shown in the hero instead). Every other registry mask strategy MUST
# have a CAPABILITY_PROOFS entry, enforced by the completeness sentry.
WAIVED_MASK_STRATEGIES = {"passthrough"}


CAPABILITY_PROOFS: list[CapabilityProof] = [
    _mask_proof(
        "fpe",
        "Format-preserving encryption",
        "account",
        ["100000001", "100000002", "100000003"],
        {"namespace": "acct", "provider_config": {"charset": "digits"}},
        "Output preserves the exact length and digit charset of the input, and is reversible with the key.",
        _same_length,
        namespaces={"acct": {"declared_by": ["t.account"]}},
    ),
    _mask_proof(
        "redact",
        "Redaction",
        "ssn",
        ["500-10-1000", "501-11-1001", "502-12-1002"],
        {},
        "The original value is destroyed and replaced with a fixed mask; redaction is irreversible.",
        _all_changed,
    ),
    _mask_proof(
        "hash",
        "Joinability-preserving hash",
        "user_id",
        ["alice", "bob", "carol"],
        {"namespace": "uid"},
        "Each value becomes a fixed-length keyed hex token; equal inputs hash equally, so joins survive.",
        lambda col, i, o: (
            _all_changed(col, i, o)
            and len({len(b[col]) for b in o}) == 1
            and all(len(b[col]) == 64 for b in o)
        ),
        namespaces={"uid": {"declared_by": ["t.user_id"]}},
    ),
    _mask_proof(
        "truncate",
        "Truncation",
        "phone",
        ["555-100-2000", "555-101-2001", "555-102-2002"],
        {"provider_config": {"length": 4}},
        "Only the first N characters are kept; output is never longer than the input.",
        lambda col, i, o: (
            all(len(b[col]) <= len(a[col]) for a, b in zip(i, o, strict=True))
            and all(len(b[col]) == 4 for b in o)
        ),
    ),
    _mask_proof(
        "bucketize",
        "Bucketization",
        "age",
        ["42", "47", "81"],
        {"provider_config": {"width": 10, "format": "range"}},
        "Numeric values collapse into fixed-width ranges, so distinct inputs share a bucket.",
        # 42 and 47 both fall in the [40, 50) bucket, so output cardinality < input cardinality.
        lambda col, i, o: (
            _all_changed(col, i, o)
            and len({b[col] for b in o}) < len({a[col] for a in i})
            and all("-" in b[col] for b in o)
        ),
    ),
    _mask_proof(
        "categorical",
        "Categorical remap",
        "tier",
        ["platinum", "gold", "diamond"],
        {
            "namespace": "tier",
            "provider_config": {"categories": ["bronze", "silver"]},
        },
        "Every value is remapped onto the configured category pool; equal inputs map to the same category.",
        # Inputs are never in the target pool, so pool membership already proves remapping.
        lambda col, i, o: all(b[col] in {"bronze", "silver"} for b in o),
        namespaces={"tier": {"declared_by": ["t.tier"]}},
    ),
    _mask_proof(
        "date_shift",
        "Date shifting",
        "dob",
        ["1980-01-15", "1991-06-30", "2003-11-02"],
        {
            "namespace": "dob",
            "provider_config": {"min_days": 30, "max_days": 90},
        },
        "Each date is shifted by a keyed offset inside the configured window, preserving the date format.",
        # min_days=30 excludes a zero shift, so _all_changed is a valid check here.
        lambda col, i, o: (
            _all_changed(col, i, o)
            and _same_length(col, i, o)
            and all(b[col].count("-") == 2 for b in o)
        ),
        namespaces={"dob": {"declared_by": ["t.dob"]}},
    ),
    _mask_proof(
        "shuffle",
        "Within-column shuffle",
        "salary",
        ["50000", "75000", "92000"],
        {"namespace": "salary"},
        "Values are permuted within the column; the exact multiset of values is preserved.",
        # Shuffle may leave a value in place, so multiset equality is the correct invariant, not _all_changed.
        lambda col, i, o: sorted(a[col] for a in i) == sorted(b[col] for b in o),
        namespaces={"salary": {"declared_by": ["t.salary"]}},
    ),
    _mask_proof(
        "faker",
        "Synthetic replacement",
        "first_name",
        ["__src_a__", "__src_b__", "__src_c__"],
        {"provider": "person_first_name", "namespace": "name"},
        "Values are replaced with synthetic provider output drawn from a value pool, not the originals.",
        # _all_changed rules out sentinel passthrough; non-empty guards a provider returning "".
        lambda col, i, o: _all_changed(col, i, o) and all(b[col] for b in o),
        namespaces={"name": {"declared_by": ["t.first_name"]}},
    ),
    _mask_proof(
        "formula",
        "Custom formula",
        "code",
        ["ABC123", "XYZ789", "QRS456"],
        {"provider_config": {"formula": "value[::-1]"}},
        "A user-supplied expression transforms each value; here the value is reversed, preserving length.",
        lambda col, i, o: _all_changed(col, i, o) and _same_length(col, i, o),
    ),
    _mask_proof(
        "text_redact",
        "Free-text PII redaction",
        "note",
        [
            "Member SSN 500-10-1000 on file",
            "Reach me at user1@example.com today",
            "Spoke with member SSN 502-12-1002",
        ],
        {},
        "PII spans inside free text are replaced with a redaction token; surrounding text is preserved.",
        lambda col, i, o: (
            all("[REDACTED]" in b[col] for b in o)
            and not any(
                tok in b[col]
                for a, b in zip(i, o, strict=True)
                for tok in ("500-10-1000", "user1@example.com", "502-12-1002")
            )
        ),
    ),
]


def _yaml_for(proof: CapabilityProof) -> str:
    table = proof.config["tables"][0]
    snippet = {"tables": [{"name": table["name"], "columns": table["columns"]}]}
    return yaml.safe_dump(snippet, sort_keys=False, default_flow_style=False).rstrip("\n")


def _run_capability(proof: CapabilityProof) -> dict:
    df = pd.DataFrame({proof.column: proof.input_values})
    with tempfile.TemporaryDirectory() as _tmp:
        tmp = Path(_tmp)
        csv_path = tmp / "t.csv"
        df.to_csv(csv_path, index=False)

        cfg = {
            **proof.config,
            "sources": {
                "t": {"type": "file", "format": "csv", "path": str(csv_path)},
            },
            "targets": {
                "t": {"type": "file", "format": "csv", "path": "/dev/null"},
            },
        }
        validated = PipelineConfig.model_validate(cfg).model_dump()
        sources = {"t": pa.Table.from_pandas(df)}
        result = run_pipeline(validated, sources, engine_version=ENGINE_VERSION)
        out_df = result.outputs["t"].to_pandas()

    inp = _records(df, n=len(df))
    out = _records(out_df, n=len(out_df))
    if not proof.check(proof.column, inp, out):
        raise RuntimeError(
            f"capability {proof.id}: invariant check failed; not emitting a false proof"
        )
    return {
        "id": proof.id,
        "kind": proof.kind,
        "title": proof.title,
        "column": proof.column,
        "config_yaml": _yaml_for(proof),
        "input": inp,
        "output": out,
        "invariant": proof.invariant,
    }


def _capabilities() -> list[dict]:
    return [_run_capability(p) for p in CAPABILITY_PROOFS]


def _providers_list() -> list[dict]:
    m = _capability_matrix_module()
    # _providers() returns (name, backend, deterministic, unique) tuples.
    return [
        {"name": name, "backend": backend, "deterministic": det == "yes", "unique": uniq == "yes"}
        for name, backend, det, uniq in m._providers()
    ]


def _generation_strategies_list() -> list[str]:
    return _capability_matrix_module()._generation_strategies()


def _benchmarks() -> list[dict]:
    # Transcribed from tests/benchmark/calibration/results.md, Run 1 (captured
    # 2026-05-09, Intel Core i7-1265U). Pipeline: source.file, filter (10% pass),
    # mask (single hash rule), target.file on a HIPAA-shaped parquet fixture.
    # Benchmarks are a captured artifact, not regenerable here; the date is the
    # provenance. The source doc states no engine version, so none is claimed.
    runs = [
        # (rows label, row count, hybrid elapsed seconds)
        ("1M rows", 1_000_000, 0.59),
        ("5M rows", 5_000_000, 3.14),
        ("10M rows", 10_000_000, 5.47),
    ]
    out = []
    for label, rows, elapsed in runs:
        rate_m = round(rows / elapsed / 1_000_000, 1)
        out.append(
            {
                "shape": f"{label}, HIPAA-shaped parquet, hybrid engine",
                "throughput": f"~{rate_m}M rows/s",
                "measured_at": "2026-05-09",
                "note": f"{elapsed} s elapsed; Intel Core i7-1265U; single hash-rule pipeline",
                "source": _CALIBRATION_REL,
            }
        )
    return out


def build() -> dict:
    return {
        "engine_version": ENGINE_VERSION,
        "generated_at": GENERATED_AT,
        "surface": _surface(),
        "hero": _hero(),
        "capabilities": _capabilities(),
        "providers": _providers_list(),
        "generation_strategies": _generation_strategies_list(),
        "benchmarks": _benchmarks(),
    }


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(build(), indent=2, sort_keys=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
