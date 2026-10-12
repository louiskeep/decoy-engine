"""Phase D / D2c-ii: shared fixtures and a stand-in kernel for the SEQ categorical contract.

Shapes reuse the D2b builders (`_d2b_shapes`). `CatMarkingKernel` plays the compiled INDEX
kernel (`derive_index_batch`). It returns index 0 for every valid row, so every non-null cell
becomes `categories[0]` (uniform) and a test can tell the kernel's output from the Python
handler's keyed pick. Null positions come back null, as the real kernel does.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np
import pyarrow as pa
import pytest

from decoy_engine.execution import _sequential_native
from decoy_engine.execution.native._crypto_ext import CryptoExtensionUnavailableError
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.relationships._graph import OrphanPolicy
from tests.parity._d1a_hash_data import column_seed
from tests.parity._d2b_shapes import (
    Shape,
    edge,
    graph,
    hashed,
    make_plan,
    passthrough,
    strs,
    table_seed,
)

CAT_NS = "ns_cat"
CATS = ["red", "green", "blue", "amber", "violet", "teal", "ochre"]
WEIGHTS = [5.0, 1.0, 2.0, 0.5, 3.0, 1.5, 7.0]
SRC = ["alice", "bob", None, "carol", "", "alice", "dave"]
NFC = ["é", "é", None, "日本", "", "x", "a-1"]
HK = [f"h{i}" for i in range(7)]


def cat_seed(
    namespace: str | None = CAT_NS,
    *,
    categories: Any = CATS,
    deterministic: bool | None = None,
    **config: Any,
) -> ColumnSeed:
    cfg: dict[str, Any] = {"categories": categories, **config}
    seed = column_seed("categorical", namespace=namespace, provider_config=tuple(cfg.items()))
    return seed if deterministic is None else replace(seed, deterministic=deterministic)


def payload_columns() -> dict[str, tuple[pa.Array, ColumnSeed]]:
    """name -> (source column, seed): one categorical configuration per column."""
    return {
        "c_uniform": (strs(SRC), cat_seed()),
        "c_weighted": (strs(SRC), cat_seed(weights=WEIGHTS)),
        "c_profile": (
            strs(SRC),
            cat_seed(categories=CATS[:4], weights=[10, 3, 2, 1], from_profile=True),
        ),
        "c_other_ns": (strs(SRC), cat_seed("ns_other")),
        "c_nfc": (strs(NFC), cat_seed()),
    }


ADMITTED = list(payload_columns())


def payload_shape(
    columns: dict[str, tuple[pa.Array, ColumnSeed]] | None = None,
    *,
    extra_seeds: dict[str, ColumnSeed] | None = None,
    extra_cols: dict[str, pa.Array] | None = None,
    policy: OrphanPolicy = OrphanPolicy.PRESERVE,
    when: dict[str, str] | None = None,
) -> Shape:
    """P(payload..., hk) -> C(ck): the categorical columns are payload, the key is hashed."""
    cols = columns if columns is not None else payload_columns()
    seeds = {name: seed for name, (_, seed) in cols.items()}
    data = {name: arr for name, (arr, _) in cols.items()}
    seeds.update(extra_seeds or {})
    seeds = {n: replace(sd, when=(when or {}).get(n, sd.when)) for n, sd in seeds.items()}
    data.update(extra_cols or {})
    plan = make_plan(
        {
            "P": table_seed({**seeds, "hk": hashed("ns_hk")}),
            "C": table_seed({"ck": hashed("ns_hk")}),
        }
    )
    n = len(next(iter(data.values()), HK))
    keys = strs([f"h{i}" for i in range(n)])
    return Shape(
        plan,
        graph(edge("P", "hk", "C", "ck", "ns_hk", policy)),
        {"P": pa.table({**data, "hk": keys}), "C": pa.table({"ck": keys})},
    )


def later_table_shape(columns: dict[str, tuple[pa.Array, ColumnSeed]] | None = None) -> Shape:
    """P(hk) -> C(ck, categorical...): the categorical columns live in the LATER table."""
    cols = columns if columns is not None else {"cat": (strs(SRC), cat_seed())}
    keys = strs(HK)
    plan = make_plan(
        {
            "P": table_seed({"hk": hashed("ns_hk")}),
            "C": table_seed(
                {"ck": hashed("ns_hk"), **{name: seed for name, (_, seed) in cols.items()}}
            ),
        }
    )
    return Shape(
        plan,
        graph(edge("P", "hk", "C", "ck", "ns_hk", OrphanPolicy.PRESERVE)),
        {
            "P": pa.table({"hk": keys}),
            "C": pa.table({"ck": keys, **{name: arr for name, (arr, _) in cols.items()}}),
        },
    )


def cat_key_shape(
    policy: OrphanPolicy = OrphanPolicy.REMAP, *, hash_payload: bool = False
) -> Shape:
    """A.k -> B.k -> C.k with every key a categorical column, one orphan at each hop."""
    pool = [f"key{i}" for i in range(12)]
    keys_a = ["alpha", "beta"]
    keys_b = [*keys_a, "gamma"]
    keys_c = [*keys_b, "delta"]
    plan = make_plan(
        {
            "A": table_seed(
                {
                    "k": cat_seed("ns_ab", categories=pool),
                    "n": passthrough(),
                    **({"h": hashed("ns_h")} if hash_payload else {}),
                }
            ),
            "B": table_seed({"k": cat_seed("ns_bc", categories=pool)}),
            "C": table_seed({"k": cat_seed("ns_bc", categories=pool)}),
        }
    )
    return Shape(
        plan,
        graph(
            edge("A", "k", "B", "k", "ns_ab", policy),
            edge("B", "k", "C", "k", "ns_bc", policy),
        ),
        {
            "A": pa.table(
                {
                    "k": strs(keys_a),
                    "n": strs(["x", "y"]),
                    **({"h": strs(["u", "v"])} if hash_payload else {}),
                }
            ),
            "B": pa.table({"k": strs(keys_b)}),
            "C": pa.table({"k": strs(keys_c)}),
        },
    )


@dataclass
class IndexCall:
    rows: int
    arrow_type: pa.DataType
    namespace: str
    pool_size: int
    mask_key: bytes


@dataclass
class CatMarkingKernel:
    """A `derive_index_batch` stand-in that picks index 0 for every valid row."""

    fail_on_call: int | None = None
    calls: list[IndexCall] = field(default_factory=list)

    def derive_index_batch(
        self,
        values: Any,
        *,
        mask_key: bytes,
        namespace: str,
        pool_size: int,
        native_threads: int | None = None,
    ) -> pa.Array:
        self.calls.append(IndexCall(len(values), values.type, namespace, pool_size, mask_key))
        if self.fail_on_call == len(self.calls):
            raise RuntimeError("injected kernel fault")
        valid = values.is_valid().to_numpy(zero_copy_only=False)
        return pa.array(np.zeros(len(values), dtype=np.uint64), mask=~valid, type=pa.uint64())


def install_cat(monkeypatch: pytest.MonkeyPatch, kernel: Any) -> Any:
    """Make the SEQ route's one-time index-kernel load return `kernel`."""
    monkeypatch.setattr(_sequential_native, "load_compiled_index_kernel", lambda: kernel)
    return kernel


def hide_cat(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make only the index-kernel load fail, leaving the hash and fpe loads alone."""

    def unavailable() -> None:
        raise CryptoExtensionUnavailableError("index companion hidden for a Python-only control")

    monkeypatch.setattr(_sequential_native, "load_compiled_index_kernel", unavailable)


def expect_first_category(oracle: list[Any], categories: list[str] = CATS) -> list[Any]:
    """What the index-0 stand-in emits: `categories[0]` wherever the oracle has a value."""
    return [None if v is None else categories[0] for v in oracle]
