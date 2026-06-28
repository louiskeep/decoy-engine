"""Runnable sibling of complex_healthcare_claims.yaml.

Writes 10-row source CSVs, runs the engine end-to-end, and prints source vs
masked side by side. Uses REAL strategy/provider names (the teaching YAML uses
illustrative ones); keeps the same lessons: FPE+namespace, deterministic FPE,
coherent composites, from_parent FK inheritance, and the token vault.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pandas as pd
import pyarrow as pa

from decoy_engine.config._pipeline import PipelineConfig
from decoy_engine.execution._pipeline import run_pipeline

HERE = Path(__file__).resolve().parent
IN = HERE / "in"
IN.mkdir(exist_ok=True)

# --------------------------------------------------------------------------
# 1. Source data: 10 rows each. members is the FK parent; claims is a child.
# --------------------------------------------------------------------------
members = pd.DataFrame(
    {
        "member_id": [f"{100000000 + i}" for i in range(10)],
        "ssn": [f"{500 + i:03d}-{10 + i:02d}-{1000 + i:04d}" for i in range(10)],
        "mrn": [f"mrn{i:05d}" for i in range(10)],
        "first_name": ["Ava", "Ben", "Cara", "Dan", "Eve", "Finn", "Gail", "Hugo", "Ivy", "Jack"],
        "last_name": [
            "Reed",
            "Shaw",
            "Tran",
            "Underwood",
            "Vance",
            "West",
            "Xu",
            "York",
            "Zane",
            "Ames",
        ],
        "email": [f"user{i}@example.com" for i in range(10)],
        "city": ["Austin"] * 10,
        "state": ["TX"] * 10,
        "zip": [f"{73301 + i}" for i in range(10)],
        "legacy_account": [f"ACCT-{900 + i}" for i in range(10)],
    }
)
# claims: 10 rows, each referencing a member_id that EXISTS in members (FK valid).
claims = pd.DataFrame(
    {
        "member_id": [f"{100000000 + (i % 10)}" for i in range(10)],
        "claim_id": [f"{700000 + i}" for i in range(10)],
        "billed_amount": [round(100.0 + 13.5 * i, 2) for i in range(10)],
    }
)
providers = pd.DataFrame(
    {
        "npi": [f"{1000000000 + i}" for i in range(10)],
        "pan": [f"4{i:015d}" for i in range(10)],
    }
)

members.to_csv(IN / "members.csv", index=False)
claims.to_csv(IN / "claims.csv", index=False)
providers.to_csv(IN / "providers.csv", index=False)

# --------------------------------------------------------------------------
# 2. Config (runnable variant — real strategies/providers).
# --------------------------------------------------------------------------
config = {
    "version": 1,
    "global_settings": {"seed": 1234567, "post_validation": False},
    "sources": {
        "members": {"type": "file", "format": "csv", "path": "examples/in/members.csv"},
        "claims": {"type": "file", "format": "csv", "path": "examples/in/claims.csv"},
        "providers": {"type": "file", "format": "csv", "path": "examples/in/providers.csv"},
    },
    "targets": {
        "members": {"type": "file", "format": "csv", "path": "examples/out/members.csv"},
        "claims": {"type": "file", "format": "csv", "path": "examples/out/claims.csv"},
        "providers": {"type": "file", "format": "csv", "path": "examples/out/providers.csv"},
        "audit_log": {"type": "file", "format": "csv", "path": "examples/out/audit_log.csv"},
    },
    "namespaces": {
        "member_identity": {"declared_by": ["members.member_id"]},
        "account_vault": {"declared_by": ["members.legacy_account"]},
    },
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
                    "name": "mrn",
                    "strategy": "fpe",
                    "deterministic": True,
                    "namespace": "mrn_space",
                    "provider_config": {"charset": "alphanum"},
                },
                # Coherent composite #1: name + email, explicit group namespace.
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
                # Coherent composite #2: city/state/zip, DERIVED namespace.
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
                # Vaulted one-way pseudonym (hash + namespace + vault).
                {
                    "name": "legacy_account",
                    "strategy": "hash",
                    "namespace": "account_vault",
                    "vault": True,
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
            "row_count": 10,
            "generate_columns": [
                {"name": "event_id", "type": "sequence", "start": 1, "step": 1},
                {"name": "actor", "type": "faker", "faker_type": "user_name"},
                {
                    "name": "action",
                    "type": "categorical",
                    "categories": ["view", "edit", "export", "delete"],
                },
                {"name": "risk", "type": "formula", "formula": "1 if action == 'delete' else 0"},
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

# Validate against the real strict model, then dump to the plain dict the
# engine consumes.
cfg = PipelineConfig.model_validate(config).model_dump()

# --------------------------------------------------------------------------
# 3. Run. sources are caller-loaded Arrow tables keyed by table name.
# --------------------------------------------------------------------------
sources = {
    "members": pa.Table.from_pandas(members),
    "claims": pa.Table.from_pandas(claims),
    "providers": pa.Table.from_pandas(providers),
}

result = run_pipeline(cfg, sources, engine_version="demo-0")

OUT = HERE / "out"
OUT.mkdir(exist_ok=True)


def show(title: str, src: pd.DataFrame | None, masked: pa.Table) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)
    mdf = masked.to_pandas()
    mdf.to_csv(OUT / f"{title}.csv", index=False)
    if src is not None:
        print("--- SOURCE ---")
        print(src.to_string(index=False))
    print("--- MASKED ---")
    print(mdf.to_string(index=False))


outputs = result.outputs
show("members", members, outputs["members"])
show("claims", claims, outputs["claims"])
show("providers", providers, outputs["providers"])
show("audit_log", None, outputs["audit_log"])
print("\nDone. Outputs written to examples/out/")
