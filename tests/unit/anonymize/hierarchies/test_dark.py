"""Dark: importing the engine does not load the package, and nothing public names it."""

from __future__ import annotations

import subprocess
import sys

import decoy_engine
import decoy_engine.anonymize as anonymize
import decoy_engine.anonymize.hierarchies as hierarchies
from decoy_engine.execution._strategies import strategy_catalog


def test_engine_import_does_not_load_anonymize() -> None:
    code = "import sys, decoy_engine; print('decoy_engine.anonymize' in sys.modules)"
    result = subprocess.run(  # noqa: S603 -- args are test literals
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "False"


def test_no_anonymize_name_in_the_engine_api_or_catalog() -> None:
    ours = set(anonymize.__all__) | set(hierarchies.__all__) | {"anonymize", "hierarchies"}
    assert not ours & set(decoy_engine.__all__)
    kinds = {"interval", "band", "prefix", "date", "category"}
    for row in strategy_catalog():
        assert row.name not in ours | kinds
        assert "hierarch" not in row.name and "anonymi" not in row.name
