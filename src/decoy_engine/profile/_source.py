"""profile_source: orchestrate profile generation from a pipeline config.

Reads `config["sources"]`, loads each table via the per-source-type
reader (file/csv or file/parquet in V1), walks each DataFrame via
`walk_dataframe`, and composes a Profile.

The caller (CLI or platform runner) hands in a config dict that has
already been validated through `PipelineConfig.model_validate(...).model_dump()`.
profile_source does NOT re-validate; the choke-point pattern means
validation happens once, upstream.

PK / FK metadata is derived from `config["relationships"]`:
- A column listed in any relationship's `parent.columns` is `declared_pk`.
- A column listed in any relationship's `children[].columns` has
  `is_fk=True` and `fk_target = (parent_table, parent_column)` matched
  positionally for composite FKs.

S3 (Determinism Layer) replaces the RNG seeding pattern; for now,
`seed=None` uses a non-deterministic Random instance and `seed=<int>`
uses `random.Random(seed)`.
"""

from __future__ import annotations

import random
from datetime import datetime
from typing import Any

import pandas as pd

from decoy_engine.profile._types import Profile, Relationship, TableProfile
from decoy_engine.profile._walk import walk_dataframe


def profile_source(
    config: dict[str, Any],
    *,
    sample_rows: int | None = 10_000,
    seed: int | None = None,
) -> Profile:
    """Profile every source declared in `config["sources"]`.

    Args:
        config: a validated pipeline-config dict (must be the output of
            `PipelineConfig.model_validate(...).model_dump()`; profile_source
            does not re-validate).
        sample_rows: passed through to `walk_dataframe`. None means full
            scan; default 10k caps cardinality work on large tables.
        seed: passed through to the RNG that drives reservoir sampling.
            None means non-deterministic sampling (test mode + interactive
            use); explicit int means cross-run reproducibility.

    Returns:
        A frozen `Profile` covering every table declared in
        `config["sources"]`. The `tables` tuple order mirrors
        `config["sources"]` iteration order (Python 3.7+ dict order).
    """
    from decoy_engine import __version__ as engine_version

    rng = random.Random(seed) if seed is not None else random.Random()

    relationships_config = config.get("relationships", []) or []
    pk_cols_per_table = _derive_pk_cols(relationships_config)
    fk_specs_per_table = _derive_fk_specs(relationships_config)
    relationships = tuple(_build_relationships(relationships_config))

    sources = config.get("sources", {}) or {}
    tables: list[TableProfile] = []
    for table_name, source_descriptor in sources.items():
        df = _load_source(source_descriptor)
        tables.append(
            walk_dataframe(
                df,
                table_name=table_name,
                declared_pk_cols=pk_cols_per_table.get(table_name, frozenset()),
                fk_specs=fk_specs_per_table.get(table_name, {}),
                sample_rows=sample_rows,
                rng=rng,
            )
        )

    return Profile(
        schema_version=1,
        tables=tuple(tables),
        relationships=relationships,
        profiled_at=datetime.now(),
        decoy_engine_version=engine_version,
        profile_seed=seed,
    )


# ---------------------------------------------------------------------
# Source-type dispatch
# ---------------------------------------------------------------------


def _load_source(source_descriptor: dict[str, Any]) -> pd.DataFrame:
    """Dispatch on `type` + `format` to the per-source-type reader.

    V1 supports `type: "file" | "s3" | "gcs"`. S14-CLOUD-SRC-S3GCS added the
    cloud variants. The Pydantic adapter has already rejected other types at
    validation time; this NotImplementedError is defensive (e.g., a caller that
    skipped the adapter would land here). SFTP rides S18; DB rides V2.1.
    """
    src_type = source_descriptor.get("type")
    if src_type == "file":
        return _load_file_source(source_descriptor)
    if src_type == "s3":
        return _load_s3_source(source_descriptor)
    if src_type == "gcs":
        return _load_gcs_source(source_descriptor)
    raise NotImplementedError(
        f"profile_source: unsupported source type {src_type!r}. "
        "Supported types: file, s3, gcs (S18 adds sftp; V2.1 adds db)."
    )


def _load_file_source(source_descriptor: dict[str, Any]) -> pd.DataFrame:
    fmt = source_descriptor.get("format")
    path = source_descriptor.get("path")
    if not isinstance(path, str):
        raise ValueError(f"profile_source: file source missing string `path`, got {path!r}")
    if fmt == "csv":
        return pd.read_csv(path)
    if fmt == "parquet":
        return pd.read_parquet(path)
    raise NotImplementedError(
        f"profile_source: unsupported file format {fmt!r}. V1 supports csv | parquet only."
    )


def _load_s3_source(source_descriptor: dict[str, Any]) -> pd.DataFrame:
    """Read an S3 object into a DataFrame. The engine never sees raw secrets:
    `credentials_ref` is opaque and ignored here (the platform resolves it before
    the descriptor reaches the engine, or the SDK walks its default credential
    chain). `endpoint_url` is supported for S3-compatible services (MinIO, R2)
    and moto-S3 in CI.

    Pattern lessons applied from QA Q1, Q3, Q4, Q10 (legacy DB connector):
    - The boto3 client is constructed inside a function call (no shared global).
    - The object is fetched once + held in BytesIO for the pandas reader.
    - No string-interpolated query fragments (boto3's get_object is parameterized).
    - SDK exceptions surface as the SDK's own typed errors; this layer adds
      no `str(e)` cell-value leakage into log messages.
    """
    import io

    import boto3

    fmt = source_descriptor.get("format")
    bucket = source_descriptor.get("bucket")
    key = source_descriptor.get("key")
    if not isinstance(bucket, str) or not bucket:
        raise ValueError(f"profile_source: s3 source missing bucket, got {bucket!r}")
    if not isinstance(key, str) or not key:
        raise ValueError(f"profile_source: s3 source missing key, got {key!r}")

    client_kwargs: dict[str, Any] = {}
    region = source_descriptor.get("region")
    if isinstance(region, str) and region:
        client_kwargs["region_name"] = region
    endpoint_url = source_descriptor.get("endpoint_url")
    if isinstance(endpoint_url, str) and endpoint_url:
        client_kwargs["endpoint_url"] = endpoint_url

    client = boto3.client("s3", **client_kwargs)
    response = client.get_object(Bucket=bucket, Key=key)
    body = io.BytesIO(response["Body"].read())

    if fmt == "csv":
        return pd.read_csv(body)
    if fmt == "parquet":
        return pd.read_parquet(body)
    raise NotImplementedError(
        f"profile_source: unsupported s3 format {fmt!r}. V1 supports csv | parquet only."
    )


def _load_gcs_source(source_descriptor: dict[str, Any]) -> pd.DataFrame:
    """Read a GCS object into a DataFrame. Mirror of `_load_s3_source` with GCS
    semantics. The engine never sees raw secrets; `credentials_ref` is opaque
    and the SDK uses Application Default Credentials when not set.
    """
    import io

    from google.cloud import storage

    fmt = source_descriptor.get("format")
    bucket_name = source_descriptor.get("bucket")
    object_name = source_descriptor.get("object")
    if not isinstance(bucket_name, str) or not bucket_name:
        raise ValueError(f"profile_source: gcs source missing bucket, got {bucket_name!r}")
    if not isinstance(object_name, str) or not object_name:
        raise ValueError(f"profile_source: gcs source missing object, got {object_name!r}")

    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(object_name)
    data = blob.download_as_bytes()
    body = io.BytesIO(data)

    if fmt == "csv":
        return pd.read_csv(body)
    if fmt == "parquet":
        return pd.read_parquet(body)
    raise NotImplementedError(
        f"profile_source: unsupported gcs format {fmt!r}. V1 supports csv | parquet only."
    )


# ---------------------------------------------------------------------
# Relationship metadata derivation
# ---------------------------------------------------------------------


def _derive_pk_cols(
    relationships_config: list[dict[str, Any]],
) -> dict[str, frozenset[str]]:
    """Build {table_name: frozenset(pk_column_names)} from relationships.

    Every column listed in any relationship's `parent.columns` is
    treated as `declared_pk` on its table. For composite PKs, every
    member column carries the flag.
    """
    pk_cols: dict[str, set[str]] = {}
    for rel in relationships_config:
        if not isinstance(rel, dict):
            continue
        parent = rel.get("parent", {})
        if not isinstance(parent, dict):
            continue
        parent_table = parent.get("table")
        parent_columns = parent.get("columns", [])
        if not isinstance(parent_table, str) or not isinstance(parent_columns, list):
            continue
        pk_cols.setdefault(parent_table, set()).update(parent_columns)
    return {t: frozenset(cols) for t, cols in pk_cols.items()}


def _derive_fk_specs(
    relationships_config: list[dict[str, Any]],
) -> dict[str, dict[str, tuple[str, str]]]:
    """Build {child_table: {child_column: (parent_table, parent_column)}}.

    For composite FKs, member columns map positionally: child_columns[i]
    -> (parent_table, parent_columns[i]).
    """
    fk_specs: dict[str, dict[str, tuple[str, str]]] = {}
    for rel in relationships_config:
        if not isinstance(rel, dict):
            continue
        parent = rel.get("parent", {})
        children = rel.get("children", [])
        if not isinstance(parent, dict) or not isinstance(children, list):
            continue
        parent_table = parent.get("table")
        parent_columns = parent.get("columns", [])
        if not isinstance(parent_table, str) or not isinstance(parent_columns, list):
            continue
        for child in children:
            if not isinstance(child, dict):
                continue
            child_table = child.get("table")
            child_columns = child.get("columns", [])
            if not isinstance(child_table, str) or not isinstance(child_columns, list):
                continue
            # Positional mapping; lengths should match (S2's composite_columns_length_match).
            if len(child_columns) != len(parent_columns):
                # Profile-layer Relationship.__post_init__ will catch this when we
                # build the Relationship tuple; here we silently skip to avoid a
                # cascade of confusing errors.
                continue
            table_fk_specs = fk_specs.setdefault(child_table, {})
            for child_col, parent_col in zip(child_columns, parent_columns, strict=True):
                if isinstance(child_col, str) and isinstance(parent_col, str):
                    table_fk_specs[child_col] = (parent_table, parent_col)
    return fk_specs


def _build_relationships(
    relationships_config: list[dict[str, Any]],
) -> list[Relationship]:
    """Convert config relationships into Profile-layer Relationship tuples.

    Each config relationship may have multiple children; each child
    becomes one Relationship instance in the Profile. The Relationship
    dataclass `__post_init__` enforces composite_columns_length_match.
    """
    out: list[Relationship] = []
    for rel in relationships_config:
        if not isinstance(rel, dict):
            continue
        parent = rel.get("parent", {})
        children = rel.get("children", [])
        if not isinstance(parent, dict) or not isinstance(children, list):
            continue
        parent_table = parent.get("table")
        parent_columns = parent.get("columns", [])
        namespace = rel.get("namespace")
        if not isinstance(parent_table, str) or not isinstance(parent_columns, list):
            continue
        for child in children:
            if not isinstance(child, dict):
                continue
            child_table = child.get("table")
            child_columns = child.get("columns", [])
            if not isinstance(child_table, str) or not isinstance(child_columns, list):
                continue
            out.append(
                Relationship(
                    parent_table=parent_table,
                    parent_columns=tuple(parent_columns),
                    child_table=child_table,
                    child_columns=tuple(child_columns),
                    namespace=namespace if isinstance(namespace, str) else None,
                )
            )
    return out
