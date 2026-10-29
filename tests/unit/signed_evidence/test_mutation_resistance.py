"""A2: no mutation of the statement, header or signature fully verifies (property + exhaustive)."""

from __future__ import annotations

import base64
import json
from functools import lru_cache

import rfc8785
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as hst

from decoy_engine.signed_evidence import SchemaStatus, SignatureStatus, verify_signed_evidence
from tests.unit.signed_evidence import _corpus

_VECTOR = "valid_release_openssl"


@lru_cache(maxsize=1)
def _fixture():
    vec = _corpus.load(_VECTOR)
    return vec, _corpus.build_context(vec)


def _run(statement: bytes, signature: bytes | None, tmp):
    vec, ctx = _fixture()
    root = tmp / "art"
    if not root.exists():
        _corpus.materialize_artifacts(vec, root)
    return verify_signed_evidence(
        statement=statement, signature=signature, artifact_root=root, context=ctx
    )


def _ok(r) -> bool:
    return r.exit_class.is_success


def test_control_unmodified_bundle_verifies(tmp_path) -> None:
    vec, _ = _fixture()
    assert _ok(_run(vec.statement, vec.signature, tmp_path))


def test_every_single_bit_flip_of_the_statement_is_detected(tmp_path) -> None:
    vec, _ = _fixture()
    for i in range(len(vec.statement)):
        mutated = bytearray(vec.statement)
        mutated[i] ^= 0x01
        r = _run(bytes(mutated), vec.signature, tmp_path)
        assert not _ok(r), f"byte {i}"
        assert not (r.signature is SignatureStatus.VALID and r.schema is SchemaStatus.SUPPORTED), (
            f"byte {i}"
        )


def test_every_single_bit_flip_of_the_signature_value_is_detected(tmp_path) -> None:
    vec, _ = _fixture()
    sig = json.loads(vec.signature)
    raw = bytearray(base64.urlsafe_b64decode(sig["signature"] + "=="))
    assert len(raw) == 64
    for i in range(len(raw)):
        for bit in range(8):
            m = bytearray(raw)
            m[i] ^= 1 << bit
            sig2 = {**sig, "signature": base64.urlsafe_b64encode(bytes(m)).rstrip(b"=").decode()}
            r = _run(vec.statement, json.dumps(sig2).encode(), tmp_path)
            assert r.signature is SignatureStatus.INVALID and not _ok(r), (i, bit)


def test_every_single_bit_flip_of_the_protected_header_is_detected(tmp_path) -> None:
    vec, _ = _fixture()
    sig = json.loads(vec.signature)
    hdr = bytearray(base64.urlsafe_b64decode(sig["protected"] + "=="))
    for i in range(len(hdr)):
        m = bytearray(hdr)
        m[i] ^= 0x01
        sig2 = {**sig, "protected": base64.urlsafe_b64encode(bytes(m)).rstrip(b"=").decode()}
        assert not _ok(_run(vec.statement, json.dumps(sig2).encode(), tmp_path)), i


def _leaf_paths(node, prefix=()):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _leaf_paths(v, (*prefix, k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _leaf_paths(v, (*prefix, i))
    else:
        yield prefix


_LEAVES = list(_leaf_paths(json.loads(_corpus.load(_VECTOR).statement)))
_SCALARS = hst.one_of(
    hst.none(),
    hst.booleans(),
    hst.integers(-5, 10**6),
    hst.text(max_size=12),
    hst.sampled_from(["PASS", "FAIL", "INCONCLUSIVE", "REJECTED", "sha256:" + "0" * 64]),
)


@settings(
    max_examples=300, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(hst.sampled_from(_LEAVES), _SCALARS)
def test_property_any_leaf_mutation_with_the_original_signature_never_verifies(
    tmp_path_factory, leaf, value
) -> None:
    vec, _ = _fixture()
    doc = json.loads(vec.statement)
    node = doc
    for p in leaf[:-1]:
        node = node[p]
    if node[leaf[-1]] == value:
        return
    node[leaf[-1]] = value
    try:
        mutated = rfc8785.dumps(doc)
    except Exception:
        return
    tmp = tmp_path_factory.mktemp("m")
    r = _run(mutated, vec.signature, tmp)
    assert not _ok(r) and not (
        r.signature is SignatureStatus.VALID and r.schema is SchemaStatus.SUPPORTED
    )


@settings(max_examples=100, deadline=None)
@given(hst.permutations(list(json.loads(_corpus.load(_VECTOR).statement).items())))
def test_property_reordered_members_are_not_canonical_and_never_ok(tmp_path_factory, items) -> None:
    vec, _ = _fixture()
    original = list(json.loads(vec.statement).items())
    if list(items) == sorted(original):
        return
    reordered = json.dumps(dict(items), separators=(",", ":"), ensure_ascii=False).encode()
    if reordered == vec.statement:
        return
    r = _run(reordered, vec.signature, tmp_path_factory.mktemp("r"))
    assert not _ok(r)
