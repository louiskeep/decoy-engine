"""`S3Source.endpoint_url` carries a host and nothing that is a credential or a request."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from pydantic_core import PydanticCustomError

from decoy_engine.config import S3Source

CANARY = "CANARY-7c2e41"


def _source(endpoint_url: str | None) -> dict[str, object]:
    return {"type": "s3", "format": "csv", "bucket": "b", "key": "k", "endpoint_url": endpoint_url}


@pytest.mark.parametrize(
    "url",
    [
        None,
        "http://localhost:9000",
        "https://acct.r2.cloudflarestorage.com",
        "https://minio.internal:9000/prefix",
    ],
)
def test_plain_endpoints_are_accepted(url: str | None) -> None:
    assert S3Source.model_validate(_source(url)).endpoint_url == url


@pytest.mark.parametrize(
    "url",
    [
        f"https://user:{CANARY}@host.example",
        f"https://{CANARY}@host.example",
        f"https://host.example/path?token={CANARY}",
        f"https://host.example?{CANARY}",
        f"https://host.example/path?{CANARY}#frag",
        f"https://host.example#user:{CANARY}@x",
        f"https://host.example/p#{CANARY}",
        f"https://host.example%40{CANARY}",
        f"https://user%40{CANARY}.example",
        f"https://host.example/p%3F{CANARY}",
        f"https://host.example/p%3f{CANARY}",
        f"https://host.example/p%23{CANARY}",
        f"ftp://host.example/{CANARY}",
        f"//host.example/{CANARY}",
    ],
)
def test_userinfo_and_query_are_refused_without_echoing_the_value(url: str) -> None:
    with pytest.raises(ValidationError) as info:
        S3Source.model_validate(_source(url))
    assert CANARY not in str(info.value)


def _chain(exc: BaseException) -> list[BaseException]:
    seen: list[BaseException] = []
    pending: list[BaseException | None] = [exc]
    while pending:
        cur = pending.pop()
        if cur is None or cur in seen:
            continue
        seen.append(cur)
        pending += [cur.__cause__, cur.__context__]
    return seen


def test_malformed_port_refusal_keeps_no_chained_value_error() -> None:
    url = f"https://{CANARY}.example:99999x"
    with pytest.raises(ValidationError) as info:
        S3Source.model_validate(_source(url))
    assert CANARY not in str(info.value)
    # pydantic drops the chain, so also check the exception the validator itself raises.
    with pytest.raises(PydanticCustomError) as raised:
        S3Source._endpoint_is_a_host(url)
    chain = _chain(raised.value)[1:]  # PydanticCustomError is itself a ValueError subclass
    assert not [e for e in chain if isinstance(e, ValueError)]
    assert all(CANARY not in str(e) for e in [*chain, raised.value])
