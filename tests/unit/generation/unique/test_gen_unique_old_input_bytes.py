"""GEN-UNIQUE AT-8 (E1): configs without the new keys keep their bytes.

The pinned digests were captured on engine main (`6bdd4840`) before the E1 refactors
(`pool_opted_out`, `parent_keys`, the shared refusal builder) touched any generation code. A
green run says generated cells, the Plan's embedded `config_json` and every column's seed
fingerprint are what they were. Do not re-pin to make this pass: a moved digest is output drift
(U3) and is fixed at its source.

Re-pinned `cells` for three corpus configs when generated numeric columns became int64 (GEN-C2):
rendering each numeric column back to text reproduces the old digests, so only the cell type
moved. `config_json` and `fingerprints` are untouched.
"""

from __future__ import annotations

import pytest

from decoy_engine.config._pipeline import PipelineConfig
from tests.unit.generation.unique._gu_corpus import CORPUS, digests

PINNED = {
    "all_basic_types": {
        "cells": "c7ea7449a95cb1740c336f96421aa53acb924b3760ec007f1bea1e81f5434fe6",
        "config_json": "c82643e094caf6665bf2a292cda6561eb89766a1292e244a818c86ce2b45f074",
        "fingerprints": "d25f1cc532c7beae91e731d26bcb92d754e62641192ee872bd4097a74a458fdd",
    },
    "all_basic_types_unlisted_formula": {
        "cells": "1f5c4a2f626e7917e8ba2b4978ccac968c7f9234076364d40a9cfe74ef975cca",
        "config_json": "1a7a3630c78e0ea1f924f26b6d12248be77aada420e34bc1eccfe9f42f823351",
        "fingerprints": "42e2831b6ec3f309cba7df47c4a6d7e38bec722d64da7aa09d9408059ac6c72e",
    },
    "pooled_and_opted_out": {
        "cells": "090b25b7223a2c086173164bfa22ed505f9e3fe5ee3f33626c7486273a672a4e",
        "config_json": "112f0e19dfdb6c968665c45966d171e4ef2014fcbd1b4a6195006f5ebb7e5994",
        "fingerprints": "d67f68757ce19ce25611dc593bcfa666faa87ef11ad98a035df2f247160493db",
    },
    "reference_shapes": {
        "cells": "d2d6fef3a62e972cf3b7de53a426f9466db94bc4228c83ed572439eb80b109ee",
        "config_json": "340574619a7f541f441d255d4d46b8a46944c081cb2c8b8249504b00be7163d5",
        "fingerprints": "1d9c6f66c834e768ed3e14db1dfc81e326df6352d69768863cf156391c4d5361",
    },
    "reference_to_sparse_parent": {
        "cells": "73072f2424b22ae2468badd3d7ef1095ced76cdac67e7816fdc288ac872449f8",
        "config_json": "e4cd641dbe9b3acc1c287b5f6918a45a580156c9d71f282a50956c106ca207a4",
        "fingerprints": "52c95b54d59718ec5e4cb993dc9110e9efec9fdef9cf50563c0917527d058c68",
    },
}


def test_pins_cover_the_whole_corpus():
    assert set(PINNED) == set(CORPUS)


@pytest.mark.golden
@pytest.mark.usefixtures("lenient_generate_checks")
@pytest.mark.parametrize("name", sorted(PINNED))
def test_cells_plan_and_fingerprints_are_unchanged(name):
    got = digests(name)
    assert got["cells"] == PINNED[name]["cells"], "generated cells drifted"
    assert got["config_json"] == PINNED[name]["config_json"], "serialized Plan config drifted"
    assert got["fingerprints"] == PINNED[name]["fingerprints"], "seed fingerprints drifted"


def test_config_json_has_no_unique_keys():
    # M9: dumping `unique_together: []` (or `unique`) into an old config's Plan turns this red.
    cfg = PipelineConfig.model_validate(CORPUS["reference_shapes"]).model_dump()
    assert "unique_together" not in repr(cfg)
    assert "'unique'" not in repr(cfg)


def test_unique_keys_are_accepted_once_wired():
    # E1 pinned these keys as refused until the wiring slice; E2 is that slice, so they now validate.
    col = {"name": "c", "type": "sequence", "start": 1, "unique": True}
    cfg = {
        **CORPUS["all_basic_types"],
        "tables": [{"name": "t", "row_count": 3, "generate_columns": [col]}],
        "targets": {"t": {"type": "file", "format": "csv", "path": "o.csv"}},
    }
    assert PipelineConfig.model_validate(cfg)

    plain = {"name": "c", "type": "sequence", "start": 1}
    table = {"name": "t", "row_count": 3, "unique_together": [], "generate_columns": [plain]}
    assert (
        "unique_together"
        not in PipelineConfig.model_validate({**cfg, "tables": [table]}).model_dump()["tables"][0]
    )


def test_serialization_schema_still_lists_the_table_fields():
    schema = PipelineConfig.model_json_schema(mode="serialization")
    defs = schema.get("$defs", {})
    table = next(d for n, d in defs.items() if n.endswith("TableConfig"))
    assert {"name", "row_count", "generate_columns"} <= set(table["properties"])
