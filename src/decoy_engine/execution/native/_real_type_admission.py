"""Native-route admission decided from real types, not profile labels.

The static admission (`_static_route_decision`) reads the profile's coarse dtype
labels. Four things it cannot see decide whether the compiled path matches the
oracle, so `plan_native_route` checks them here once it has the first chunk:

- a hash column's real Arrow type (a dictionary-encoded string, a date32 or a
  decimal128 profiles as `object` or a numeric label and passes the static
  check, but the compiled hash kernel does not take it while the oracle hashes
  it);
- a categorical column's real source type (the compiled index kernel's admitted
  input is `string`; a numeric or dictionary source reroutes to the oracle);
- a bucket_perturb column's real source type (the native operator takes exactly
  `string`; an Arrow `large_string` profiles as `object`, which maps back to
  `string`, so only the real schema can tell the two apart);
- a faker column's provider name (the pool is built as strings, which is right
  only for the providers C1 admits; any other provider, for example a
  date-of-birth provider, produces values the string pool cannot hold). The
  provider's actual output under the caller's registry is checked separately,
  on the resolved pool, by `_chunked_entry._resolve_admitted_pools`.

A rejection reroutes the whole table to the oracle with a coded reason, so the
two routes agree on the input instead of the native one failing after the first
chunk was accepted.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import pyarrow as pa

from decoy_engine.execution.native._phase3_eligibility import C1_PROVIDER_ALLOWLIST
from decoy_engine.execution.native._requirements import hash_config_rejection


def _providers(config: dict[str, Any], table: str) -> dict[str, Any]:
    for table_cfg in config.get("tables") or ():
        if isinstance(table_cfg, dict) and table_cfg.get("name") == table:
            return {
                c["name"]: c.get("provider")
                for c in table_cfg.get("columns") or ()
                if isinstance(c, dict) and "name" in c
            }
    return {}


def categorical_source_type_rejection(column: str, schema: pa.Schema) -> str | None:
    """The coded reason a categorical column's real source type is not the one the
    native operator takes, or None. Slice C1 admits `string` only, the domain the
    full-frame route proves (`_unified_slice_admission`); any other type reroutes to
    the oracle before masking instead of failing inside the kernel."""
    typ = schema.field(column).type
    return None if typ == pa.string() else f"categorical_source_type_not_string:{column}:{typ}"


def bucket_perturb_source_type_rejection(column: str, schema: pa.Schema) -> str | None:
    """The coded reason a bucket_perturb column's real source type is not the one the
    native operator takes, or None. The native domain is exactly `string`, the same
    one the full-frame route proves (`bucket_perturb_config_rejection`); `large_string`
    passes the upstream chunk-safety gate for the oracle route, so it declines here
    to the oracle instead of reaching the kernel."""
    typ = schema.field(column).type
    return None if typ == pa.string() else f"bucket_perturb_source_type_not_string:{column}:{typ}"


def real_type_rejection(
    config: dict[str, Any],
    node_routes: Iterable[Any],
    first_schema: pa.Schema,
    *,
    table: str,
    profile: Any,
) -> str | None:
    """The coded reason an admitted table must run on the oracle, or None."""
    node_routes = tuple(node_routes)
    # Only the hash columns are read from the resident table (`hash_config_rejection`
    # resolves one column's type). `Schema.empty_table` raises for a union or a
    # run-end-encoded string_view field, so building it over the whole schema would turn
    # a carried passthrough column of that type into a crash at admission.
    resident = {
        table: pa.schema(
            [first_schema.field(n.column) for n in node_routes if n.strategy == "hash"]
        ).empty_table()
    }
    providers = _providers(config, table)
    for node in node_routes:
        if node.strategy == "hash":
            reason = hash_config_rejection(node.column, table, profile, resident_sources=resident)
            if reason is not None:
                return reason
        elif node.strategy == "faker" and providers.get(node.column) not in C1_PROVIDER_ALLOWLIST:
            return f"faker_provider_not_native:{node.column}:{providers.get(node.column)}"
        elif node.strategy == "categorical":
            reason = categorical_source_type_rejection(node.column, first_schema)
            if reason is not None:
                return reason
        elif node.strategy == "bucket_perturb":
            reason = bucket_perturb_source_type_rejection(node.column, first_schema)
            if reason is not None:
                return reason
    return None
