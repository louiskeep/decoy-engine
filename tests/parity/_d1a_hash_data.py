"""Phase D / D1a: frozen hash fixture shared by the baseline and native-parity suites.

`EXPECTED_TOKENS` is a hardcoded literal captured from the pre-D1a pure-Python
`hash_array` at base cc2f1c71, over input every Unicode version agrees on. It is
never derived from the output under test. The native companion kernel must
reproduce it byte for byte (KAT-pinned crypto), so the same literal pins both
the Python baseline and the native result.

Re-pinned under F13 (job_seed keying was removed): every token except `c_utf8_secret` is now
the pure-Python `hash_array` keyed on `THROWAWAY_KEY_PROVIDER.mask_key()`, the key `run_chain` passes.

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
import pytest

from decoy_engine.execution import ParquetTransactionalSink
from decoy_engine.execution.out_of_core import _hash_run, run_fk_out_of_core
from decoy_engine.execution.out_of_core._source import LazySource
from decoy_engine.kernel import hash_array
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph
from tests._keyed_secret import THROWAWAY_KEY_PROVIDER

JOB_SEED = b"\x11" * 8
# The key every keyed route in this fixture runs under (`run_chain` passes the provider).
MASK_KEY = THROWAWAY_KEY_PROVIDER.mask_key()
# `c_utf8_secret` was captured under `SecretKeyProvider(SECRET).mask_key()` (an HKDF root,
# not the raw bytes), so it differs from the throwaway-keyed `c_utf8` tokens.
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
    sink: Any | None = None,
    reorder: bool = False,
    lazy_dir: Path | None = None,
    sources: dict[str, Any] | None = None,
    hk_policy: OrphanPolicy = OrphanPolicy.PRESERVE,
    **run_kwargs: Any,
) -> ChainRun:
    """Run the chain through `run_fk_out_of_core`.

    `out_dir` selects the sink path (outputs are read back from the committed
    parquet); `sink` substitutes a recording sink writing to `out_dir`; `reorder` forces the reorder driver (`stream_table`) for every table
    with an incoming edge, which needs the sink; `lazy_dir` serves the sources from
    parquet files through `LazySource`.
    """
    run_kwargs.setdefault("key_provider", THROWAWAY_KEY_PROVIDER)
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
        sink=sink or (ParquetTransactionalSink(out_dir) if out_dir is not None else None),
        **run_kwargs,
    )
    if out_dir is None:
        return ChainRun(result, dict(result.outputs))
    return ChainRun(result, {n: pq.read_table(out_dir / f"{n}.parquet") for n in srcs})


def hash_seeds(names: list[str]) -> tuple[tuple[str, ColumnSeed], ...]:
    return tuple((n, column_seed()) for n in names)


class PythonReferenceKernel:
    """A stand-in for the compiled companion kernel, computed with the Python hash.

    Lets the routing machinery (decision, pinning, abort, disposition) be tested on a box
    without the companion; the real kernel is exercised by the companion-present suites.
    `fail_on_call` makes the Nth `derive_batch` raise, to model a mid-run kernel fault."""

    def __init__(self, fail_on_call: int | None = None) -> None:
        self.calls = 0
        self.fail_on_call = fail_on_call

    def derive_batch(
        self,
        values: Any,
        *,
        mask_key: bytes,
        namespace: str,
        truncate: int | None,
        native_threads: int | None = None,
    ) -> pa.Array:
        self.calls += 1
        if self.calls == self.fail_on_call:
            raise RuntimeError("injected kernel fault")
        return hash_array(values, seed=mask_key, namespace=namespace, truncate=truncate)


def install_companion(
    monkeypatch: pytest.MonkeyPatch, kernel: PythonReferenceKernel | None = None
) -> dict[str, Any]:
    """Make the route's one-time companion load return `kernel`; counts the loads."""
    state: dict[str, Any] = {"loads": 0, "kernel": kernel or PythonReferenceKernel()}

    def load() -> PythonReferenceKernel:
        state["loads"] += 1
        return state["kernel"]

    monkeypatch.setattr(_hash_run, "load_compiled_crypto_kernel", load)
    return state


EXPECTED_TOKENS: dict[str, list[str | None]] = {
    "c_utf8": [
        "68fbd2ab5cc3b5e136d4b076ece2559fc72bf54f3c46f3c75b8798ab08171ade",
        "49f52b76c4be928e3814de3dddbf8807cb6eb98a33c8bfbb05e7f9753778a9e6",
        "19030a0354cbdb4c7b0cc4f76e9c31904842f61ce32307d228ac09f72c860393",
        "411512c530cdf942c8b1c49aa4c74f51644fc3ffeda10a6c79b7c4afb9a8d19b",
        "a5ead2b5cc2a613ba6bb7b7d4d1c08ad041f2f24466aaff9e04814541db571c3",
        "a5ead2b5cc2a613ba6bb7b7d4d1c08ad041f2f24466aaff9e04814541db571c3",
        None,
        "65c9d5bd84b189e7ce2f4481ee295317e1bef3739f55843097980f38b8dc48a8",
        "5f8bd31e2f1a67f6d71e9299d3123e97f74b61ac21475e66e141df3f3838a66c",
        "b2f33be00e8333ed177a7b0122baf78b41f90ebdaaa25c30c7e7a95d2bbeec8b",
    ],
    "c_large": [
        "68fbd2ab5cc3b5e136d4b076ece2559fc72bf54f3c46f3c75b8798ab08171ade",
        "49f52b76c4be928e3814de3dddbf8807cb6eb98a33c8bfbb05e7f9753778a9e6",
        "19030a0354cbdb4c7b0cc4f76e9c31904842f61ce32307d228ac09f72c860393",
        "411512c530cdf942c8b1c49aa4c74f51644fc3ffeda10a6c79b7c4afb9a8d19b",
        "a5ead2b5cc2a613ba6bb7b7d4d1c08ad041f2f24466aaff9e04814541db571c3",
        "a5ead2b5cc2a613ba6bb7b7d4d1c08ad041f2f24466aaff9e04814541db571c3",
        None,
        "65c9d5bd84b189e7ce2f4481ee295317e1bef3739f55843097980f38b8dc48a8",
        "5f8bd31e2f1a67f6d71e9299d3123e97f74b61ac21475e66e141df3f3838a66c",
        "b2f33be00e8333ed177a7b0122baf78b41f90ebdaaa25c30c7e7a95d2bbeec8b",
    ],
    "c_bool": [
        "7b20c195cd7caed4f748fae57acc1f70f01036766aafef81a543b0ce869425e6",
        "4d797e2ba251c28b9bcaa0209b8285dfae18029d7c3cfb5f2167362c9388c29c",
        None,
        "7b20c195cd7caed4f748fae57acc1f70f01036766aafef81a543b0ce869425e6",
        "4d797e2ba251c28b9bcaa0209b8285dfae18029d7c3cfb5f2167362c9388c29c",
        "7b20c195cd7caed4f748fae57acc1f70f01036766aafef81a543b0ce869425e6",
        None,
        "4d797e2ba251c28b9bcaa0209b8285dfae18029d7c3cfb5f2167362c9388c29c",
        "7b20c195cd7caed4f748fae57acc1f70f01036766aafef81a543b0ce869425e6",
        "4d797e2ba251c28b9bcaa0209b8285dfae18029d7c3cfb5f2167362c9388c29c",
    ],
    "c_i8": [
        "836cc68c7ba9978c6dd2aad8b4ed8977b4600e134e1975076e9f7bbabd45925b",
        "5440b9d05ffa31b55854ba02be381da142efc4650758db2533aca20cf5abed1b",
        "1b75450c6f0d92d30df6c9b8b2c1850c768213917f5e559682d071316788b821",
        "87e5cf79e49cc087ba1e37e4ed3d82eb90e985bfc9b33db40568203c3ce978f5",
        "c5395c2934d3f12106f085877fbba2431de9e42550e5aaec23546a0699dbf758",
        None,
        "579805156c54ffd624e85e9e2a761e1f85a2de475dc38c7a957a6c7c145fbc09",
        "17cc7e098b2508f4a512df8cc316ee24620b54ac21578b88dead057904445eab",
        "c5185b2bddcc83f5dc12537c3e6e7f6efb7ae5a475dff45af69a3c6141b08519",
        "676ce5d08bc01ed28e89234b501436d36a1e478def6f156360e368e8415ff849",
    ],
    "c_i16": [
        "6f67ded820efe36c1e744b3f07b002d440d07e245f9d17a83afd389f23321455",
        "5440b9d05ffa31b55854ba02be381da142efc4650758db2533aca20cf5abed1b",
        "1b75450c6f0d92d30df6c9b8b2c1850c768213917f5e559682d071316788b821",
        "87e5cf79e49cc087ba1e37e4ed3d82eb90e985bfc9b33db40568203c3ce978f5",
        "832ffb57908241e99834587f64cf797a5fda105dde98d89a493d37804f39cad6",
        None,
        "579805156c54ffd624e85e9e2a761e1f85a2de475dc38c7a957a6c7c145fbc09",
        "17cc7e098b2508f4a512df8cc316ee24620b54ac21578b88dead057904445eab",
        "debd1856978b26b6d002b5144ec45e38bca4ba07433ec3b08b788ce3e7c13ab8",
        "b519b9a94d506827dad43f301c65037e45f219dcc6703c9eec67bd2118eb07d1",
    ],
    "c_i32": [
        "571ff2d0ed400252c80af2891fe56f0ea0e92547964a22ecd8573001649ca2e1",
        "5440b9d05ffa31b55854ba02be381da142efc4650758db2533aca20cf5abed1b",
        "1b75450c6f0d92d30df6c9b8b2c1850c768213917f5e559682d071316788b821",
        "87e5cf79e49cc087ba1e37e4ed3d82eb90e985bfc9b33db40568203c3ce978f5",
        "17a2b775bc574609bf3584c0e052351002b0cf0511e80d9398ba6be80d5fc18c",
        None,
        "579805156c54ffd624e85e9e2a761e1f85a2de475dc38c7a957a6c7c145fbc09",
        "17cc7e098b2508f4a512df8cc316ee24620b54ac21578b88dead057904445eab",
        "61be83c67b0a83541a5ce4296a08409a6da1a713c40b55477814a08b498bee72",
        "235808f8cf0a69756cc5e2f9573795ed009f699a1b80ee3c220ca36bf5b8d439",
    ],
    "c_i64": [
        "75a949c068aecaaa6797826001f1639e13df0271db73b259965c643d2fba4579",
        "5440b9d05ffa31b55854ba02be381da142efc4650758db2533aca20cf5abed1b",
        "1b75450c6f0d92d30df6c9b8b2c1850c768213917f5e559682d071316788b821",
        "87e5cf79e49cc087ba1e37e4ed3d82eb90e985bfc9b33db40568203c3ce978f5",
        "af1a164af87f50a662c9a0a75f6e28c60fce79c134e7f8f9187bc4fa76ee78a3",
        None,
        "579805156c54ffd624e85e9e2a761e1f85a2de475dc38c7a957a6c7c145fbc09",
        "17cc7e098b2508f4a512df8cc316ee24620b54ac21578b88dead057904445eab",
        "be5906b8844963fc09f059783fe6a5ef7f208c15d7bd385979a31537b0a03a14",
        "cbd9ef83968f8acb2ed4a72cdfc18d8107937dd5953146b57c833d38cbb49981",
    ],
    "c_u8": [
        "1b75450c6f0d92d30df6c9b8b2c1850c768213917f5e559682d071316788b821",
        "87e5cf79e49cc087ba1e37e4ed3d82eb90e985bfc9b33db40568203c3ce978f5",
        "c5395c2934d3f12106f085877fbba2431de9e42550e5aaec23546a0699dbf758",
        "40286aa830a9e4bc38bf8f5571da14fe5537054b306ae956464ed27c30f93f83",
        "d67a750d714bea0acec5be7119beb7e319c829c525b5b0d26687b2691aa43c44",
        None,
        "579805156c54ffd624e85e9e2a761e1f85a2de475dc38c7a957a6c7c145fbc09",
        "408af5f09a900e4cfe6f38c22607c71ae4a314f3dd493669513c73f095e7fdf3",
        "3bbcfb10be13ab9ca04d960c2e248cad2fbe3f90805bbd8909e948f9eec4c172",
        "413e3fd23deea74bee9d4cb424ff53caa52028232ad6bb5e4167a1ef1ee566c2",
    ],
    "c_u16": [
        "1b75450c6f0d92d30df6c9b8b2c1850c768213917f5e559682d071316788b821",
        "87e5cf79e49cc087ba1e37e4ed3d82eb90e985bfc9b33db40568203c3ce978f5",
        "d67a750d714bea0acec5be7119beb7e319c829c525b5b0d26687b2691aa43c44",
        "debd1856978b26b6d002b5144ec45e38bca4ba07433ec3b08b788ce3e7c13ab8",
        "680572e5142497040e38592256147596835cb77020d793a8d91781b33d2a4bb2",
        None,
        "579805156c54ffd624e85e9e2a761e1f85a2de475dc38c7a957a6c7c145fbc09",
        "408af5f09a900e4cfe6f38c22607c71ae4a314f3dd493669513c73f095e7fdf3",
        "3bbcfb10be13ab9ca04d960c2e248cad2fbe3f90805bbd8909e948f9eec4c172",
        "413e3fd23deea74bee9d4cb424ff53caa52028232ad6bb5e4167a1ef1ee566c2",
    ],
    "c_u32": [
        "1b75450c6f0d92d30df6c9b8b2c1850c768213917f5e559682d071316788b821",
        "87e5cf79e49cc087ba1e37e4ed3d82eb90e985bfc9b33db40568203c3ce978f5",
        "680572e5142497040e38592256147596835cb77020d793a8d91781b33d2a4bb2",
        "61be83c67b0a83541a5ce4296a08409a6da1a713c40b55477814a08b498bee72",
        "dc89d0775d278794cc5bf635e6977de7cacfc07d0a1f37103f8239c0c508de51",
        None,
        "579805156c54ffd624e85e9e2a761e1f85a2de475dc38c7a957a6c7c145fbc09",
        "408af5f09a900e4cfe6f38c22607c71ae4a314f3dd493669513c73f095e7fdf3",
        "3bbcfb10be13ab9ca04d960c2e248cad2fbe3f90805bbd8909e948f9eec4c172",
        "413e3fd23deea74bee9d4cb424ff53caa52028232ad6bb5e4167a1ef1ee566c2",
    ],
    "c_u64": [
        "1b75450c6f0d92d30df6c9b8b2c1850c768213917f5e559682d071316788b821",
        "87e5cf79e49cc087ba1e37e4ed3d82eb90e985bfc9b33db40568203c3ce978f5",
        "be5906b8844963fc09f059783fe6a5ef7f208c15d7bd385979a31537b0a03a14",
        "af1a164af87f50a662c9a0a75f6e28c60fce79c134e7f8f9187bc4fa76ee78a3",
        "dcf72fd63c37077f5c0fb6b68a682bc309ee8a43fccd043b02074dd3ddffa0dc",
        "7dbef3737c8c5209cee3a5673a206cb7944184f6a128c7a97e15d17f4525f85b",
        None,
        "408af5f09a900e4cfe6f38c22607c71ae4a314f3dd493669513c73f095e7fdf3",
        "3bbcfb10be13ab9ca04d960c2e248cad2fbe3f90805bbd8909e948f9eec4c172",
        "413e3fd23deea74bee9d4cb424ff53caa52028232ad6bb5e4167a1ef1ee566c2",
    ],
    "c_trunc": [
        "68fbd2ab5cc3",
        "49f52b76c4be",
        "19030a0354cb",
        "411512c530cd",
        "a5ead2b5cc2a",
        "a5ead2b5cc2a",
        None,
        "65c9d5bd84b1",
        "5f8bd31e2f1a",
        "b2f33be00e83",
    ],
    "c_other_ns": [
        "a3eedd12f01bf3d1737dcadb82632c40764a63e536a63ae1000a9c794db1fc8c",
        "0e78254c292d75107c3968ce3401dd11ddf900ac0a0111f0e9901c78d682b403",
        "1cc9984e443dc4fbfd68a06aed4bfe88115c8a6de0e41817957e2ffb6ca996c1",
        "1ccb39a7e512ed87d78fdfbe91c5bdc4aa975c0a8644555d1014a90d79a333e1",
        "e656fc783a5a8af141436f4be0772565e0d51634d72a14052073dd6a38d529c0",
        "e656fc783a5a8af141436f4be0772565e0d51634d72a14052073dd6a38d529c0",
        None,
        "b398bdf5d88c0d8a6480be0e869a35312ac21c8f3680d76d3aafcc5b8e474ed8",
        "0a550a65ac1fc37c67e64340fdbc19b6a67378b45b87b4f563cb24c9f9b35303",
        "51512084c1463a8ebf3dd7f622b570e609c8ec048d685e59f0f901d5cb4037c1",
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
        "fa7c1aa39564bba14948fb07b61e7ce2b445dfbb867b6f33b6119cc3f9e0afaa",
        "d1f93de247e8a5dc953f651a11dbebb9fa2d373f336c939629bc8d6147b6ebac",
        "a33e1672d99a0bf07a3b92e111de6e672148e01515b422e4da1935a47b05edc8",
        "590b94fac2b760fa415d5527983bd19e0010da29f1539d4a8e3924f0c6386438",
        "9fe4068ca604d454a4e0a15f0281453913707f5226d0ff38853b01bcf691c225",
        "2de8eb6759e59911b98601a11a05de756a9234f6f3a08609f48d4f14bcb6d7ab",
        "542fe4b5ed67ad9037f87f7b9c5df28f974ca9629fc66c8736e656055f0a095a",
        "a14e4bab1dfeb554524c2e392340c69c38c55eeec0d606261950122edcef9e0d",
        "4464491865721e1d7d722c8621b76243b5e5ae49a809a2251465af1bfbfdf65b",
        "7a77d369cfed9abd9291ee19271cb44781f58be784dcdf949881920b589ac758",
    ],
}
