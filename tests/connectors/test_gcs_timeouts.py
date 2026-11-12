"""Every GCS call the engine makes carries the engine's own timeout and retry (AT-C10a-d).

Two layers. Recording fakes stand in for google-cloud-storage and capture the
``timeout`` and ``retry`` each call received, across all ten connector and
profile-reader entry points. A local socket that accepts and never answers stands
in for a black-holed endpoint (``STORAGE_EMULATOR_HOST``); each entry point runs
in a child process with shortened bounds and must fail inside them with
value-free text.
"""

from __future__ import annotations

import importlib
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

gax_exc = pytest.importorskip("google.api_core.exceptions")

import decoy_engine  # noqa: E402
from decoy_engine.connectors.gcs import GCSConfig, GCSFileSink, GCSFileSource  # noqa: E402
from decoy_engine.profile._cloud_readers import GcsCsvSource, GcsParquetSource  # noqa: E402
from decoy_engine.sdk import TransientError  # noqa: E402

MISSING = "<missing>"
MARK_BKT = "zz_marker_bkt"
MARK_OBJ = "zz_marker_obj"
CSV_BODY = b"a,b\n1,x\n2,y\n3,z\n"


def parquet_body() -> bytes:
    buf = io.BytesIO()
    pq.write_table(pa.table({"a": [1, 2, 3], "b": ["x", "y", "z"]}), buf)
    return buf.getvalue()


def expected_bounds() -> tuple[tuple[float, float], float]:
    try:
        mod = importlib.import_module("decoy_engine.connectors._clients")
    except ImportError:
        return (5.0, 60.0), 120.0
    return mod.GCS_TIMEOUT, mod.GCS_RETRY_TOTAL_S


# ----- recording fakes (shared with test_gcs_error_text.py) ------------------------


class Recorder:
    """Call log plus the failure plan: ``fail[point]`` is raised at that point."""

    def __init__(self, body: bytes = b"", fail: dict[str, BaseException] | None = None):
        self.body = body
        self.fail = fail or {}
        self.calls: list[tuple[str, Any, Any]] = []
        self.client_kwargs: list[tuple[tuple, dict]] = []

    def hit(self, point: str, timeout: Any = MISSING, retry: Any = MISSING) -> None:
        if point in _CALL_POINTS:
            self.calls.append((point, timeout, retry))
        if point in self.fail:
            raise self.fail[point]


_CALL_POINTS = {"get_bucket", "reload", "download_as_bytes", "open"}


class FakeFile:
    def __init__(self, rec: Recorder, mode: str):
        self.rec, self.mode = rec, mode
        self._buf = io.BytesIO(rec.body if "r" in mode else b"")

    def read(self, n: int = -1) -> bytes:
        self.rec.hit("read")
        return self._buf.read(n)

    def write(self, data: bytes) -> int:
        self.rec.hit("write")
        return self._buf.write(data)

    def close(self) -> None:
        self.rec.hit("close")

    def __enter__(self) -> FakeFile:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class FakeBlob:
    def __init__(self, rec: Recorder, name: str):
        self.rec, self.name = rec, name
        self.size: int | None = None
        self.content_type = "application/octet-stream"
        self.updated = None

    def reload(self, timeout: Any = MISSING, retry: Any = MISSING) -> None:
        self.rec.hit("reload", timeout, retry)
        self.size = len(self.rec.body)

    def download_as_bytes(
        self,
        start: int | None = None,
        end: int | None = None,
        timeout: Any = MISSING,
        retry: Any = MISSING,
    ) -> bytes:
        self.rec.hit("download_as_bytes", timeout, retry)
        body = self.rec.body
        return body if start is None else body[start : (end + 1 if end is not None else None)]

    def open(self, mode: str, timeout: Any = MISSING, retry: Any = MISSING) -> FakeFile:
        self.rec.hit("open", timeout, retry)
        return FakeFile(self.rec, mode)


class FakeBucket:
    def __init__(self, rec: Recorder, name: str):
        self.rec, self.name = rec, name

    def blob(self, name: str) -> FakeBlob:
        return FakeBlob(self.rec, name)

    def list_blobs(
        self, prefix: str | None = None, timeout: Any = MISSING, retry: Any = MISSING
    ) -> Iterator[FakeBlob]:
        self.rec.calls.append(("list_blobs", timeout, retry))

        def pages() -> Iterator[FakeBlob]:
            self.rec.hit("list_iter")
            blob = FakeBlob(self.rec, "listed.bin")
            blob.size = len(self.rec.body)
            yield blob

        return pages()


class FakeClient:
    def __init__(self, rec: Recorder):
        self.rec = rec

    def bucket(self, name: str) -> FakeBucket:
        return FakeBucket(self.rec, name)

    def get_bucket(self, name: str, timeout: Any = MISSING, retry: Any = MISSING) -> FakeBucket:
        self.rec.hit("get_bucket", timeout, retry)
        return FakeBucket(self.rec, name)

    def __enter__(self) -> FakeClient:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


@pytest.fixture
def install(monkeypatch: pytest.MonkeyPatch) -> Callable[..., Recorder]:
    """Patch ``google.cloud.storage.Client`` with a recording fake."""
    from google.cloud import storage

    def _install(body: bytes = b"", fail: dict[str, BaseException] | None = None) -> Recorder:
        rec = Recorder(body, fail)

        def _client(*args: Any, **kwargs: Any) -> FakeClient:
            rec.client_kwargs.append((args, kwargs))
            return FakeClient(rec)

        monkeypatch.setattr(storage, "Client", _client)
        return rec

    return _install


CFG = GCSConfig(bucket=MARK_BKT, project="test-project")
DESC_CSV = {"format": "csv", "bucket": MARK_BKT, "object": MARK_OBJ}
DESC_PQ = {"format": "parquet", "bucket": MARK_BKT, "object": MARK_OBJ}

ENTRIES: dict[str, tuple[Callable[[], Any], Callable[[], bytes]]] = {
    "GCSFileSource.check": (lambda: GCSFileSource(CFG).check(), lambda: b""),
    "GCSFileSource.list": (lambda: list(GCSFileSource(CFG).list()), lambda: CSV_BODY),
    "GCSFileSource.head": (lambda: GCSFileSource(CFG).head(MARK_OBJ), lambda: CSV_BODY),
    "GCSFileSource.open": (lambda: b"".join(GCSFileSource(CFG).open(MARK_OBJ)), lambda: CSV_BODY),
    "GCSFileSink.check": (lambda: GCSFileSink(CFG).check(), lambda: b""),
    "GCSFileSink.write": (lambda: GCSFileSink(CFG).write(MARK_OBJ, iter([b"x"])), lambda: b""),
    "GcsParquetSource.row_count": (lambda: GcsParquetSource(DESC_PQ).row_count(), parquet_body),
    "GcsParquetSource.to_frame": (lambda: GcsParquetSource(DESC_PQ).to_frame(), parquet_body),
    "GcsCsvSource.row_count": (lambda: GcsCsvSource(DESC_CSV).row_count(), lambda: CSV_BODY),
    "GcsCsvSource.to_frame": (lambda: GcsCsvSource(DESC_CSV).to_frame(), lambda: CSV_BODY),
}


# ----- AT-C10a: every entry passes the engine's timeout and retry ---------------


@pytest.mark.parametrize("entry", sorted(ENTRIES))
def test_every_gcs_entry_passes_engine_timeout_and_retry(
    entry: str, install: Callable[..., Recorder]
) -> None:
    run, body = ENTRIES[entry]
    rec = install(body())
    result = run()
    assert getattr(result, "ok", True) is True, getattr(result, "detail", None)
    assert rec.calls, f"{entry} made no recorded GCS call"
    want_timeout, want_total = expected_bounds()
    for method, timeout, retry in rec.calls:
        assert timeout == want_timeout, f"{entry}: {method} got timeout={timeout!r}"
        total = getattr(retry, "timeout", None)
        assert total == want_total, f"{entry}: {method} got retry={retry!r}"


# ----- AT-C10d: a translated timeout is transient on the connector ---------------


def test_timeout_is_transient_on_the_connector(install: Callable[..., Recorder]) -> None:
    import requests

    cause = requests.exceptions.ReadTimeout(f"https://zz-marker-host/b/{MARK_BKT}/o/{MARK_OBJ}")
    install(fail={"reload": gax_exc.RetryError("Timeout of 1.0s exceeded", cause)})
    with pytest.raises(TransientError):
        GCSFileSource(CFG).head(MARK_OBJ)


# ----- class 9: Client() vs Client(project=None) preserved per caller -----------


def test_client_kwargs_preserved(
    install: Callable[..., Recorder], monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = install(CSV_BODY)
    GCSFileSource(GCSConfig(bucket="b")).head("o")
    GCSFileSource(GCSConfig(bucket="b", project="p")).head("o")
    from google.oauth2 import service_account

    creds = object()
    monkeypatch.setattr(
        service_account.Credentials, "from_service_account_info", classmethod(lambda c, i: creds)
    )
    GCSFileSource(GCSConfig(bucket="b", project="p", service_account_json="{}")).head("o")
    assert rec.client_kwargs == [
        ((), {"project": None}),
        ((), {"project": "p"}),
        ((), {"project": "p", "credentials": creds}),
    ]
    rec.client_kwargs.clear()
    GcsCsvSource(DESC_CSV).row_count()
    GcsCsvSource(DESC_CSV).to_frame()
    assert rec.client_kwargs == [((), {}), ((), {})]


# ----- AT-C10b / AT-C10c: black-holed endpoint, child processes ------------------

CHILD = r"""
import importlib, json, sys, time
entry = sys.argv[1]
sys.path.insert(0, sys.argv[2])  # the tree under test, not whichever checkout is installed
try:
    c = importlib.import_module("decoy_engine.connectors._clients")
    c.GCS_TIMEOUT = (0.5, 0.5)
    c.GCS_RETRY_TOTAL_S = 1.0
except ImportError:
    pass
from decoy_engine.connectors.gcs import GCSConfig, GCSFileSink, GCSFileSource
from decoy_engine.profile._cloud_readers import GcsCsvSource, GcsParquetSource
from decoy_engine.sdk import ConnectorError, TransientError
B, O = "zz_marker_bkt", "zz_marker_obj"
cfg = GCSConfig(bucket=B)
csv = {"format": "csv", "bucket": B, "object": O}
pqd = {"format": "parquet", "bucket": B, "object": O}
run = {
    "GCSFileSource.check": lambda: GCSFileSource(cfg).check(),
    "GCSFileSource.list": lambda: list(GCSFileSource(cfg).list()),
    "GCSFileSource.head": lambda: GCSFileSource(cfg).head(O),
    "GCSFileSource.open": lambda: b"".join(GCSFileSource(cfg).open(O)),
    "GCSFileSink.check": lambda: GCSFileSink(cfg).check(),
    "GCSFileSink.write": lambda: GCSFileSink(cfg).write(O, iter([b"x"])),
    "GcsParquetSource.row_count": lambda: GcsParquetSource(pqd).row_count(),
    "GcsParquetSource.to_frame": lambda: GcsParquetSource(pqd).to_frame(),
    "GcsCsvSource.row_count": lambda: GcsCsvSource(csv).row_count(),
    "GcsCsvSource.to_frame": lambda: GcsCsvSource(csv).to_frame(),
}[entry]
out = {"entry": entry}
t0 = time.monotonic()
try:
    res = run()
    out["ok"] = getattr(res, "ok", None)
    out["detail"] = getattr(res, "detail", None)
except Exception as exc:
    out["transient"] = isinstance(exc, TransientError)
    out["connector"] = isinstance(exc, ConnectorError)
    out["text"] = str(exc) + " " + repr(exc)
out["elapsed"] = time.monotonic() - t0
print(json.dumps(out))
"""

SRC_DIR = Path(decoy_engine.__file__).resolve().parent.parent
BOUND_S = 15.0
CHILD_KILL_S = 30.0


@pytest.fixture(scope="module")
def black_hole_runs() -> dict[str, dict[str, Any]]:
    """Run every entry once against a socket that accepts and never answers."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(128)
    held: list[socket.socket] = []
    stop = threading.Event()

    def _accept() -> None:
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                held.append(srv.accept()[0])
            except OSError:
                continue

    thread = threading.Thread(target=_accept, daemon=True)
    thread.start()
    env = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")}
    env.update(
        STORAGE_EMULATOR_HOST=f"http://127.0.0.1:{srv.getsockname()[1]}",
        NO_PROXY="127.0.0.1,localhost",
        no_proxy="127.0.0.1,localhost",
        PYTHONWARNINGS="ignore",
    )
    procs = {
        e: (
            time.monotonic(),
            subprocess.Popen(  # noqa: S603 - fixed argv
                [sys.executable, "-I", "-c", CHILD, e, str(SRC_DIR)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                text=True,
            ),
        )
        for e in sorted(ENTRIES)
    }
    runs: dict[str, dict[str, Any]] = {}
    try:
        for entry, (start, proc) in procs.items():
            remaining = max(0.1, CHILD_KILL_S - (time.monotonic() - start))
            try:
                out, err = proc.communicate(timeout=remaining)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                runs[entry] = {"killed": True, "wall": time.monotonic() - start}
                continue
            wall = time.monotonic() - start
            lines = out.strip().splitlines()
            runs[entry] = json.loads(lines[-1]) if lines else {"stderr": err[-2000:]}
            runs[entry]["wall"] = wall
    finally:
        stop.set()
        thread.join(timeout=2)
        for conn in held:
            conn.close()
        srv.close()
    return runs


def _finished(run: dict[str, Any], entry: str) -> None:
    if run.get("killed"):
        pytest.fail(f"{entry}: still running after {CHILD_KILL_S:.0f}s against a black hole")
    assert "elapsed" in run, f"{entry}: child crashed: {run.get('stderr')}"
    assert run["wall"] < BOUND_S, f"{entry}: took {run['wall']:.1f}s, bound {BOUND_S:.0f}s"


@pytest.mark.parametrize("entry", sorted(ENTRIES))
@pytest.mark.serial
def test_black_holed_endpoint_fails_inside_the_bound(
    entry: str, black_hole_runs: dict[str, dict[str, Any]]
) -> None:
    run = black_hole_runs[entry]
    _finished(run, entry)
    if entry.endswith(".check"):
        assert run["ok"] is False and run["detail"]
    elif entry.startswith("GCSFile"):
        assert run.get("transient") is True, run
    else:
        assert run.get("connector") is True, run


@pytest.mark.parametrize("entry", sorted(ENTRIES))
@pytest.mark.serial
def test_timeout_error_text_is_value_free(
    entry: str, black_hole_runs: dict[str, dict[str, Any]]
) -> None:
    run = black_hole_runs[entry]
    _finished(run, entry)
    text = run["detail"] if entry.endswith(".check") else run.get("text")
    assert text, run
    for marker in (MARK_BKT, MARK_OBJ, "127.0.0.1"):
        assert marker not in text, f"{entry}: error text carries {marker!r}"
    if entry.startswith("GCSFile") and not entry.endswith(".check"):
        assert run.get("transient") is True, run
