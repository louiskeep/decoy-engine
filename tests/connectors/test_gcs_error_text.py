"""GCS failure text is value-free on every surface (AT-E1 to AT-E3, V1 rule 3).

Every exception in the matrix carries a bucket, an object, a host and a URL in its
message. None of them may reach ``str()``/``repr()`` of an error the engine raises,
or ``CheckResult.detail``; the raised type follows the one classification table.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Callable
from typing import Any

import pytest

gax_exc = pytest.importorskip("google.api_core.exceptions")
gauth = pytest.importorskip("google.auth.exceptions")
requests = pytest.importorskip("requests")

from test_gcs_timeouts import (  # noqa: E402
    CSV_BODY,
    Recorder,
    install,
    parquet_body,
)

from decoy_engine.connectors.gcs import GCSConfig, GCSFileSink, GCSFileSource  # noqa: E402
from decoy_engine.profile._cloud_readers import (  # noqa: E402
    GcsCsvSource,
    GcsParquetSource,
    build_gcs_profile_source,
)
from decoy_engine.sdk import PermanentError, TransientError  # noqa: E402

_ = install  # fixture re-export
MSG = "zz_marker_bkt/zz_marker_obj https://zz-marker-host/b/zz_marker_bkt/o/zz_marker_obj"
MARKERS = ("zz_marker", "zz-marker-host")

MATRIX: dict[str, tuple[Callable[[], BaseException], type]] = {
    "NotFound": (lambda: gax_exc.NotFound(MSG), PermanentError),
    "Forbidden": (lambda: gax_exc.Forbidden(MSG), PermanentError),
    "PermissionDenied": (lambda: gax_exc.PermissionDenied(MSG), PermanentError),
    "Unauthenticated": (lambda: gax_exc.Unauthenticated(MSG), PermanentError),
    "ServiceUnavailable": (lambda: gax_exc.ServiceUnavailable(MSG), TransientError),
    "TooManyRequests": (lambda: gax_exc.TooManyRequests(MSG), TransientError),
    "DeadlineExceeded": (lambda: gax_exc.DeadlineExceeded(MSG), TransientError),
    "GatewayTimeout": (lambda: gax_exc.GatewayTimeout(MSG), TransientError),
    "InternalServerError": (lambda: gax_exc.InternalServerError(MSG), TransientError),
    "RetryError": (lambda: gax_exc.RetryError(MSG, ValueError(MSG)), TransientError),
    "requests.Timeout": (lambda: requests.exceptions.Timeout(MSG), TransientError),
    "requests.ConnectionError": (
        lambda: requests.exceptions.ConnectionError(MSG),
        TransientError,
    ),
    "BadRequest": (lambda: gax_exc.BadRequest(MSG), PermanentError),
    "auth.TransportError": (lambda: gauth.TransportError(MSG), TransientError),
    "auth.TimeoutError": (lambda: gauth.TimeoutError(MSG), TransientError),
    "auth.DefaultCredentialsError": (lambda: gauth.DefaultCredentialsError(MSG), PermanentError),
    "auth.RefreshError": (lambda: gauth.RefreshError(MSG), PermanentError),
    "auth.MalformedError": (lambda: gauth.MalformedError(MSG), PermanentError),
    "auth.GoogleAuthError": (lambda: gauth.GoogleAuthError(MSG), PermanentError),
    "RuntimeError": (lambda: RuntimeError(MSG), PermanentError),
}
CASES = sorted(MATRIX)
CFG = GCSConfig(bucket="zz_marker_bkt", project="test-project")


def _value_free(*texts: str) -> None:
    for text in texts:
        for marker in MARKERS:
            assert marker not in text, f"error text carries {marker!r}: {text!r}"


@pytest.mark.parametrize("cls", [GCSFileSource, GCSFileSink])
@pytest.mark.parametrize("case", CASES)
def test_check_detail_is_value_free(case: str, cls: type, install: Callable[..., Recorder]):
    make, _expected = MATRIX[case]
    install(fail={"get_bucket": make()})
    result = cls(CFG).check()
    assert result.ok is False
    assert result.detail
    _value_free(result.detail)


CONNECTOR_POINTS: dict[str, tuple[str, Callable[[], Any]]] = {
    "list@iterate": ("list_iter", lambda: list(GCSFileSource(CFG).list())),
    "head@reload": ("reload", lambda: GCSFileSource(CFG).head("zz_marker_obj")),
    "open@open": ("open", lambda: b"".join(GCSFileSource(CFG).open("zz_marker_obj"))),
    "open@read": ("read", lambda: b"".join(GCSFileSource(CFG).open("zz_marker_obj"))),
    "write@write": ("write", lambda: GCSFileSink(CFG).write("zz_marker_obj", iter([b"x"]))),
    "write@close": ("close", lambda: GCSFileSink(CFG).write("zz_marker_obj", iter([b"x"]))),
}


@pytest.mark.parametrize("point", sorted(CONNECTOR_POINTS))
@pytest.mark.parametrize("case", CASES)
def test_raised_connector_errors_are_value_free(
    case: str, point: str, install: Callable[..., Recorder]
) -> None:
    make, expected = MATRIX[case]
    where, run = CONNECTOR_POINTS[point]
    install(CSV_BODY, fail={where: make()})
    with pytest.raises(Exception) as info:
        run()
    err = info.value
    _value_free(str(err), repr(err))
    assert type(err) is expected, f"{case} at {point}: got {type(err).__name__}"


READER_POINTS: dict[str, tuple[str, Callable[[], Any], Callable[[], bytes]]] = {
    "GcsCsvSource.row_count@reload": (
        "reload",
        lambda: GcsCsvSource(_CSV).row_count(),
        lambda: CSV_BODY,
    ),
    "GcsCsvSource.row_count@download": (
        "download_as_bytes",
        lambda: GcsCsvSource(_CSV).row_count(),
        lambda: CSV_BODY,
    ),
    "GcsCsvSource.to_frame@download": (
        "download_as_bytes",
        lambda: GcsCsvSource(_CSV).to_frame(),
        lambda: CSV_BODY,
    ),
    "GcsParquetSource.schema@reload": (
        "reload",
        lambda: GcsParquetSource(_PQ).schema(),
        parquet_body,
    ),
    "GcsParquetSource.schema@download": (
        "download_as_bytes",
        lambda: GcsParquetSource(_PQ).schema(),
        parquet_body,
    ),
    "GcsParquetSource.to_frame@download": (
        "download_as_bytes",
        lambda: GcsParquetSource(_PQ).to_frame(),
        parquet_body,
    ),
}
_CSV = {"format": "csv", "bucket": "zz_marker_bkt", "object": "zz_marker_obj"}
_PQ = {"format": "parquet", "bucket": "zz_marker_bkt", "object": "zz_marker_obj"}


@pytest.mark.parametrize("point", sorted(READER_POINTS))
@pytest.mark.parametrize("case", CASES)
def test_profile_reader_errors_are_value_free(
    case: str, point: str, install: Callable[..., Recorder]
) -> None:
    make, expected = MATRIX[case]
    where, run, body = READER_POINTS[point]
    install(body(), fail={where: make()})
    with pytest.raises(Exception) as info:
        run()
    err = info.value
    _value_free(str(err), repr(err))
    assert type(err) is expected, f"{case} at {point}: got {type(err).__name__}"


@pytest.mark.parametrize(
    "descriptor",
    [
        {"format": "csv", "bucket": None, "object": "zz_marker_obj"},
        {"format": "csv", "bucket": b"zz_marker_bkt", "object": "zz_marker_obj"},
        {"format": "csv", "bucket": "b", "object": ["zz_marker_obj"]},
    ],
    ids=["bucket-none", "bucket-bytes", "object-list"],
)
def test_profile_reader_descriptor_errors_are_value_free(
    descriptor: dict[str, Any], install: Callable[..., Recorder]
) -> None:
    install(CSV_BODY)
    with pytest.raises(ValueError) as info:
        build_gcs_profile_source(descriptor).row_count()
    _value_free(str(info.value), repr(info.value))


@pytest.mark.parametrize("cls", [TransientError, PermanentError])
def test_engine_errors_pass_through_unchanged(cls: type, install: Callable[..., Recorder]):
    original = cls("GCS transient network or service error")
    install(CSV_BODY, fail={"reload": original})
    with pytest.raises(cls) as info:
        GCSFileSource(CFG).head("o")
    assert info.value is original


# ----- direct checks of the factory's file-object and fallback translation --------


def _clients() -> Any:
    return importlib.import_module("decoy_engine.connectors._clients")


@pytest.mark.parametrize("where", ["read", "write", "close"])
def test_open_file_object_translates_after_open(where: str, install: Callable[..., Recorder]):
    """``gcs_open`` itself translates errors raised by the returned file object.

    The factory module is absent on the base, so the missing attribute is an assertion.
    """
    try:
        gcs_open = _clients().gcs_open
    except ImportError:
        gcs_open = None
    assert gcs_open is not None, "connectors._clients.gcs_open does not exist"
    from test_gcs_timeouts import FakeBlob

    rec = install(CSV_BODY, fail={where: gax_exc.ServiceUnavailable(MSG)})
    with pytest.raises(TransientError) as info:
        with gcs_open(FakeBlob(rec, "o"), "rb" if where == "read" else "wb") as remote:
            if where == "read":
                remote.read(4)
            else:
                remote.write(b"x")
    _value_free(str(info.value), repr(info.value))


def test_wrap_gcs_error_classifies_a_retry_timeout_as_transient() -> None:
    from decoy_engine.connectors.gcs import _wrap_gcs_error

    err = _wrap_gcs_error(gax_exc.RetryError(MSG, ValueError(MSG)))
    assert type(err) is TransientError, type(err).__name__
    _value_free(str(err), repr(err))


@pytest.mark.parametrize("where", ["read", "write", "close"])
def test_open_errors_reach_the_connector_value_free(where: str, install: Callable[..., Recorder]):
    """The error comes from the returned file object, not from ``open`` itself.

    Driven through the public connector entries so it fails on the base by assertion.
    """
    install(CSV_BODY, fail={where: gax_exc.ServiceUnavailable(MSG)})
    raised: BaseException | None = None
    try:
        if where == "read":
            b"".join(GCSFileSource(CFG).open("o"))
        else:
            GCSFileSink(CFG).write("o", iter([b"x"]))
    except Exception as err:
        raised = err
    assert type(raised) is TransientError, type(raised).__name__
    _value_free(str(raised), repr(raised))


def test_without_google_libraries_every_error_is_the_fixed_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients = _clients()
    for name in ("google.api_core.exceptions", "requests", "requests.exceptions"):
        monkeypatch.setitem(sys.modules, name, None)
    err = clients.gcs_error(RuntimeError(MSG))
    assert type(err) is PermanentError
    assert str(err) == "Unexpected GCS error"


CONSTRUCTION_ENTRIES: dict[str, Callable[[], Any]] = {
    "GCSFileSource.check": lambda: GCSFileSource(CFG).check(),
    "GCSFileSink.check": lambda: GCSFileSink(CFG).check(),
    "GCSFileSource.list": lambda: list(GCSFileSource(CFG).list()),
    "GCSFileSource.head": lambda: GCSFileSource(CFG).head("zz_marker_obj"),
    "GCSFileSource.open": lambda: b"".join(GCSFileSource(CFG).open("zz_marker_obj")),
    "GCSFileSink.write": lambda: GCSFileSink(CFG).write("zz_marker_obj", iter([b"x"])),
    "GcsCsvSource.row_count": lambda: GcsCsvSource(_CSV).row_count(),
    "GcsCsvSource.to_frame": lambda: GcsCsvSource(_CSV).to_frame(),
    "GcsParquetSource.row_count": lambda: GcsParquetSource(_PQ).row_count(),
    "GcsParquetSource.to_frame": lambda: GcsParquetSource(_PQ).to_frame(),
}
CONSTRUCTION_FAILURES: dict[str, Callable[[], BaseException]] = {
    "adc-missing": lambda: gauth.DefaultCredentialsError(
        f"File /zz_marker_dir/creds.json was not found. {MSG}"
    ),
    "refresh-error": lambda: gauth.RefreshError("zz_marker token for zz-marker-host"),
    "runtime-error": lambda: RuntimeError("zz_marker"),
}


@pytest.mark.parametrize("entry", sorted(CONSTRUCTION_ENTRIES))
@pytest.mark.parametrize("failure", sorted(CONSTRUCTION_FAILURES))
def test_client_construction_errors_are_value_free(
    entry: str, failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from google.cloud import storage

    def _fail(*args: Any, **kwargs: Any) -> Any:
        raise CONSTRUCTION_FAILURES[failure]()

    monkeypatch.setattr(storage, "Client", _fail)
    result = None
    try:
        result = CONSTRUCTION_ENTRIES[entry]()
    except Exception as err:
        _value_free(str(err), repr(err))
        assert "zz_marker_dir" not in str(err) + repr(err)
        assert type(err) is PermanentError, type(err).__name__
    else:
        assert result.ok is False
        _value_free(result.detail)
        assert "zz_marker_dir" not in result.detail


class _BrokenBucketClient:
    """A client whose ``bucket()`` raises, as a malformed bucket name does."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def bucket(self, name: str) -> Any:
        raise ValueError(f"Invalid bucket name {name!r} zz_marker")


@pytest.mark.parametrize("entry", ["list", "head", "open", "write"])
def test_bucket_handle_failures_are_typed_and_value_free(
    entry: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from google.cloud import storage

    monkeypatch.setattr(storage, "Client", _BrokenBucketClient)
    run = {
        "list": lambda: list(GCSFileSource(CFG).list()),
        "head": lambda: GCSFileSource(CFG).head("o"),
        "open": lambda: b"".join(GCSFileSource(CFG).open("o")),
        "write": lambda: GCSFileSink(CFG).write("o", iter([b"x"])),
    }[entry]
    with pytest.raises(PermanentError) as info:
        run()
    _value_free(str(info.value), repr(info.value))


SA_ENTRIES: dict[str, Callable[[GCSConfig], Any]] = {
    "GCSFileSource.check": lambda c: GCSFileSource(c).check(),
    "GCSFileSink.check": lambda c: GCSFileSink(c).check(),
    "GCSFileSource.list": lambda c: list(GCSFileSource(c).list()),
    "GCSFileSource.head": lambda c: GCSFileSource(c).head("o"),
    "GCSFileSource.open": lambda c: b"".join(GCSFileSource(c).open("o")),
    "GCSFileSink.write": lambda c: GCSFileSink(c).write("o", iter([b"x"])),
}


def _chain_has_secret(err: BaseException) -> bool:
    """True when anything reachable through cause or context carries the secret."""
    seen: BaseException | None = err
    while seen is not None:
        if "zz_marker_secret" in repr(seen) or "zz_marker_secret" in str(getattr(seen, "doc", "")):
            return True
        seen = seen.__cause__ or seen.__context__
    return False


@pytest.mark.parametrize("entry", sorted(SA_ENTRIES))
@pytest.mark.parametrize("case", ["invalid-json", "rejected-key"])
def test_service_account_failures_are_value_free(
    entry: str, case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AT-E4 cases 4 and 5: the credential text never reaches the error or its chain."""
    from google.oauth2 import service_account

    if case == "invalid-json":
        text = '{"zz_marker_secret": '
    else:
        text = '{"zz_marker_secret": "key"}'

        def _reject(*args: Any, **kwargs: Any) -> Any:
            raise ValueError("zz_marker_secret key")

        monkeypatch.setattr(
            service_account.Credentials, "from_service_account_info", classmethod(_reject)
        )
    cfg = GCSConfig(bucket="zz_marker_bkt", project="p", service_account_json=text)
    try:
        result = SA_ENTRIES[entry](cfg)
    except Exception as err:
        _value_free(str(err), repr(err))
        assert type(err) is PermanentError, type(err).__name__
        assert err.__cause__ is None
        assert not _chain_has_secret(err)
    else:
        assert result.ok is False
        _value_free(result.detail)


@pytest.mark.parametrize("case", sorted(c for c in MATRIX if c.startswith("auth.")))
def test_google_auth_errors_get_their_own_fixed_text(case: str) -> None:
    make, expected = MATRIX[case]
    err = _clients().gcs_error(make())
    assert type(err) is expected
    want = (
        "GCS transient network or service error"
        if expected is TransientError
        else "GCS credentials unavailable or invalid"
    )
    assert str(err) == want
