"""F6 ratchets for the v6 F2/F3 generation-determinism rewrite.

Locks the end-to-end (generate_tables) invariants the GenDeriveContext fix
restores, complementing the primitive-level tests in
tests/unit/generators/test_gen_derive_context.py:

- across-processes byte-identity (catches process-local seed leakage that
  the in-process parity oracle cannot see),
- generate-twice in-process byte-stability,
- cross-column independence (the F3 `column_seed + i` correlation is gone)
  observed through real generated output, not just the seed ints.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys

import pytest

from decoy_engine.config._pipeline import PipelineConfig
from decoy_engine.generation.synthesize import generate_tables


def _multi_col_config(row_count: int = 24) -> dict:
    """A config exercising every per-column RNG family: sequence (none),
    faker (faker family), categorical (py family), a numeric faker with a
    null_probability (numpy family), and a second distinct faker column so
    cross-column independence is observable."""
    return {
        "version": 1,
        "global_settings": {"seed": 42},
        "sources": {},
        "tables": [
            {
                "name": "people",
                "row_count": row_count,
                "generate_columns": [
                    {"name": "id", "type": "sequence", "start": 1000, "step": 1},
                    {"name": "first", "type": "faker", "faker_type": "first_name"},
                    {"name": "last", "type": "faker", "faker_type": "last_name"},
                    {"name": "tier", "type": "categorical", "categories": ["A", "B", "C"]},
                    {
                        "name": "score",
                        "type": "faker",
                        "faker_type": "pyint",
                        "null_probability": 0.3,
                    },
                ],
            }
        ],
        "targets": {"people": {"type": "file", "format": "csv", "path": "out.csv"}},
    }


def _digest_tables(tables: dict) -> str:
    """Stable hash over every table's Arrow bytes, table-name sorted."""
    h = hashlib.sha256()
    for name in sorted(tables):
        h.update(name.encode("utf-8"))
        # to_pylist over each column gives a stable, comparable representation
        # independent of Arrow buffer padding.
        t = tables[name]
        for col in t.column_names:
            h.update(col.encode("utf-8"))
            h.update(repr(t.column(col).to_pylist()).encode("utf-8"))
    return h.hexdigest()


_CHILD_SCRIPT = """
import json, sys, hashlib
from decoy_engine.config._pipeline import PipelineConfig
from decoy_engine.generation.synthesize import generate_tables

cfg = PipelineConfig.model_validate(json.loads(sys.argv[1])).model_dump()
tables = generate_tables(cfg)
h = hashlib.sha256()
for name in sorted(tables):
    h.update(name.encode("utf-8"))
    t = tables[name]
    for col in t.column_names:
        h.update(col.encode("utf-8"))
        h.update(repr(t.column(col).to_pylist()).encode("utf-8"))
sys.stdout.write(h.hexdigest())
"""


@pytest.mark.golden
class TestGenerateProcessStability:
    def test_subprocess_generates_byte_identical_tables(self):
        import json

        cfg = PipelineConfig.model_validate(_multi_col_config()).model_dump()
        parent = _digest_tables(generate_tables(cfg))

        child = subprocess.run(  # noqa: S603 -- args are test literals
            [sys.executable, "-c", _CHILD_SCRIPT, json.dumps(_multi_col_config())],
            capture_output=True,
            text=True,
            check=True,
        )
        assert child.stdout.strip() == parent, (
            "generate_tables process-stability drift: a generated column reads "
            "process-local state (PID, thread-local, module-global RNG) that "
            "defeats the determinism contract."
        )


class TestGenerateInProcessStability:
    def test_generate_twice_is_byte_identical(self):
        cfg = PipelineConfig.model_validate(_multi_col_config()).model_dump()
        assert _digest_tables(generate_tables(cfg)) == _digest_tables(generate_tables(cfg))


class TestCrossColumnIndependence:
    def test_distinct_faker_columns_are_not_row_shifted(self):
        # F3 regression: with column_seed + i, two adjacent faker columns
        # produced row-shifted-identical streams. `first` and `last` draw from
        # different providers AND different fingerprints; their per-row values
        # must not align under any small row shift.
        cfg = PipelineConfig.model_validate(_multi_col_config(row_count=40)).model_dump()
        t = generate_tables(cfg)["people"]
        first = t.column("first").to_pylist()
        last = t.column("last").to_pylist()
        assert first != last
        for k in range(1, 4):
            assert first[k:] != last[: len(first) - k]
            assert last[k:] != first[: len(last) - k]

    def test_null_prob_column_is_deterministic_across_runs(self):
        # The numpy-family null mask must be stable run-to-run (and, per the
        # lockstep fix, identical between engines -- covered by the parity oracle).
        cfg = PipelineConfig.model_validate(_multi_col_config()).model_dump()
        a = generate_tables(cfg)["people"].column("score").to_pylist()
        b = generate_tables(cfg)["people"].column("score").to_pylist()
        assert a == b
        assert any(v is None for v in a)  # null injection actually fired
