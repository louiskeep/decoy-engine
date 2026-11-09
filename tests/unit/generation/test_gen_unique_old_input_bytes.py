"""GEN-UNIQUE AT-8 (E1): configs without the new keys keep their bytes.

The pinned digests were captured on engine main (`6bdd4840`) before the E1 refactors
(`pool_opted_out`, `parent_keys`, the shared refusal builder) touched any generation code. A
green run says generated cells, the Plan's embedded `config_json` and every column's seed
fingerprint are what they were. Do not re-pin to make this pass: a moved digest is output drift
(U3) and is fixed at its source.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from decoy_engine import reasons
from decoy_engine.config._pipeline import PipelineConfig
from tests.unit.generation._gu_corpus import CORPUS, digests

PINNED = {
    "all_basic_types": {
        "cells": "736bb7ab5fd4cc4c92f01753bfe52c4d0b3f8f39d28f0affd989527e147a511e",
        "config_json": "1a7a3630c78e0ea1f924f26b6d12248be77aada420e34bc1eccfe9f42f823351",
        "fingerprints": "42e2831b6ec3f309cba7df47c4a6d7e38bec722d64da7aa09d9408059ac6c72e",
    },
    "pooled_and_opted_out": {
        "cells": "090b25b7223a2c086173164bfa22ed505f9e3fe5ee3f33626c7486273a672a4e",
        "config_json": "112f0e19dfdb6c968665c45966d171e4ef2014fcbd1b4a6195006f5ebb7e5994",
        "fingerprints": "d67f68757ce19ce25611dc593bcfa666faa87ef11ad98a035df2f247160493db",
    },
    "reference_shapes": {
        "cells": "cf5b75d46a16dc727a6e7b2cc893525823aad608c921b68d1f726df7ce1f0185",
        "config_json": "340574619a7f541f441d255d4d46b8a46944c081cb2c8b8249504b00be7163d5",
        "fingerprints": "1d9c6f66c834e768ed3e14db1dfc81e326df6352d69768863cf156391c4d5361",
    },
    "reference_to_sparse_parent": {
        "cells": "52bc517b47d08c550cdd648ac1e739f05f34f5bb943f38c9f4fcf08a16e66679",
        "config_json": "e4cd641dbe9b3acc1c287b5f6918a45a580156c9d71f282a50956c106ca207a4",
        "fingerprints": "52c95b54d59718ec5e4cb993dc9110e9efec9fdef9cf50563c0917527d058c68",
    },
}


def test_pins_cover_the_whole_corpus():
    assert set(PINNED) == set(CORPUS)


@pytest.mark.golden
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


def test_unique_keys_are_still_refused_until_the_wiring_slice():
    # Every head stays runnable: before the wiring slice the keys are unknown, as today.
    col = {"name": "c", "type": "sequence", "start": 1, "unique": True}
    cfg = {
        **CORPUS["all_basic_types"],
        "tables": [{"name": "t", "row_count": 3, "generate_columns": [col]}],
        "targets": {"t": {"type": "file", "format": "csv", "path": "o.csv"}},
    }
    with pytest.raises(ValidationError) as exc:
        PipelineConfig.model_validate(cfg)
    assert {e["type"] for e in exc.value.errors()} == {str(reasons.config.GENERATE_OPTION_UNKNOWN)}

    plain = {"name": "c", "type": "sequence", "start": 1}
    table = {"name": "t", "row_count": 3, "unique_together": [], "generate_columns": [plain]}
    with pytest.raises(ValidationError):
        PipelineConfig.model_validate({**cfg, "tables": [table]})


def test_serialization_schema_still_lists_the_table_fields():
    schema = PipelineConfig.model_json_schema(mode="serialization")
    defs = schema.get("$defs", {})
    table = next(d for n, d in defs.items() if n.endswith("TableConfig"))
    assert {"name", "row_count", "generate_columns"} <= set(table["properties"])
