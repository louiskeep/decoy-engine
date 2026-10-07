"""Native gate for a bucket_perturb `date_format`: compile rejection implies native rejection.

The native admission gate shares the one date_format rule with plan compile, so
a format that cannot write a date back never reaches the native kernel either.
The native gate keeps its own extra declines (autodetect, timezone directives).
"""

from __future__ import annotations

from typing import Any

import pytest

from decoy_engine.execution.native._operator_config_rejections import (
    bucket_perturb_config_rejection,
)
from decoy_engine.plan import PlanCompileError, run_config_only_checks

_REJECTED_BY_COMPILE = [
    "ISO8601",
    "iso8601",
    "mixed",
    "YYYY-MM-DD",
    "foo",
    "%%Y",
    "%",
    "%H:%M:%S",
    "%f",
    "%z",
    "%Z",
    "%Q",
    "%%%%Y",
    "%Y %Q",
    "%Y%",
    0,
]


def _native(fmt: Any) -> str | None:
    return bucket_perturb_config_rejection(
        "c",
        "t",
        None,
        namespace="ns",
        provider_config={"bucket": "month", "date_format": fmt},
    )


def _compile_rejects(fmt: Any) -> bool:
    cfg = {
        "version": 1,
        "tables": [
            {
                "name": "t",
                "columns": [
                    {
                        "name": "c",
                        "strategy": "bucket_perturb",
                        "namespace": "ns",
                        "provider_config": {"bucket": "month", "date_format": fmt},
                    }
                ],
            }
        ],
    }
    try:
        run_config_only_checks(cfg)
    except PlanCompileError as exc:
        return exc.code == "bucket_perturb_date_format_unsupported"
    return False


@pytest.mark.parametrize("fmt", _REJECTED_BY_COMPILE, ids=[repr(f) for f in _REJECTED_BY_COMPILE])
def test_every_format_compile_rejects_is_rejected_natively(fmt: Any) -> None:
    assert _compile_rejects(fmt)
    assert _native(fmt) is not None


@pytest.mark.parametrize("fmt", ["mixed", "ISO8601"])
def test_pandas_special_names_keep_the_earlier_special_format_decline(fmt: str) -> None:
    assert _native(fmt) == "bucket_perturb_special_date_format:c"


@pytest.mark.parametrize("fmt", ["foo", "%Q", "%H:%M:%S"])
def test_the_native_reason_is_the_shared_rule_code(fmt: str) -> None:
    assert _native(fmt) == "bucket_perturb_date_format_unsupported:c"


@pytest.mark.parametrize("fmt", ["%Y-%m-%d", "%d/%m/%Y", "%Y", "100%% %Y", "%Y-%m-%d %f"])
def test_a_writable_format_is_still_admitted(fmt: str) -> None:
    assert not _compile_rejects(fmt)
    assert _native(fmt) is None


@pytest.mark.parametrize("fmt", [None, ""])
def test_autodetect_keeps_its_own_native_decline(fmt: str | None) -> None:
    assert _native(fmt) == "bucket_perturb_requires_date_format:c"


@pytest.mark.parametrize("fmt", ["%Y-%m-%d%z", "%Y-%m-%d %Z", "%z", "%Z%Y-%m-%d"])
def test_timezone_directive_keeps_its_own_native_decline(fmt: str) -> None:
    assert _native(fmt) == "bucket_perturb_timezone_directive:c"
