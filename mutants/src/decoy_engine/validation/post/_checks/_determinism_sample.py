"""determinism_sample scan (engine-v2 S10): deterministic columns are stable.

For a deterministic-mode column the masking contract is "same source value ->
same masked value". This scan audits that single-run-observable invariant over a
sample (up to `ctx.sample_size` rows): if any source value maps to two different
output values, the column is non-deterministic and the job hard-fails.

NOTE (design choice flagged for Dennis at end-of-sprint): the spec phrased this as
"replay the determinism / cross-run reproducibility." Cross-RUN byte-identity is
not observable from a single run's output (and re-deriving it would re-implement
each strategy and couple the validator to S9). The within-run "same source -> same
masked" invariant is the strongest single-run-auditable form and is what S9's
deterministic path guarantees; cross-run reproducibility is already S9-tested and
S13-gated. If Dennis wants a true re-derive replay, that is a follow-up.
"""

from __future__ import annotations

from decoy_engine.validation.post._scan import ScanContext, ScanOutcome, column_values

_NAME = "determinism_sample"


def run_determinism_sample(ctx: ScanContext) -> ScanOutcome:
    failed = False
    for table_name, table_seed in ctx.plan.seed_envelope.per_table:
        out_table = ctx.outputs.get(table_name)
        src_table = ctx.sources.get(table_name)
        if out_table is None or src_table is None:
            continue
        for col_name, seed in table_seed.per_column:
            if not seed.deterministic:
                continue
            if col_name not in out_table.column_names or col_name not in src_table.column_names:
                continue
            src_vals = column_values(src_table, col_name)
            out_vals = column_values(out_table, col_name)
            if len(src_vals) != len(out_vals):
                continue
            mapping: dict[object, object] = {}
            for source, masked in list(zip(src_vals, out_vals, strict=True))[: ctx.sample_size]:
                if source is None:
                    continue
                if source in mapping and mapping[source] != masked:
                    failed = True
                    break
                mapping[source] = masked
            if failed:
                break
        if failed:
            break
    return ScanOutcome(name=_NAME, failed=failed)
