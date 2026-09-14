"""Task 4.4 C7: no-publish safety.

`ShadowCoordinator` has no sink/publisher/target dependency at all (proved
structurally in `test_shadow_coordinator.py`); this file proves it
DYNAMICALLY: every transactional-sink method is poisoned, a full shadow run
goes through both the shadow coordinator and the pinned oracle, and none of
the poisoned methods ever fire. It also asserts no target descriptor ever
reaches `ShadowCoordinator`/`ShadowContext`, and that the oracle's own
required (but inert, `sink=None`) target writes nothing to disk.

The static regex sweep + fresh-import check for the whole `execution.
physical` package already live in `tests/sentry/test_physical_seam_
disconnection.py` (Task 4.2/4.3) and need no change: they already guard
every current production route, and the new Task 4.4 modules live inside
`execution/physical/`, so the existing "nothing outside physical/ imports
physical/" sweep already covers them.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution._transactional_sink import (
    ParquetTransactionalSink,
    _CallableSinkAdapter,
)
from decoy_engine.execution.physical._plan import ExecutionBinding
from decoy_engine.execution.physical._shadow_context import ShadowContext
from decoy_engine.execution.physical._shadow_coordinator import ShadowCoordinator
from tests.physical._shadow_helpers import (
    build_config,
    run_shadow_and_oracle,
    write_read_only_fixture,
)

_POISONED_METHODS = ("write", "write_batches", "commit", "abort")


class _SinkInvokedError(AssertionError):
    """A distinct type so a failure here is unambiguous about what broke."""


@pytest.fixture(autouse=True)
def _poison_every_transactional_sink_method(monkeypatch: pytest.MonkeyPatch) -> None:
    def _bomb(*args: Any, **kwargs: Any) -> Any:
        raise _SinkInvokedError("a transactional-sink method fired during a shadow run")

    for cls in (ParquetTransactionalSink, _CallableSinkAdapter):
        for name in _POISONED_METHODS:
            if hasattr(cls, name):
                monkeypatch.setattr(cls, name, _bomb)


def test_shadow_and_oracle_run_never_touch_a_poisoned_sink_method(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    path = write_read_only_fixture(tmp_path, source, "t")
    config = build_config(tmp_path, "t", path, [{"name": "c", "strategy": "redact"}])

    run = run_shadow_and_oracle(config, "t", source)

    assert run.shadow.outputs["t"].num_rows == 3
    assert run.oracle.outputs["t"].num_rows == 3
    # No file the oracle's declared (but sink=None) target names was ever
    # created -- the oracle returns the resident output only.
    target_path = tmp_path / "t.out.parquet"
    assert not target_path.exists()


def test_no_target_descriptor_parameter_reaches_shadow_context_or_coordinator() -> None:
    for cls in (ShadowContext, ShadowCoordinator):
        params = set(inspect.signature(cls.__init__).parameters)
        for forbidden in ("target", "target_path", "sink", "targets"):
            assert forbidden not in params, f"{cls.__name__}.__init__ must not accept {forbidden!r}"
    run_params = set(inspect.signature(ShadowCoordinator.run).parameters)
    for forbidden in ("target", "target_path", "sink", "targets"):
        assert forbidden not in run_params


def test_execution_binding_never_carries_a_target_or_path_field() -> None:
    """Belt-and-suspenders: the frozen per-node binding itself has no field
    a future change could accidentally repurpose into a write destination."""
    field_names = {f for f in ExecutionBinding.__dataclass_fields__}
    for forbidden in ("target", "target_path", "sink", "output_path", "write_path"):
        assert forbidden not in field_names
