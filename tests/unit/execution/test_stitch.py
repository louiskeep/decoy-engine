"""Task 4.6 slice 5b-i acceptance A1: direct characterization of
`stitch_generate_mask_outputs`, the single shared owner of the generate+mask
output-stitch precedence `run_pipeline` (`_pipeline.py`) and the physical-
plan shadow coordinator's independent-mixed dispatch
(`execution/physical/_shadow_mixed.py`) both call.

The mixed-job `run_pipeline` byte-equality regression this refactor must
stay behavior-preserving for lives in `test_run_pipeline.py`'s
`TestRunPipelineMixed`/`TestRunPipelineDeterminism` classes (already
exercising a real mixed config's stitched output before and after this
extraction, unmodified by this slice); this file covers the FUNCTION itself
in isolation, which those integration tests cannot pin directly.
"""

from __future__ import annotations

import pyarrow as pa

from decoy_engine.execution._stitch import stitch_generate_mask_outputs


def test_mask_wins_a_name_collision() -> None:
    generate_table = pa.table({"v": [1, 2]})
    mask_table = pa.table({"v": [9, 9]})
    outputs = stitch_generate_mask_outputs({"t": generate_table}, {"t": mask_table})
    assert outputs["t"] is mask_table


def test_key_order_is_generate_first_then_mask_appended() -> None:
    generate_outputs = {"b": pa.table({"v": [1]}), "a": pa.table({"v": [2]})}
    mask_outputs = {"c": pa.table({"v": [3]})}
    outputs = stitch_generate_mask_outputs(generate_outputs, mask_outputs)
    assert list(outputs) == ["b", "a", "c"]


def test_tie_overwrite_does_not_reposition_the_key() -> None:
    """A name present on BOTH sides keeps its GENERATE-side position in the
    result -- `dict.update` replaces a value in place, it never moves the
    key to the end -- so mask winning the VALUE never changes the UNION's
    key order."""
    generate_outputs = {"a": pa.table({"v": [1]}), "shared": pa.table({"v": [2]})}
    mask_outputs = {"shared": pa.table({"v": [9]}), "z": pa.table({"v": [3]})}
    outputs = stitch_generate_mask_outputs(generate_outputs, mask_outputs)
    assert list(outputs) == ["a", "shared", "z"]
    assert outputs["shared"] is mask_outputs["shared"]


def test_table_object_identity_is_preserved_not_copied() -> None:
    generate_table = pa.table({"v": [1]})
    mask_table = pa.table({"v": [2]})
    outputs = stitch_generate_mask_outputs({"g": generate_table}, {"m": mask_table})
    assert outputs["g"] is generate_table
    assert outputs["m"] is mask_table


def test_pure_generate_and_pure_mask_pass_through_unchanged() -> None:
    generate_table = pa.table({"v": [1]})
    assert stitch_generate_mask_outputs({"g": generate_table}, {}) == {"g": generate_table}
    mask_table = pa.table({"v": [2]})
    assert stitch_generate_mask_outputs({}, {"m": mask_table}) == {"m": mask_table}
    assert stitch_generate_mask_outputs({}, {}) == {}
