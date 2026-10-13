"""Phase D / D2c-i: shared fixtures and a stand-in kernel for the SEQ fpe contract.

Shapes reuse the D2b builders (`_d2b_shapes`): a plan, a graph and source tables that run
through the SEQ route (`run_sequential`) and the full-frame oracle (`run`). Every fpe key is
a 10-digit string so it clears the FF1 domain floor (radix**length >= 1,000,000).

`FpeMarkingKernel` plays the compiled FF1 kernel. It runs the pure-Python reference and then
prefixes every ciphertext, so a test can tell the kernel's output from a Python recomputation
of the same value (SEQ rebuilds buffers through pandas, so buffer identity cannot prove it).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution import _sequential_native
from decoy_engine.execution.native._crypto_ext import CryptoExtensionUnavailableError, FpeConfig
from decoy_engine.execution.native._crypto_reference import reference_fpe
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

MARK = "native:"
FPE_NS = "ns_fpe"

DIGITS = ["1234567890", "0000000001", None, "", "9999999999", "1234567890", "5555555555"]
SSN = ["123-45-6789", "987-65-4321", None, "000-00-0001", "123-45-6789", "555-55-5555", ""]
ALNUM = ["AbC123xyz9", "zzzzzzzzzz", None, "Q1w2E3r4T5", "", "AbC123xyz9", "0123456789"]
PARTIAL = ["M10000000", "M20000001", None, "10000000", "", "M10000000", "X30000002"]
JOIN = ["1234567890", "1234567890", None, "9999999999", "", "5555555555", "0000000001"]
# Composed and decomposed forms of the same text, plus a high codepoint. fpe does not
# normalize, so both must come out byte-identical native vs Python.
NFC = ["é1234567890", "é1234567890", None, "日本1234567890", "", "1234567890", "a-1234567"]
HK = [f"h{i}" for i in range(7)]


def fpe_seed(namespace: str | None = FPE_NS, **config: Any) -> ColumnSeed:
    return column_seed("fpe", namespace=namespace, provider_config=tuple(config.items()))


def payload_columns() -> dict[str, tuple[pa.Array, ColumnSeed]]:
    """name -> (source column, seed): one fpe configuration per column."""
    return {
        "f_digits": (strs(DIGITS), fpe_seed()),
        "f_ssn": (strs(SSN), fpe_seed()),
        "f_alnum": (strs(ALNUM), fpe_seed(charset="alphanum")),
        "f_nosep": (strs(DIGITS), fpe_seed(preserve_separators=False)),
        "f_luhn": (strs(["4539578763621486"] * 7), fpe_seed(validate_luhn=True)),
        "f_partial": (strs(PARTIAL), fpe_seed()),
        "f_group": (strs(JOIN), fpe_seed(fpe_join_group="grp")),
        "f_nfc": (strs(NFC), fpe_seed()),
    }


def payload_shape(
    columns: dict[str, tuple[pa.Array, ColumnSeed]] | None = None,
    *,
    extra_seeds: dict[str, ColumnSeed] | None = None,
    extra_cols: dict[str, pa.Array] | None = None,
    policy: OrphanPolicy = OrphanPolicy.PRESERVE,
    when: dict[str, str] | None = None,
) -> Shape:
    """P(payload..., hk) -> C(ck): the fpe columns are payload, the key is hashed.

    `when` maps a payload column to its `when:` expression."""
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
        {
            "P": pa.table({**data, "hk": keys}),
            "C": pa.table({"ck": keys}),
        },
    )


def fpe_key_shape(
    policy: OrphanPolicy = OrphanPolicy.REMAP, *, hash_payload: bool = False
) -> Shape:
    """A.k -> B.k -> C.k with every key an fpe column, one orphan at each hop.

    A.k is a parent key; B.k is both child and parent key (the chain's middle) and C.k a child.
    fpe is a native key strategy: A.k derives on the kernel, B.k and C.k inherit, and each
    orphan is minted by the parent's kernel call."""
    keys_a = ["1000000001", "1000000002"]
    keys_b = [*keys_a, "2000000002"]
    keys_c = [*keys_b, "3000000003"]
    plan = make_plan(
        {
            "A": table_seed(
                {
                    "k": fpe_seed("ns_ab"),
                    "n": passthrough(),
                    **({"h": hashed("ns_h")} if hash_payload else {}),
                }
            ),
            "B": table_seed({"k": fpe_seed("ns_bc")}),
            "C": table_seed({"k": fpe_seed("ns_bc")}),
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
class FpeCall:
    rows: int
    arrow_type: pa.DataType
    namespace: str
    tweak_column: str
    config: FpeConfig
    mask_key: bytes


@dataclass
class FpeMarkingKernel:
    """A `_CompiledFpeKernel.run` stand-in whose ciphertext differs from Python on every row."""

    fail_on_call: int | None = None
    calls: list[FpeCall] = field(default_factory=list)

    def run(
        self,
        values: Any,
        *,
        mask_key: bytes | None,
        namespace: str,
        tweak_column: str,
        config: FpeConfig,
        forward: bool,
        native_threads: int | None = None,
    ) -> Any:
        assert forward is True
        assert mask_key is not None
        self.calls.append(
            FpeCall(len(values), values.type, namespace, tweak_column, config, mask_key)
        )
        if self.fail_on_call == len(self.calls):
            raise RuntimeError("injected kernel fault")
        ref = reference_fpe().encrypt_batch(
            values, mask_key=mask_key, namespace=namespace, tweak_column=tweak_column, config=config
        )
        marked = [v if v in (None, "") else MARK + v for v in ref.values.to_pylist()]
        return type(ref)(values=pa.array(marked, pa.string()), errors=ref.errors, warnings=())


def install_fpe(monkeypatch: pytest.MonkeyPatch, kernel: Any) -> Any:
    """Make the SEQ route's one-time fpe companion load return `kernel`."""
    monkeypatch.setattr(_sequential_native, "load_compiled_fpe_kernel", lambda: kernel)
    return kernel


def hide_fpe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make only the fpe companion load fail, leaving the hash load alone."""

    def unavailable() -> None:
        raise CryptoExtensionUnavailableError("fpe companion hidden for a Python-only control")

    monkeypatch.setattr(_sequential_native, "load_compiled_fpe_kernel", unavailable)


def unmarked(values: list[str | None]) -> list[str | None]:
    return [None if v is None else v.removeprefix(MARK) for v in values]


def warning_view(warnings: Any) -> list[tuple[Any, ...]]:
    """Code, provider, column and detail of each warning, in emission order."""
    return [(w.code, w.provider, w.column, w.detail) for w in warnings]


def assert_marked_like(emitted: list[str | None], oracle: list[str | None]) -> None:
    """`emitted` is `oracle` with the kernel's mark on every ciphertext.

    Null and empty cells never reach the cipher, so they carry no mark."""
    assert len(emitted) == len(oracle)
    for got, want in zip(emitted, oracle, strict=True):
        assert got == (want if want in (None, "") else MARK + want)
