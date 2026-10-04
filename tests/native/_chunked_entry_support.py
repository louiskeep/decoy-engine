"""Shared builders for the `run_mask_chunked` production-contract tests.

Kept out of the test modules so the three contract files (values/schema,
side channels, evidence/errors) share one config shape, one key, and one way
of running the oracle and the new entry point on the same chunks.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterable
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.config._pipeline import PipelineConfig
from decoy_engine.keyprovider import SecretKeyProvider

COMPANION_PRESENT = importlib.util.find_spec("decoy_engine_native") is not None
NEEDS_COMPANION = pytest.mark.skipif(
    not COMPANION_PRESENT,
    reason="decoy-engine-native companion not installed; the companion-present CI job covers this",
)

ENGINE_VERSION = "b1-contract"
MASK_KEY = bytes(range(32))
TABLE = "t"


def key_provider(secret: bytes = MASK_KEY) -> SecretKeyProvider:
    return SecretKeyProvider(secret=secret, key_version="v1")


def vault_key() -> bytes:
    """The key a `VaultWriter` needs to match the run's resolved mask key."""
    return key_provider().mask_key()


def make_config(
    columns: list[dict[str, Any]],
    *,
    extra_tables: list[dict[str, Any]] | None = None,
    relationships: list[dict[str, Any]] | None = None,
    global_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # `when` is rejected by ColumnConfig validation but read by the engine from the
    # dumped dict, so pull it out before validating and put it back afterwards.
    whens = {c["name"]: c["when"] for c in columns if "when" in c}
    columns = [{k: v for k, v in c.items() if k != "when"} for c in columns]
    tables = [{"name": TABLE, "columns": columns}, *(extra_tables or [])]
    raw: dict[str, Any] = {
        "version": 1,
        "global_settings": {"seed": 20261001, "post_validation": False, **(global_settings or {})},
        "sources": {
            t["name"]: {"type": "file", "format": "csv", "path": "/dev/null"} for t in tables
        },
        "targets": {
            t["name"]: {"type": "file", "format": "csv", "path": "/dev/null"} for t in tables
        },
        "tables": tables,
    }
    if relationships is not None:
        raw["relationships"] = relationships
    dumped = PipelineConfig.model_validate(raw).model_dump()
    for col in dumped["tables"][0]["columns"]:
        if col["name"] in whens:
            col["when"] = whens[col["name"]]
    return dumped


def redact(name: str, **cfg: Any) -> dict[str, Any]:
    col: dict[str, Any] = {"name": name, "strategy": "redact"}
    if cfg:
        col["provider_config"] = cfg
    return col


def truncate(name: str, length: int = 3) -> dict[str, Any]:
    return {
        "name": name,
        "strategy": "truncate",
        "provider_config": {"length": length, "keep": "head"},
    }


def passthrough(name: str) -> dict[str, Any]:
    return {"name": name, "strategy": "passthrough"}


def hash_col(name: str, namespace: str | None = None) -> dict[str, Any]:
    return {"name": name, "strategy": "hash", "namespace": namespace or f"ns_{name}"}


def categorical(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "strategy": "categorical",
        "namespace": f"ns_{name}",
        "deterministic": True,
        "provider_config": {"categories": ["a", "b", "c"]},
    }


FORCE_ORACLE_VALUE = "2020-03-15"


def force_oracle(name: str) -> dict[str, Any]:
    """A column the chunked dispatcher still vetoes (`date_shift`), so a table that
    carries it runs on the oracle route. Its source column holds `FORCE_ORACLE_VALUE`.
    Callers assert `native_admitted is False` plus `date_shift_not_native_chunked_route`,
    so a forced leg cannot silently become a native run."""
    return {
        "name": name,
        "strategy": "date_shift",
        "namespace": f"ns_{name}",
        "provider_config": {"date_format": "%Y-%m-%d", "min_days": -30, "max_days": 30},
    }


def faker_col(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "strategy": "faker",
        "provider": "person_first_name",
        "deterministic": True,
        "namespace": f"ns_{name}",
        "pool_size": 40,
    }


def split(table: pa.Table, size: int) -> list[pa.Table]:
    """Uneven tail on purpose: callers pick a size that does not divide the rows."""
    return [table.slice(i, size) for i in range(0, table.num_rows, size)]


def column_values(chunks: Iterable[pa.Table], name: str) -> list[Any]:
    return [v for chunk in chunks for v in chunk.column(name).to_pylist()]


def string_source(n: int = 11) -> pa.Table:
    return pa.table(
        {
            "s": pa.array(
                [None if i % 5 == 2 else f"value-{i:04d}" for i in range(n)], pa.string()
            ),
            "p": pa.array([None if i % 4 == 1 else i for i in range(n)], pa.int64()),
        }
    )
