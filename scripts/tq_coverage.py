"""In-process coverage runner for the Test-Quality Program (TQ).

Why this exists: `pytest --cov` breaks in this repo. coverage's `source=`
filtering reloads modules, and duckdb's compiled `_duckdb._sqltypes` submodule
does not survive the reload ("'_duckdb' is not a package"). Importing duckdb
BEFORE coverage starts fixes it, which the pytest-cov plugin cannot do (it
starts coverage at plugin load, before any conftest). This runner pre-imports
duckdb, then starts coverage, then runs pytest in-process.

TQ-3 owes the CI-side equivalent (a sitecustomize/.pth pre-import or a coverage
plugin) before the diff-coverage ratchet lands; until then, measure module
coverage with this.

Usage:
    uv run --frozen --extra dev --extra lint --extra vault \
        python scripts/tq_coverage.py <cov.source.module> <pytest-arg> [pytest-arg ...]

Example:
    ... python scripts/tq_coverage.py decoy_engine.relationships._graph \
        tests/unit/relationships tests/property/test_ri_graph_invariants.py -q
"""

from __future__ import annotations

import sys

import coverage
import duckdb  # noqa: F401  # MUST precede coverage.start(); see module docstring.


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    source = sys.argv[1]
    pytest_args = sys.argv[2:]

    cov = coverage.Coverage(source=[source], branch=True)
    cov.start()
    import pytest  # imported under coverage so the run is measured

    rc = pytest.main(pytest_args)
    cov.stop()
    cov.save()
    print(f"\n===== COVERAGE ({source}) =====")
    cov.report(show_missing=True)
    return int(rc)


if __name__ == "__main__":
    raise SystemExit(main())
