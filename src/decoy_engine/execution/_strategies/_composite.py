"""composite routing for the execution adapter (engine-v2 S9).

A composite WorkNode (kind == "composite") writes multiple coherent output
columns in ONE pass via the S8 `CompositeGenerator.generate_bundle` (S9 spec
§6.2). The registry binding for a composite is a `CompositeAdapter` whose
`generate()` raises `composite_requires_bundle_path`; the actual generator is
built here via the factory + the whole-tuple namespace the S8 step-2.5
auto-binding produced (resolved through `namespace_registry.for_column(table,
sorted(output_columns))`). The deterministic key is the first (sorted) output
column's source values; non-deterministic mode ignores the source.
"""

from __future__ import annotations

import pandas as pd

from decoy_engine.execution._adapter import StrategyContext
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._runner import WorkNode
from decoy_engine.generation.composite import (
    CompositeGenerator,
    composite_address,
    composite_city_state_zip,
    composite_custom,
    composite_name_email,
    composite_person,
    composite_provider,
)
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.providers_v2._adapter import ProviderSpec


class CompositeHandler:
    """Runs a composite node's generate_bundle and writes its output columns."""

    name: str = "<composite>"

    def run(
        self,
        df: pd.DataFrame,
        node: WorkNode,
        ctx: StrategyContext,
    ) -> tuple[pd.DataFrame, list[QualityWarning]]:
        coherent_namespace = ctx.namespace_registry.for_column(node.table, node.columns)
        if coherent_namespace is None:
            raise ExecutionError(
                code="composite_namespace_unresolved",
                message=(
                    f"composite group ({node.table!r}, {node.columns}) has no namespace "
                    "binding; build_namespace_registry should auto-bind the whole tuple."
                ),
            )
        col_seed = node.plan_slice
        if not isinstance(col_seed, ColumnSeed):
            raise ExecutionError(
                code="unsupported_strategy",
                message=f"composite node {node.columns} has no ColumnSeed plan slice.",
            )

        generator: CompositeGenerator
        if node.provider == "composite_name_email":
            generator = composite_name_email(
                coherent_namespace=coherent_namespace, registry=ctx.registry
            )
        elif node.provider == "composite_city_state_zip":
            generator = composite_city_state_zip(coherent_namespace=coherent_namespace)
        elif node.provider == "composite_person":
            # MG-4 (2026-05-31): 4-output coherent person bundle.
            generator = composite_person(
                coherent_namespace=coherent_namespace, registry=ctx.registry
            )
        elif node.provider == "composite_address":
            # MG-4 (2026-05-31): 4-output coherent address bundle.
            generator = composite_address(
                coherent_namespace=coherent_namespace, registry=ctx.registry
            )
        elif node.provider == "composite_provider":
            # MG-4 (2026-05-31): 3-output coherent provider bundle
            # (NPI + provider_name + practice_address).
            generator = composite_provider(
                coherent_namespace=coherent_namespace, registry=ctx.registry
            )
        elif node.provider == "composite_custom":
            # MG-4 (2026-05-31): generic 1-4 column bundle; the bundle
            # declaration rides in provider_config["bundle"] as a list
            # of {column, provider, optional config} dicts. Plan-compile
            # has already validated the wiring; we trust the shape here
            # and let CompositeCustom.__init__ raise on a malformed
            # bundle (the typed CompositeError surfaces to the runner).
            cfg = dict(col_seed.provider_config)
            # QA-3 F10 (2026-05-31): renamed from `bundle` to
            # `bundle_decl` to disambiguate from the generated
            # output bundle below (line 121). The two values are
            # different shapes (declaration list vs generated dict)
            # and the shadow was a readability hazard.
            bundle_decl = cfg.get("bundle") or []
            if not isinstance(bundle_decl, list):
                raise ExecutionError(
                    code="composite_custom_bundle_shape",
                    message=(
                        f"composite_custom on {node.columns}: provider_config['bundle'] "
                        f"must be a list, got {type(bundle_decl).__name__}."
                    ),
                )
            generator = composite_custom(
                coherent_namespace=coherent_namespace,
                bundle=bundle_decl,
                registry=ctx.registry,
            )
        else:
            raise ExecutionError(
                code="unsupported_strategy",
                message=f"unknown composite provider {node.provider!r}.",
            )

        deterministic = col_seed.deterministic
        spec = ProviderSpec(
            locale=None,
            deterministic=deterministic,
            namespace=coherent_namespace if deterministic else None,
            seed=ctx.job_seed,
            extra=dict(col_seed.provider_config),
        )
        source = df[node.columns[0]] if deterministic else None
        bundle = generator.generate_bundle(
            spec, len(df), source=source, deterministic=deterministic
        )
        for out_col, series in bundle.items():
            if out_col not in df.columns:
                # A composite that cannot write a declared output column is a
                # wiring error, not a silent partial-write (Dennis slice-2h M1).
                raise ExecutionError(
                    code="composite_output_column_missing",
                    message=(
                        f"composite {node.provider!r} produced output column {out_col!r} "
                        f"which is not in table {node.table!r}; columns={node.columns}."
                    ),
                )
            df[out_col] = list(series)
        return df, []
