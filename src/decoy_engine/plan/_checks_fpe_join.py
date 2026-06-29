"""Plan-compile check for fpe_join_group columns (SP-46).

Added as its own module to mirror the per-strategy check decomposition
pattern established in SP-10c (see _checks_group_key.py, _checks_grouped_series.py).
Keeping it separate avoids growing _checks.py past its allowlisted ceiling.

This module exports exactly one function: ``check_fpe_join_groups``,
which validates that every fpe_join_group declared across the pipeline
config is structurally sound. It is imported by plan/_compile.py alongside
the other check functions from the sibling _checks_*.py modules.

Checks (each a distinct PlanCompileError code):

  fpe_join_group_singleton
      A group has fewer than two members (decision D). A single-member group
      has no cross-column joining effect; the operator must either add a second
      member or remove the key.

  fpe_join_group_non_fpe_member
      A column that carries fpe_join_group uses a strategy other than 'fpe'.
      The group name only affects the FPE tweak; no other strategy reads it.

  fpe_join_group_config_mismatch
      Members of the same group differ in charset, preserve_separators,
      validate_luhn, or checksum. Joining requires byte-identical ciphertext
      for equal plaintexts; a config difference would make that impossible.

  fpe_join_group_namespace_mismatch
      Members of the same group declare different namespaces. The FPE key
      is derived from (job_seed, namespace), so members must share a namespace
      to share a key. Different namespaces produce different keys regardless
      of the shared tweak.

Config-only (no profile, no source data): safe to run in both compile
branches and in ``run_config_only_checks``. Validation never mutates.

Returns a tuple of manifest warning strings (one per active group) that
the caller can include in PlanCompileResult.warnings to create an
auditable opt-in record.
"""

from __future__ import annotations

from typing import Any

from decoy_engine.plan._errors import PlanCompileError

# FPE config keys that must match across all members of the same join group.
# Differing values would cause the same plaintext to encrypt to different
# ciphertexts under the same key and tweak, breaking the join property.
_HOMOGENEITY_KEYS: tuple[str, ...] = (
    "charset",
    "preserve_separators",
    "validate_luhn",
    "checksum",
)


def check_fpe_join_groups(config: dict[str, Any]) -> tuple[str, ...]:
    """Validate fpe_join_group declarations; return manifest warning strings.

    Compile-check ownership table row #21 (SP-46, 2026-06-29).

    Walks all columns across all tables, collecting those that declare
    ``provider_config.fpe_join_group``.  Validates the collected groups
    against the four structural invariants (singleton, non-fpe member,
    config homogeneity, namespace homogeneity).

    For every valid group found, appends a warning string to the returned
    tuple.  The caller (plan/_compile.py) includes these in
    ``PlanCompileResult.warnings`` so the decision to waive per-column
    domain separation appears in the job manifest.

    Args:
        config: Raw pipeline config dict.

    Raises:
        PlanCompileError: On the first structural violation found.

    Returns:
        Manifest warning strings, one per active join group.
    """
    tables = config.get("tables", []) if isinstance(config.get("tables"), list) else []

    # Collect members per group name: list of {table, column, strategy, namespace, cfg}
    groups: dict[str, list[dict[str, Any]]] = {}

    for table_entry in tables:
        if not isinstance(table_entry, dict):
            continue
        table_name = table_entry.get("name", "?")
        for col_entry in table_entry.get("columns", []) or []:
            if not isinstance(col_entry, dict):
                continue
            pc = col_entry.get("provider_config") or {}
            if not isinstance(pc, dict):
                continue
            group_name = pc.get("fpe_join_group")
            if not group_name or not isinstance(group_name, str):
                continue
            col_name = col_entry.get("name", "?")
            groups.setdefault(group_name, []).append(
                {
                    "table": table_name,
                    "column": col_name,
                    "strategy": col_entry.get("strategy"),
                    "namespace": col_entry.get("namespace"),
                    # Homogeneity snapshot with the fpe handler's defaults applied,
                    # so an omitted key compares equal to its explicit default
                    # (e.g. an omitted charset == charset:"digits"); both join.
                    "cfg_snap": {
                        "charset": pc.get("charset", "digits"),
                        "preserve_separators": bool(pc.get("preserve_separators", True)),
                        "validate_luhn": bool(pc.get("validate_luhn", False)),
                        "checksum": pc.get("checksum") or None,
                    },
                }
            )

    manifest_warnings: list[str] = []

    for group_name, members in groups.items():
        # ---- D: singleton hard error -----------------------------------------
        if len(members) < 2:
            m = members[0]
            raise PlanCompileError(
                code="fpe_join_group_singleton",
                path=(f"tables.{m['table']}.columns.{m['column']}.provider_config.fpe_join_group"),
                message=(
                    f"fpe_join_group {group_name!r} has only one member "
                    f"({m['table']}.{m['column']}). A join group requires at "
                    "least two members to have any cross-column effect. Add the "
                    "second member or remove the fpe_join_group key."
                ),
            )

        # ---- non-fpe member --------------------------------------------------
        for m in members:
            if m["strategy"] != "fpe":
                raise PlanCompileError(
                    code="fpe_join_group_non_fpe_member",
                    path=(
                        f"tables.{m['table']}.columns.{m['column']}.provider_config.fpe_join_group"
                    ),
                    message=(
                        f"fpe_join_group {group_name!r}: column "
                        f"{m['table']}.{m['column']} has strategy "
                        f"{m['strategy']!r}, not 'fpe'. Only fpe columns may "
                        "participate in a join group; the group name affects the "
                        "FPE tweak and has no effect on other strategies."
                    ),
                )

        # ---- config homogeneity ----------------------------------------------
        first = members[0]
        for m in members[1:]:
            if m["cfg_snap"] != first["cfg_snap"]:
                diffs = [
                    k for k in _HOMOGENEITY_KEYS if m["cfg_snap"].get(k) != first["cfg_snap"].get(k)
                ]
                raise PlanCompileError(
                    code="fpe_join_group_config_mismatch",
                    path=(f"tables.{m['table']}.columns.{m['column']}.provider_config"),
                    message=(
                        f"fpe_join_group {group_name!r}: column "
                        f"{m['table']}.{m['column']} differs from "
                        f"{first['table']}.{first['column']} in "
                        f"{diffs!r}. All join group members must have identical "
                        "charset, preserve_separators, validate_luhn, and checksum "
                        "so that equal plaintexts encrypt to equal ciphertexts."
                    ),
                )

        # ---- namespace homogeneity -------------------------------------------
        namespaces = [m["namespace"] for m in members]
        if len(set(namespaces)) > 1:
            ns_detail = ", ".join(f"{m['table']}.{m['column']}={m['namespace']!r}" for m in members)
            raise PlanCompileError(
                code="fpe_join_group_namespace_mismatch",
                path=f"tables.*.columns.*.provider_config.fpe_join_group.{group_name}",
                message=(
                    f"fpe_join_group {group_name!r}: members resolve to different "
                    f"namespaces ({ns_detail}). The FPE key is derived from "
                    "(job_seed, namespace); members must share the same namespace "
                    "to share the same key. Declare the same namespace on all "
                    "members, or bind them through a FK relationship."
                ),
            )

        # ---- manifest record for a valid active group ------------------------
        member_labels = ", ".join(f"{m['table']}.{m['column']}" for m in members)
        manifest_warnings.append(
            f"fpe_join_group {group_name!r} active (members: {member_labels}): "
            "cross-column FPE domain separation intentionally waived"
        )

    return tuple(manifest_warnings)
