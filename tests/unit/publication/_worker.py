"""Out-of-process publisher: `_worker.py <target> <operation_id> <variant> <fault> [go_file]`.

Faults kill the process with SIGKILL at a real I/O boundary (not a mock of the outcome):
kill_after_stage, kill_before_rename, kill_after_rename (immutable unit) and
kill_before_switch / kill_after_switch (managed unit: the CURRENT pointer replace).
Env: WORKER_UNIT=managed, WORKER_REPLACE=<expected descriptor digest>, WORKER_BACKUP=<ref>.
A go_file makes racing workers start together.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

import pyarrow as pa

from decoy_engine import publication as pub
from decoy_engine.execution._transactional_sink import ManagedDatasetSink, ParquetTransactionalSink

target, op, variant, fault = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
if len(sys.argv) > 5:
    go = Path(sys.argv[5])
    while not go.exists():
        time.sleep(0.005)


def die() -> None:
    os.kill(os.getpid(), signal.SIGKILL)


real_replace = os.replace


def _is_switch(dst: object) -> bool:
    return Path(str(dst)).name == "CURRENT"


if fault in ("kill_before_rename", "kill_before_switch"):

    def _before(src: object, dst: object) -> None:
        if fault == "kill_before_rename" or _is_switch(dst):
            die()
        real_replace(src, dst)  # type: ignore[arg-type]

    os.replace = _before  # type: ignore[assignment]
elif fault in ("kill_after_rename", "kill_after_switch"):

    def _after(src: object, dst: object) -> None:
        real_replace(src, dst)  # type: ignore[arg-type]
        if fault == "kill_after_rename" or _is_switch(dst):
            die()

    os.replace = _after  # type: ignore[assignment]

managed = os.environ.get("WORKER_UNIT") == "managed"
overwrite: pub.ErrorIfDifferent | pub.Replace = pub.ErrorIfDifferent()
if os.environ.get("WORKER_REPLACE"):
    backup = (
        pub.ProtectedBackup(os.environ["WORKER_BACKUP"])
        if os.environ.get("WORKER_BACKUP")
        else pub.NoBackup()
    )
    overwrite = pub.Replace(os.environ["WORKER_REPLACE"], backup)
request = pub.PublicationRequest(
    destination=target,
    operation_id=op,
    unit=pub.UNIT_MANAGED_DATASET if managed else pub.UNIT_IMMUTABLE_DIRECTORY,
    overwrite=overwrite,
)
sink = (ManagedDatasetSink if managed else ParquetTransactionalSink)(target, request=request)
sink.write("alpha", pa.table({"id": [1, 2, 3 if variant == "same" else 99]}))
if fault == "kill_after_stage":
    die()
try:
    receipt = sink.commit()
    print(json.dumps({"outcome": receipt.outcome, "descriptor": receipt.descriptor_digest}))
except pub.CommitError as exc:
    r = exc.receipt
    print(
        json.dumps({"outcome": r.outcome if r else "error", "reason": r.reason_code if r else None})
    )
