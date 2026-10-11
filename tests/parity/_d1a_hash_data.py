"""Phase D / D1a: frozen hash fixture shared by the baseline and native-parity suites.

`EXPECTED_TOKENS` is a hardcoded literal captured from the pre-D1a pure-Python
`hash_array` at base cc2f1c71, over input every Unicode version agrees on. It is
never derived from the output under test. The native companion kernel must
reproduce it byte for byte (KAT-pinned crypto), so the same literal pins both
the Python baseline and the native result.

The post-Python-version normalization case is deliberately NOT here: it is the
one accepted divergence and lives in its own characterization test.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.execution import ParquetTransactionalSink
from decoy_engine.execution.out_of_core import run_fk_out_of_core
from decoy_engine.execution.out_of_core._source import LazySource
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph

JOB_SEED = b"\x11" * 8
# `c_utf8_secret` was captured under `SecretKeyProvider(SECRET).mask_key()` (an HKDF root,
# not the raw bytes), so it differs from the job_seed-keyed `c_utf8` tokens.
SECRET = b"x" * 32
NS = "ns_h"
TABLE = "t"

# Both composed and decomposed e-acute are present: NFC makes them one token.
_UTF8 = [
    "alice",
    "Bob",
    "",
    "héllo",
    "é",
    "é",
    None,
    "日本語",
    "\U0001f600",
    "a" * 300,
]


def source_columns() -> dict[str, pa.Array]:
    return {
        "c_utf8": pa.array(_UTF8, pa.string()),
        "c_large": pa.array(_UTF8, pa.large_string()),
        "c_bool": pa.array([True, False, None, True, False, True, None, False, True, False]),
        "c_i8": pa.array([-128, -1, 0, 1, 127, None, 5, -5, 64, -64], pa.int8()),
        "c_i16": pa.array([-32768, -1, 0, 1, 32767, None, 5, -5, 256, -256], pa.int16()),
        "c_i32": pa.array([-(2**31), -1, 0, 1, 2**31 - 1, None, 5, -5, 65536, -65536], pa.int32()),
        "c_i64": pa.array(
            [-(2**63), -1, 0, 1, 2**63 - 1, None, 5, -5, 2**32, -(2**32)], pa.int64()
        ),
        "c_u8": pa.array([0, 1, 127, 128, 255, None, 5, 6, 7, 8], pa.uint8()),
        "c_u16": pa.array([0, 1, 255, 256, 65535, None, 5, 6, 7, 8], pa.uint16()),
        "c_u32": pa.array([0, 1, 65535, 65536, 2**32 - 1, None, 5, 6, 7, 8], pa.uint32()),
        "c_u64": pa.array([0, 1, 2**32, 2**63 - 1, 2**63, 2**64 - 1, None, 6, 7, 8], pa.uint64()),
    }


def column_seed(
    strategy: str = "hash",
    *,
    namespace: str | None = NS,
    provider_config: tuple[tuple[str, Any], ...] = (),
) -> ColumnSeed:
    return ColumnSeed(
        namespace=namespace,
        strategy=strategy,
        provider=strategy,
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=namespace is not None,
        provider_config=provider_config,
        coherent_with=(),
    )


_PASSTHROUGH = column_seed("passthrough", namespace=None)
HK_NS = "ns_hk"


@dataclass(frozen=True)
class ChainRun:
    """One finished chain run: the caller-visible result plus every output table."""

    result: Any
    tables: dict[str, pa.Table]


def _edge(
    parent: str,
    parent_col: str,
    child: str,
    child_col: str,
    namespace: str,
    policy: OrphanPolicy = OrphanPolicy.PRESERVE,
) -> Any:
    return RelationshipEdge(
        parent_table=parent,
        parent_columns=(parent_col,),
        child_table=child,
        child_columns=(child_col,),
        namespace=namespace,
        orphan_policy=policy,
    )


def chain_sources(payload: pa.Table) -> dict[str, pa.Table]:
    """`up.pk -> t.fk` and `t.hk -> child.ck`: `t` carries the payload under test.

    `t` is both an FK child (so either driver can serve it) and a parent-key owner
    (`hk` is a hash parent key, so the parent-key path is exercised too).
    """
    n = payload.num_rows
    keys = [f"k{i}" for i in range(n)]
    ints = pa.array(range(n), pa.int64())
    return {
        "up": pa.table({"pk": ints}),
        "t": payload.append_column("fk", ints).append_column("hk", pa.array(keys, pa.string())),
        "child": pa.table({"ck": pa.array(keys, pa.string())}),
    }


def chain_plan(payload_seeds: tuple[tuple[str, ColumnSeed], ...]) -> Any:
    hk = column_seed(namespace=HK_NS)
    tables = (
        ("up", TableSeed(per_column=(("pk", _PASSTHROUGH),), per_group=())),
        (
            "t",
            TableSeed(per_column=(*payload_seeds, ("hk", hk), ("fk", _PASSTHROUGH)), per_group=()),
        ),
        ("child", TableSeed(per_column=(("ck", hk),), per_group=())),
    )
    return SimpleNamespace(seed_envelope=SeedEnvelope(job_seed=JOB_SEED, per_table=tables))


def chain_graph(hk_policy: OrphanPolicy = OrphanPolicy.PRESERVE) -> RelationshipGraph:
    return RelationshipGraph(
        edges=(
            _edge("up", "pk", "t", "fk", "ns_up"),
            _edge("t", "hk", "child", "ck", HK_NS, hk_policy),
        ),
        ordering=(),
    )


def run_chain(
    payload: pa.Table,
    payload_seeds: tuple[tuple[str, ColumnSeed], ...],
    *,
    out_dir: Path | None = None,
    reorder: bool = False,
    lazy_dir: Path | None = None,
    sources: dict[str, Any] | None = None,
    hk_policy: OrphanPolicy = OrphanPolicy.PRESERVE,
    **run_kwargs: Any,
) -> ChainRun:
    """Run the chain through `run_fk_out_of_core`.

    `out_dir` selects the sink path (outputs are read back from the committed
    parquet); `reorder` forces the reorder driver (`stream_table`) for every table
    with an incoming edge, which needs the sink; `lazy_dir` serves the sources from
    parquet files through `LazySource`.
    """
    if reorder:
        assert out_dir is not None, "the reorder driver is sink-only"
        run_kwargs.update(
            budget_bytes=1 << 30,
            temp_disk_budget_bytes=200 << 30,
            out_of_core_reorder_threshold_rows=0,
        )
    srcs: dict[str, Any] = sources if sources is not None else chain_sources(payload)
    if lazy_dir is not None:
        for name, tbl in list(srcs.items()):
            if isinstance(tbl, pa.Table):
                path = lazy_dir / f"{name}.parquet"
                pq.write_table(tbl, path)
                srcs[name] = LazySource(path)
    result = run_fk_out_of_core(
        chain_plan(payload_seeds),
        srcs,
        registry=get_default_registry(),
        relationship_graph=chain_graph(hk_policy),
        sink=ParquetTransactionalSink(out_dir) if out_dir is not None else None,
        **run_kwargs,
    )
    if out_dir is None:
        return ChainRun(result, dict(result.outputs))
    return ChainRun(result, {n: pq.read_table(out_dir / f"{n}.parquet") for n in srcs})


def hash_seeds(names: list[str]) -> tuple[tuple[str, ColumnSeed], ...]:
    return tuple((n, column_seed()) for n in names)


EXPECTED_TOKENS: dict[str, list[str | None]] = {
    "c_utf8": [
        "12a8831ad766dfa96f1e9f816f569d04456b29329929c71b86235b6b8cc3f997",
        "42ec01e8fdaef2c887a3bc6c39ab35f8f4b617e7c53a9743ce7bdd4551136ad6",
        "b9d8dbb8dcc68e925042887109fe337db9f7477bd31e6ccca264e43d2d818618",
        "7acf6518759e656419fbda301e5d9585a5fc86be3ccb0f1da9fc9f765e75e24b",
        "b1cdb302bc12dce0b3b2ce02e79e68efbc2b74a49245f22bb97e98cf6b41617a",
        "b1cdb302bc12dce0b3b2ce02e79e68efbc2b74a49245f22bb97e98cf6b41617a",
        None,
        "61555275dbf864d354f06a05aea243f6c298d082ccab49684c46f01650301f65",
        "540cbfa6ddffe5ff237fdd3e9ee3e48c05700b768b5df4f3feb5d2f85125783a",
        "65b2c55dc955da93ba2d05cda5b29888dc494561dc9483e395c4eb12f86d4aef",
    ],
    "c_large": [
        "12a8831ad766dfa96f1e9f816f569d04456b29329929c71b86235b6b8cc3f997",
        "42ec01e8fdaef2c887a3bc6c39ab35f8f4b617e7c53a9743ce7bdd4551136ad6",
        "b9d8dbb8dcc68e925042887109fe337db9f7477bd31e6ccca264e43d2d818618",
        "7acf6518759e656419fbda301e5d9585a5fc86be3ccb0f1da9fc9f765e75e24b",
        "b1cdb302bc12dce0b3b2ce02e79e68efbc2b74a49245f22bb97e98cf6b41617a",
        "b1cdb302bc12dce0b3b2ce02e79e68efbc2b74a49245f22bb97e98cf6b41617a",
        None,
        "61555275dbf864d354f06a05aea243f6c298d082ccab49684c46f01650301f65",
        "540cbfa6ddffe5ff237fdd3e9ee3e48c05700b768b5df4f3feb5d2f85125783a",
        "65b2c55dc955da93ba2d05cda5b29888dc494561dc9483e395c4eb12f86d4aef",
    ],
    "c_bool": [
        "de2e0a6db7f07bef11e7487262776f75aeb9eb92d87c71b4669e6f6d5283c1c4",
        "628cdd08b264e78bf7befa12f27b34ea006ca9046272f91a7912f68e2f60d2e1",
        None,
        "de2e0a6db7f07bef11e7487262776f75aeb9eb92d87c71b4669e6f6d5283c1c4",
        "628cdd08b264e78bf7befa12f27b34ea006ca9046272f91a7912f68e2f60d2e1",
        "de2e0a6db7f07bef11e7487262776f75aeb9eb92d87c71b4669e6f6d5283c1c4",
        None,
        "628cdd08b264e78bf7befa12f27b34ea006ca9046272f91a7912f68e2f60d2e1",
        "de2e0a6db7f07bef11e7487262776f75aeb9eb92d87c71b4669e6f6d5283c1c4",
        "628cdd08b264e78bf7befa12f27b34ea006ca9046272f91a7912f68e2f60d2e1",
    ],
    "c_i8": [
        "d93cd8c8c930464b72af82ca33c9358cef2744524343abb5db05452c2c2b4437",
        "08857bd02dab1ca8249e70c5771d47cb00114170dd6ceacd7c58d86b6f54204f",
        "b20a0a10744a10a93c00ff9e1f1b2ea3ba64ea93606430b29d5d5222cf2d5f2b",
        "957cbc7db09b53a0839ddfc52417edc146692e63900295326a86c000b7c2abac",
        "c4b3408862b94f6ed60410201675c6380b5a656e9c1d33eeb493c885a598e9b9",
        None,
        "43d60f3cbfcb1982d20e31ab89954b577877aa35b61dace6c3b178aafd196837",
        "f699632bc3b264e162eeeb4f7e25e08257e6f34022508047d914bb247b7d824b",
        "c1602fa06f6ad1a23394c38b82ebea060cb9a14880757576e8fdde4422d5e90f",
        "5f1c9c96282803d6020859b54212c203da511db4cf73fafae2eeeaaa896351f0",
    ],
    "c_i16": [
        "d7f488b1a2fe8d070fdac286fb767deb03ec1129a48f224f5d1833638c72c021",
        "08857bd02dab1ca8249e70c5771d47cb00114170dd6ceacd7c58d86b6f54204f",
        "b20a0a10744a10a93c00ff9e1f1b2ea3ba64ea93606430b29d5d5222cf2d5f2b",
        "957cbc7db09b53a0839ddfc52417edc146692e63900295326a86c000b7c2abac",
        "6625df8eaf31c401754903351ae59045426e28c37b5cf91eb19f949b55666e31",
        None,
        "43d60f3cbfcb1982d20e31ab89954b577877aa35b61dace6c3b178aafd196837",
        "f699632bc3b264e162eeeb4f7e25e08257e6f34022508047d914bb247b7d824b",
        "8e0b47b8b4a6dafd6e8293a8f35f3bf73139872ee7296587f14bc7175b971758",
        "3ba3c68e085ea414c53a697ac85d9719367e347a2c06aa625e296dec5f102c95",
    ],
    "c_i32": [
        "bd0ec395114485a0257071583a05304cacc78724f6637542dd604b66f1228a69",
        "08857bd02dab1ca8249e70c5771d47cb00114170dd6ceacd7c58d86b6f54204f",
        "b20a0a10744a10a93c00ff9e1f1b2ea3ba64ea93606430b29d5d5222cf2d5f2b",
        "957cbc7db09b53a0839ddfc52417edc146692e63900295326a86c000b7c2abac",
        "2b6d5fced6617443267a72fd75e74f226d4c82625f3415074f2a8acc319dd294",
        None,
        "43d60f3cbfcb1982d20e31ab89954b577877aa35b61dace6c3b178aafd196837",
        "f699632bc3b264e162eeeb4f7e25e08257e6f34022508047d914bb247b7d824b",
        "e2736695932b515087ef8fa479dd45bfd0870247e70a0d608659504308396181",
        "4ebe3f901261b7f2c6b96ba8cdadfcd4c1e04e809d95082ab238e850f3d9e5be",
    ],
    "c_i64": [
        "0d5f9011d4ea9f161c12577da671915944ebeaff140d642edd25b8c3bc8939e1",
        "08857bd02dab1ca8249e70c5771d47cb00114170dd6ceacd7c58d86b6f54204f",
        "b20a0a10744a10a93c00ff9e1f1b2ea3ba64ea93606430b29d5d5222cf2d5f2b",
        "957cbc7db09b53a0839ddfc52417edc146692e63900295326a86c000b7c2abac",
        "e7b1e37c31002feb4ba87277d1b34c620474e6d70b910e6e619f44e2e649a9be",
        None,
        "43d60f3cbfcb1982d20e31ab89954b577877aa35b61dace6c3b178aafd196837",
        "f699632bc3b264e162eeeb4f7e25e08257e6f34022508047d914bb247b7d824b",
        "375ce7f2d5d8862c73601a954f0f6ca2c710611b8cbec641b8e0d6e52bfd32c4",
        "3882eb212d4452ee68352b7546d38101162592aa38629b1038efbdc616b80bfc",
    ],
    "c_u8": [
        "b20a0a10744a10a93c00ff9e1f1b2ea3ba64ea93606430b29d5d5222cf2d5f2b",
        "957cbc7db09b53a0839ddfc52417edc146692e63900295326a86c000b7c2abac",
        "c4b3408862b94f6ed60410201675c6380b5a656e9c1d33eeb493c885a598e9b9",
        "9d20fc8295f863f59148385b4769b6f4c4441429d970f8e662d04bc9b0762e11",
        "2cb63ce6da25c62345773f9f27d1cd6d0b003d54f8e2fc8e4fd831da272e16da",
        None,
        "43d60f3cbfcb1982d20e31ab89954b577877aa35b61dace6c3b178aafd196837",
        "9d6723bc3ea2549dfa0c3c015f1a9ea455d5ae0558c3421ef5fd53ec722f926a",
        "0a30e42bdacc9ec28d1beb33ed50b37dd6e4b45a5cc89e277579abb551ac3b35",
        "d7b781e8304280534cf234669d0d62b05b9e3816909251aee80a7b1f82fd43b4",
    ],
    "c_u16": [
        "b20a0a10744a10a93c00ff9e1f1b2ea3ba64ea93606430b29d5d5222cf2d5f2b",
        "957cbc7db09b53a0839ddfc52417edc146692e63900295326a86c000b7c2abac",
        "2cb63ce6da25c62345773f9f27d1cd6d0b003d54f8e2fc8e4fd831da272e16da",
        "8e0b47b8b4a6dafd6e8293a8f35f3bf73139872ee7296587f14bc7175b971758",
        "a8905dfac8277b6102ac78d8602514b2daab8120622fffdd7d33b6efe2df60f4",
        None,
        "43d60f3cbfcb1982d20e31ab89954b577877aa35b61dace6c3b178aafd196837",
        "9d6723bc3ea2549dfa0c3c015f1a9ea455d5ae0558c3421ef5fd53ec722f926a",
        "0a30e42bdacc9ec28d1beb33ed50b37dd6e4b45a5cc89e277579abb551ac3b35",
        "d7b781e8304280534cf234669d0d62b05b9e3816909251aee80a7b1f82fd43b4",
    ],
    "c_u32": [
        "b20a0a10744a10a93c00ff9e1f1b2ea3ba64ea93606430b29d5d5222cf2d5f2b",
        "957cbc7db09b53a0839ddfc52417edc146692e63900295326a86c000b7c2abac",
        "a8905dfac8277b6102ac78d8602514b2daab8120622fffdd7d33b6efe2df60f4",
        "e2736695932b515087ef8fa479dd45bfd0870247e70a0d608659504308396181",
        "582d70c6b349545f7d462d7895328fd652331a0c0058e7fe9cce0ee5173ce033",
        None,
        "43d60f3cbfcb1982d20e31ab89954b577877aa35b61dace6c3b178aafd196837",
        "9d6723bc3ea2549dfa0c3c015f1a9ea455d5ae0558c3421ef5fd53ec722f926a",
        "0a30e42bdacc9ec28d1beb33ed50b37dd6e4b45a5cc89e277579abb551ac3b35",
        "d7b781e8304280534cf234669d0d62b05b9e3816909251aee80a7b1f82fd43b4",
    ],
    "c_u64": [
        "b20a0a10744a10a93c00ff9e1f1b2ea3ba64ea93606430b29d5d5222cf2d5f2b",
        "957cbc7db09b53a0839ddfc52417edc146692e63900295326a86c000b7c2abac",
        "375ce7f2d5d8862c73601a954f0f6ca2c710611b8cbec641b8e0d6e52bfd32c4",
        "e7b1e37c31002feb4ba87277d1b34c620474e6d70b910e6e619f44e2e649a9be",
        "21634bed8caf64bfc935383a272a70f1f49f358634c9cc69e5cbf112f1952d8b",
        "f5d850d328e7f6c14bc61e5af45702213684f6f16dc0a1f86ba67e7b0e81c601",
        None,
        "9d6723bc3ea2549dfa0c3c015f1a9ea455d5ae0558c3421ef5fd53ec722f926a",
        "0a30e42bdacc9ec28d1beb33ed50b37dd6e4b45a5cc89e277579abb551ac3b35",
        "d7b781e8304280534cf234669d0d62b05b9e3816909251aee80a7b1f82fd43b4",
    ],
    "c_trunc": [
        "12a8831ad766",
        "42ec01e8fdae",
        "b9d8dbb8dcc6",
        "7acf6518759e",
        "b1cdb302bc12",
        "b1cdb302bc12",
        None,
        "61555275dbf8",
        "540cbfa6ddff",
        "65b2c55dc955",
    ],
    "c_other_ns": [
        "3797c47ef6f274aa4ccdf54c5660c0a0d3cf2e586293f46954d35566d7a0bb81",
        "95a3cb7f5dbde6cae629633df5c7cfe599b445e7981ef54afea74b0eac721050",
        "2bad60a4203190effd976fc7351a6b3cf9f8d897f3c317faefb52b018b04c0a3",
        "79f0e855f35972b4e037f96f1c3efe08662654645a922cb5733294d1cbaedabf",
        "188b1ce5ab3b810ceebac7a80e2bf7562865d2af097eba68daa2fa1f653cc8c9",
        "188b1ce5ab3b810ceebac7a80e2bf7562865d2af097eba68daa2fa1f653cc8c9",
        None,
        "22b7a008fc83e79ef0e673e283caf1e8a167a7fb1737032e6da099fd3a0444ff",
        "0269a3c4f6fcebb88087b73a6e33254bb4e224361628bc8316b498ac28a51e02",
        "aadbc64ac4c0e2427cc8eabb05dd0ff4c4753cef208a3f6f388474c3292b81a0",
    ],
    "c_utf8_secret": [
        "8cbe7418c0704f999f7276f6db3a8714c9423de3f310c4a3237153e18ac254e9",
        "5ddf77b464af655c28abeb032bf5fcc93d511a90a378f17ab0029af9cf050116",
        "c8196b5432d5920d14af8ba7db299567421cb9a3b139eefcfc8fa2c1872c9e34",
        "b7e9750caccfba5d0d64e0e52c87c3b1a89b0cf0ea5f5c36e6c42a9521a5e88f",
        "8f1764bde2c41fcb23503bc0277bf17f9767b4b0bbb5f13b7015dcfe3f770624",
        "8f1764bde2c41fcb23503bc0277bf17f9767b4b0bbb5f13b7015dcfe3f770624",
        None,
        "f4609be91e812f63eb27370ce4ecc703b2cb0090ce132f0824cc5f5efbe123f8",
        "0c254acf7a4712c4fe180590c0f8ff9367f0fb289db87864bd7524b0722dfcc4",
        "24a55c13ca1a0a6a6e04184550b1a9290951df3892e847cff538c2752a813739",
    ],
    "hk": [
        "5cc208926b17470aaf133d87f5b3af1c38b90c6e4aaae396b61969cd62909ce9",
        "eedfb57fc97e727bb0ff15de30b22b8b97836cc956caf2285b08189af83c579e",
        "2c5d8af9502e6a0b12de7283a4836cb8875f80cd6b98232e5800cad7a1aa20a9",
        "68485de0002838444384621dd2b2729265fee33fbd36b13085830e1633796bbc",
        "1a9b944edfc336b5ccfb3a1b3474726bda314e84232538d80b23e2c6d3e7e922",
        "915a48cc432aff85a4acf21a45216daa4151b153b655bb7192a61abdf0fd1603",
        "fc5edce975fd14130ec3c1ee3c1f2f929780ba28fd905b7eab5114a6f039f710",
        "c26c20f2c3f71b0d0d81b7ebf92b47339b870119c702dbb1aeaa59e3dce6397e",
        "d7f24e7086dfa1e76922af0a835341aed4a5ddd9cc6cb096738b25f098d802ea",
        "153a98c6e601cba1e32c9939c31e661adf7d7c83459564983e1ce21f809a4974",
    ],
}
