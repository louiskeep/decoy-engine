"""Mutation runner for the typed out-of-core temporal FK seam (slice 0 item 2).

Each mutant replaces one exact source snippet, runs the seam test set, and restores the file
from memory (the restore is checked by hash). A mutant is proven when the run is red. The runner
refuses to start while a target file has uncommitted changes, so a mutation can never be
committed by accident.

    python scripts/dev/ooc_fk_seam_mutants.py              # every mutant, -n 4 when xdist is present
    python scripts/dev/ooc_fk_seam_mutants.py R3-1 R1-M9   # selected ids
    python scripts/dev/ooc_fk_seam_mutants.py --list

Every round-1 mutant (R1-*) is RECONSTRUCTED: the round-1 script was not committed, so each edit
is rebuilt from its recorded name and the plan's class-table mutation column. `basis` says how
close it is: "exact" when the name or plan defines a single edit and the runner applies it (moved
code adjusted only), "approximate" when the name admits several edits and the runner picks one.
Round 2 (R2-*) and round 3 (R3-*) are the originals ("original").
Evidence: docs/plans/evidence/2026-10-31-slice0-ooc-red-baseline.txt.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OOC = "src/decoy_engine/execution/out_of_core"
JOIN = f"{OOC}/_join.py"
BATCH = f"{OOC}/_batch_join.py"
EMIT = f"{OOC}/_emit.py"
ROUTER = "src/decoy_engine/execution/_pipeline_routing_fk_dtype.py"
KEYS = "src/decoy_engine/execution/_fk_keys.py"

SEAM_TESTS = (
    "tests/parity/test_out_of_core_fk_parity.py",
    "tests/unit/execution/test_out_of_core_join_chunked.py",
    "tests/unit/execution/test_pipeline_routing_fk_dtype.py",
    "tests/property/test_fk_keys_invariants.py",
    "tests/unit/execution/test_ooc_fk_seam_round2.py",
    "tests/parity/test_ooc_temporal_ns_range.py",
    "tests/unit/execution/test_ooc_fk_seam_round3.py",
)


@dataclass(frozen=True)
class Mutant:
    id: str
    what: str
    path: str
    old: str
    new: str
    basis: str


_IDENTITY = "r != m and not _identity_temporal_promotion(r, m, seed)"
_BUILD_ALL = (
    "    arrays = [\n"
    "        fk_output_array(component, None if output_types is None else output_types[idx])\n"
    "        for idx, component in enumerate(out)\n"
    "    ]\n"
    "    for idx, array in enumerate(arrays):\n"
    "        output_chunks[idx].append(array)\n"
)
_APPEND_EACH = (
    "    for idx, component in enumerate(out):\n"
    "        output_chunks[idx].append(pa.array(component, from_pandas=True))\n"
    "        fk_output_array(component, None if output_types is None else output_types[idx])\n"
)
_CAST_TAIL = (
    "    except MemoryError:\n"
    "        raise\n"
    "    except pa.ArrowException as exc:\n"
    "        failed = type(exc)\n"
    '    raise failed(f"cannot cast an FK key chunk {chunk.type} -> {target} exactly")'
)

MUTANTS: tuple[Mutant, ...] = (
    Mutant(
        "R1-M1",
        "join token keeps the class-tagged OBJ path for datetimes",
        KEYS,
        "if normalized.tzinfo is None and normalized == normalized:",
        "if False:",
        "exact",
    ),
    Mutant(
        "R1-M2",
        "timestamp round-trip image back to us",
        BATCH,
        "        return dtype\n    if pa.types.is_date32(dtype):",
        '        return pa.timestamp("us")\n    if pa.types.is_date32(dtype):',
        "exact",
    ),
    Mutant(
        "R1-M3",
        "unify to the coarsest unit",
        BATCH,
        "return max(non_null, key=lambda dtype: _UNIT_RANK[dtype.unit])",
        "return min(non_null, key=lambda dtype: _UNIT_RANK[dtype.unit])",
        "exact",
    ),
    Mutant(
        "R1-M4",
        "child candidate added under FAIL",
        BATCH,
        "if policy in (OrphanPolicy.PRESERVE, OrphanPolicy.WARN):",
        "if policy in (OrphanPolicy.PRESERVE, OrphanPolicy.WARN, OrphanPolicy.FAIL):",
        "exact",
    ),
    Mutant(
        "R1-M5",
        "REMAP input typed for every policy (child key typed up front under FAIL too)",
        BATCH,
        "        candidates = {_python_roundtrip_type(masked_types[idx])}\n",
        "        candidates = {_python_roundtrip_type(masked_types[idx])}\n"
        "        _fk_key_value_roundtrip_type(child_key_types[idx])\n",
        "approximate",
    ),
    Mutant(
        "R1-M6a",
        "REMAP mint by inference",
        BATCH,
        "fk_output_array(normalized, dtype if temporal else None)",
        "fk_output_array(normalized, None)",
        "exact",
    ),
    Mutant(
        "R1-M6b",
        "zero-row table by inference",
        EMIT,
        "fk_output_array([], joiner.output_types[component])",
        "pa.array([], from_pandas=True)",
        "exact",
    ),
    Mutant(
        "R1-M7a",
        "exactness check dropped",
        JOIN,
        "(temporal and not _holds_exactly(values, array))",
        "False",
        "exact",
    ),
    Mutant(
        "R1-M7b",
        "unsafe temporal construction",
        JOIN,
        "array = pa.array(values, type=target, from_pandas=True)",
        "array = pa.array(values, type=target, from_pandas=True, safe=False)",
        "exact",
    ),
    Mutant(
        "R1-M8",
        "coded refusal raised inside the except, from the Arrow error",
        JOIN,
        "    except (pa.ArrowException, OverflowError):\n        array = None",
        "    except (pa.ArrowException, OverflowError) as exc:\n"
        "        raise _unrepresentable(target) from exc",
        "exact",
    ),
    Mutant("R1-M9", "router identity exemption dropped", ROUTER, _IDENTITY, "r != m", "exact"),
    Mutant(
        "R1-M10",
        "exemption ignores the identity requirement",
        ROUTER,
        '(seed is None or getattr(seed, "strategy", None) == "passthrough")',
        "True",
        "exact",
    ),
    Mutant(
        "R1-M11",
        "exemption admits any type family over an identity parent",
        ROUTER,
        "        and all(pa.types.is_timestamp(t) and t.tz is None for t in (resolved, matched))\n"
        "        and _UNIT_RANK[resolved.unit] > _UNIT_RANK[matched.unit]\n",
        "",
        "approximate",
    ),
    Mutant(
        "R1-M12",
        "append an inferred array before the typed build",
        JOIN,
        _BUILD_ALL,
        _APPEND_EACH,
        "approximate",
    ),
    Mutant(
        "R1-M13",
        "NaT no longer excluded from the TS join token",
        KEYS,
        "if normalized.tzinfo is None and normalized == normalized:",
        "if normalized.tzinfo is None:",
        "exact",
    ),
    Mutant(
        "R1-M14",
        "batch joiner left on inference",
        BATCH,
        "output_types=self._output_types,",
        "output_types=None,",
        "approximate",
    ),
    Mutant(
        "R2-N1",
        "cast re-raises the Arrow text",
        JOIN,
        "    except pa.ArrowException as exc:\n        failed = type(exc)\n    raise failed(",
        "    except pa.ArrowException as exc:\n        raise\n    raise failed(",
        "original",
    ),
    Mutant(
        "R2-N2",
        "cast chains the original Arrow error",
        JOIN,
        _CAST_TAIL,
        "    except MemoryError:\n        raise\n    except pa.ArrowException as exc:\n"
        '        raise type(exc)(f"cannot cast an FK key chunk {chunk.type} -> {target}'
        ' exactly") from exc',
        "original",
    ),
    Mutant(
        "R2-N3",
        "seam catches only ArrowInvalid/ArrowTypeError",
        JOIN,
        "    except (pa.ArrowException, OverflowError):\n        array = None",
        "    except (pa.ArrowInvalid, pa.ArrowTypeError, OverflowError):\n        array = None",
        "original",
    ),
    Mutant(
        "R2-N4",
        "router identity exemption dropped (as R1-M9)",
        ROUTER,
        _IDENTITY,
        "r != m",
        "original",
    ),
    Mutant(
        "R2-N5",
        "promotion to ns diverted",
        ROUTER,
        "        and _UNIT_RANK[resolved.unit] > _UNIT_RANK[matched.unit]\n",
        "        and _UNIT_RANK[resolved.unit] > _UNIT_RANK[matched.unit]\n"
        '        and resolved.unit != "ns"\n',
        "original",
    ),
    Mutant(
        "R2-N6",
        "whole-child oracle publishes the source unit",
        JOIN,
        "result.set_column(child_idx, child_col, _concat_fk_chunks(output_chunks[idx]))",
        "result.set_column(child_idx, child_col, _concat_fk_chunks(output_chunks[idx])"
        ".cast(source_table.column(child_col).type))",
        "original",
    ),
    Mutant(
        "R3-1",
        "seam handler swallows MemoryError again",
        JOIN,
        "    except MemoryError:\n"
        "        # `pa.ArrowMemoryError` is also an ArrowException: exhaustion is not a key type.\n"
        "        raise\n",
        "",
        "original",
    ),
    Mutant(
        "R3-2",
        "cast handler re-labels MemoryError again",
        JOIN,
        "    except MemoryError:\n        raise\n    except pa.ArrowException as exc:",
        "    except pa.ArrowException as exc:",
        "original",
    ),
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _dirty(paths: set[str]) -> list[str]:
    out = subprocess.run(  # noqa: S603 - fixed git argv over repo-relative paths
        ["git", "status", "--porcelain", "--", *sorted(paths)],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [line for line in out.splitlines() if line.strip()]


def run(mutant: Mutant, workers: int) -> tuple[bool, str]:
    path = ROOT / mutant.path
    original = path.read_text(encoding="utf-8")
    if original.count(mutant.old) != 1:
        return False, "SNIPPET NOT FOUND EXACTLY ONCE"
    argv = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *SEAM_TESTS]
    if workers > 1:
        argv += ["-n", str(workers)]
    env = dict(os.environ, PYTHONPATH=f"{ROOT / 'src'}{os.pathsep}{ROOT / 'scripts'}")
    path.write_text(original.replace(mutant.old, mutant.new, 1), encoding="utf-8")
    try:
        result = subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, text=True)  # noqa: S603
    finally:
        path.write_text(original, encoding="utf-8")
    if _sha(path.read_text(encoding="utf-8")) != _sha(original):
        raise SystemExit(f"restore of {mutant.path} failed; stop and inspect the tree")
    lines = result.stdout.splitlines()
    tally = next((line for line in reversed(lines) if " passed" in line or " failed" in line), "")
    reds = sorted(
        {line.split("::")[1].split("[")[0] for line in lines if line.startswith("FAILED")}
    )
    return (
        result.returncode != 0,
        f"{tally} red: {', '.join(reds[:4])}{' ...' if len(reds) > 4 else ''}",
    )


def main(argv: list[str]) -> int:
    if "--list" in argv:
        for m in MUTANTS:
            print(f"{m.id}\t{m.basis}\t{m.path}\t{m.what}")
        return 0
    chosen = [m for m in MUTANTS if not argv or m.id in argv]
    dirty = _dirty({m.path for m in chosen})
    if dirty:
        print("refusing: target files have uncommitted changes:\n" + "\n".join(dirty))
        return 2
    workers = 4 if importlib.util.find_spec("xdist") else 1
    survivors = 0
    for m in chosen:
        red, detail = run(m, workers)
        survivors += not red
        verdict = "RED " if red else "GREEN"
        print(f"{m.id}\t{m.basis}\t{verdict}\t{m.what}\t{detail}", flush=True)
    print(f"{len(chosen) - survivors}/{len(chosen)} red")
    return 1 if survivors else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
