"""BundlePool: a ValuePool whose entries are tuples (engine-v2 S8).

Per S8 spec §3a + cross-sprint contracts §2.7. A BundlePool holds a frozen
object-dtype array whose entries are length-K tuples (K == len(output_columns)),
one tuple per row of a shared latent. It inherits ValuePool's identity tuple
verbatim: `composite_name` lives in the inherited `provider` field, so
`.identity` is `(composite_name, locale, config_hash, seed, size)` with NO
override (R5; verified against the live ValuePool.identity property).

The single new field `output_columns` MUST carry a default because every
inherited ValuePool field is non-defaulted (a dataclass cannot have a
non-default field follow a default one). `()` is the safe default; real bundles
always set it.
"""

from __future__ import annotations

import dataclasses

from decoy_engine.generation.pool._value_pool import ValuePool


@dataclasses.dataclass(frozen=True)
class BundlePool(ValuePool):
    """A frozen pool whose `values` entries are tuples of length len(output_columns).

    Cache-keyed by the inherited 5-tuple identity (composite_name in the
    `provider` slot). Do not override `identity`.
    """

    output_columns: tuple[str, ...] = ()
