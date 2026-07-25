"""Exception type for the composite-generator package (engine-v2 S8).

`CompositeError` is a peer of the other engine runtime errors (ProviderError,
GenerationError): it covers composite-specific failures such as a single-column
generate routed at a composite (which only writes bundles), an empty domain
pool, or composite wiring that cannot be resolved.

Codes used in S8:
- `composite_requires_bundle_path`: a single-column generate/generate_batch was
  routed at a composite via the BackendAdapter surface; composites write
  multiple coherent columns in one pass and must go through generate_bundle.
- `empty_domain_pool`: composite_name_email got an explicitly empty domain pool.
- `composite_wiring_inconsistent`: row-8 compile check found a coherent group
  whose columns/strategy/output_columns/namespace do not line up.
"""

from __future__ import annotations


class CompositeError(Exception):
    """Composite-generator failure. Carries a machine-readable code."""

    def __init__(self, *, code: str, message: str = "") -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}" if message else f"[{code}]")
