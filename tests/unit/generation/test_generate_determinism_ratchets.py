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
from tests.unit._dps_helpers import compile_and_generate


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
from datetime import datetime, timezone
from decoy_engine.config._pipeline import PipelineConfig
from decoy_engine.generation.synthesize import generate_tables
from decoy_engine.plan import compile_plan
from decoy_engine.profile import Profile

cfg = PipelineConfig.model_validate(json.loads(sys.argv[1])).model_dump()
profile = Profile(
    schema_version=1,
    tables=(),
    relationships=(),
    profiled_at=datetime.now(timezone.utc),
    decoy_engine_version="test",
)
plan = compile_plan(cfg, profile, decoy_engine_version="test")
tables = generate_tables(plan)
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
        parent = _digest_tables(compile_and_generate(cfg))

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
        assert _digest_tables(compile_and_generate(cfg)) == _digest_tables(
            compile_and_generate(cfg)
        )


class TestCrossColumnIndependence:
    def test_same_domain_distinct_fingerprint_columns_are_not_row_shifted(self):
        # F3 regression: with column_seed + i, two columns with adjacent base
        # seeds produced row-shifted-identical streams. These two categoricals
        # draw from the SAME 8-value domain (so a row-shift is observable in
        # real output) but have DISTINCT fingerprints (col `b` carries explicit
        # uniform weights), so their roots -- and therefore their per-row
        # streams -- must be independent: not equal and not aligned under any
        # small shift. Picked same-domain on purpose: provider-different columns
        # (e.g. first_name vs last_name) would differ even under the old bug, so
        # they cannot witness an F3 regression.
        cats = ["A", "B", "C", "D", "E", "F", "G", "H"]
        cfg = {
            "version": 1,
            "global_settings": {"seed": 42},
            "sources": {},
            "tables": [
                {
                    "name": "t",
                    "row_count": 48,
                    "generate_columns": [
                        {"name": "a", "type": "categorical", "categories": cats},
                        {
                            "name": "b",
                            "type": "categorical",
                            "categories": cats,
                            "weights": [1] * len(cats),
                        },
                    ],
                }
            ],
            "targets": {"t": {"type": "file", "format": "csv", "path": "out.csv"}},
        }
        cfg = PipelineConfig.model_validate(cfg).model_dump()
        t = compile_and_generate(cfg)["t"]
        a = t.column("a").to_pylist()
        b = t.column("b").to_pylist()
        assert a != b
        for k in range(1, 5):
            assert a[k:] != b[: len(a) - k]
            assert b[k:] != a[: len(b) - k]

    def test_null_prob_column_is_deterministic_across_runs(self):
        # The numpy-family null mask must be stable run-to-run (and, per the
        # lockstep fix, identical between engines -- covered by the parity oracle).
        cfg = PipelineConfig.model_validate(_multi_col_config()).model_dump()
        a = compile_and_generate(cfg)["people"].column("score").to_pylist()
        b = compile_and_generate(cfg)["people"].column("score").to_pylist()
        assert a == b
        assert any(v is None for v in a)  # null injection actually fired
