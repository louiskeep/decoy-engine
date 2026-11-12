"""AT-E1, AT-E3, AT-E3b, AT-E5, AT-E18: the ``decoy-run-result/v1`` model.

The value-free walk and the read-tree walk read the real pydantic field tree, not a hand-kept list,
so a field added later is judged by the same rule as the ones that exist today.
"""

from __future__ import annotations

import ast
import copy
import inspect
import json
import textwrap
import types
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Literal, Union, get_args, get_origin

import pytest
from annotated_types import Ge
from pydantic import (
    AfterValidator,
    AllowInfNan,
    BaseModel,
    ConfigDict,
    Strict,
    StrictInt,
    ValidationError,
    computed_field,
    field_serializer,
)

from decoy_engine.plan import compile_plan, pipeline_config_hash
from decoy_engine.profile import ColumnProfile, Profile, TableProfile
from decoy_engine.reasons import ReasonRecord, _registry, decoy
from decoy_engine.run_result import RUN_RESULT_SCHEMA, RunResult, RunResultRead, _model
from decoy_engine.run_result._model import Token, ValueFree

FIXTURES = Path(__file__).resolve().parents[2] / "integration" / "compat_corpus" / "run_result"
MARKER = "zz_marker_ctx"


def load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def live_reason(**edits: Any) -> dict[str, Any]:
    payload = ReasonRecord.for_reason(decoy.UNEXPECTED_ERROR).to_payload()
    payload.update(edits)
    return payload


def failed_doc(reason: dict[str, Any]) -> dict[str, Any]:
    doc = load("run-result-v1.json")
    doc.update(status="failed", exit_code=3)
    doc["failure"] = {"category": "runtime", "error_class": "RuntimeError", "reason": reason}
    return doc


# --- value-free walk (AT-E1) -------------------------------------------------------------------


def _is_union(origin: object) -> bool:
    return origin is Union or origin is types.UnionType


def _named_types() -> dict[str, tuple[Any, ...]]:
    """The closed set of value-free kinds: the module-level aliases of ``_model``, with metadata."""
    found: dict[str, tuple[Any, ...]] = {}
    for obj in vars(_model).values():
        if get_origin(obj) is Annotated:
            _, *meta = get_args(obj)
            marks = [m for m in meta if isinstance(m, ValueFree)]
            if marks:
                found[marks[0].kind] = tuple(meta)
    return found


def _marker_problems(meta: tuple[Any, ...], path: str) -> list[str]:
    """A marker counts only when its kind is a named type and the field carries that validator."""
    kind = next(m for m in meta if isinstance(m, ValueFree)).kind
    named = _named_types().get(kind)
    if named is None:
        return [f"{path}: marker kind {kind!r} is not a named value-free type"]
    if not all(any(m == want for m in meta) for want in named):
        return [f"{path}: marker {kind!r} without its validator"]
    return []


def _strict(meta: tuple[Any, ...]) -> bool:
    return any(isinstance(m, Strict) and m.strict for m in meta)


def _annotation_problems(ann: Any, meta: tuple[Any, ...], path: str, seen: set[type]) -> list[str]:
    if any(isinstance(m, ValueFree) for m in meta):
        return _marker_problems(meta, path)
    origin = get_origin(ann)
    if origin is Annotated:
        base, *extra = get_args(ann)
        return _annotation_problems(base, meta + tuple(extra), path, seen)
    if _is_union(origin):
        return [p for arg in get_args(ann) for p in _annotation_problems(arg, meta, path, seen)]
    if origin in (list, tuple, set, frozenset):
        return [
            p
            for arg in get_args(ann)
            if arg is not Ellipsis
            for p in _annotation_problems(arg, meta, path, seen)
        ]
    if origin is dict:
        return [p for arg in get_args(ann) for p in _annotation_problems(arg, meta, path, seen)]
    if origin is Literal or ann is type(None):
        return []
    if ann in (bool, int):
        return [] if _strict(meta) else [f"{path}: lax {ann.__name__}"]
    if ann is float:
        finite = any(getattr(m, "allow_inf_nan", True) is False for m in meta)
        problems = [] if finite else [f"{path}: float without allow_inf_nan=False"]
        return problems if _strict(meta) else [*problems, f"{path}: lax float"]
    if isinstance(ann, type) and issubclass(ann, Enum):
        return []
    if isinstance(ann, type) and issubclass(ann, BaseModel):
        return _model_problems(ann, path, seen)
    return [f"{path}: unvetted leaf type {ann!r}"]


def _model_problems(model: type[BaseModel], path: str, seen: set[type]) -> list[str]:
    if model in seen:
        return []
    seen.add(model)
    problems: list[str] = []
    for name, field in model.model_fields.items():
        problems += _annotation_problems(
            field.annotation, tuple(field.metadata), f"{path}.{name}", seen
        )
    for name, computed in model.model_computed_fields.items():
        problems += _annotation_problems(computed.return_type, (), f"{path}.{name}()", seen)
    decorators = model.__pydantic_decorators__
    # The one allowed model serializer only drops an unset optional key; it adds no value.
    custom = [
        d for d in decorators.model_serializers.values() if d.cls_var_name != "_omit_when_none"
    ]
    if decorators.field_serializers or custom:
        problems.append(f"{path}: custom serializer")
    if model.model_config.get("json_encoders"):
        problems.append(f"{path}: json_encoders")
    for field in model.model_fields.values():
        for m in field.metadata:
            if type(m).__name__ in {"PlainSerializer", "WrapSerializer"}:
                problems.append(f"{path}: {type(m).__name__}")
    return problems


def value_free_problems(model: type[BaseModel]) -> list[str]:
    return _model_problems(model, model.__name__, set())


def test_every_field_is_value_free() -> None:
    assert value_free_problems(RunResult) == []


def test_every_read_field_is_value_free() -> None:
    assert value_free_problems(RunResultRead) == []


class _StrField(BaseModel):
    message: str


class _AnyField(BaseModel):
    anything: Any


class _DictField(BaseModel):
    extra: dict[str, Any]


class _ComputedStr(BaseModel):
    count: StrictInt

    @computed_field  # type: ignore[prop-decorator]
    @property
    def summary(self) -> str:
        return "x"


class _Serialized(BaseModel):
    count: StrictInt

    @field_serializer("count")
    def _ser(self, value: int) -> str:
        return str(value)


class _Nested(BaseModel):
    inner: list[_StrField]


class _BareMarker(BaseModel):
    note: Annotated[str, ValueFree("note")]


class _KnownKindNoValidator(BaseModel):
    name: Annotated[str, ValueFree("safe_name")]


class _ForgedValidator(BaseModel):
    name: Annotated[str, ValueFree("safe_name"), AfterValidator(lambda v: v)]


class _LaxInt(BaseModel):
    count: Annotated[int, Ge(0)]


class _LaxBool(BaseModel):
    flag: bool


class _LaxFloat(BaseModel):
    seconds: Annotated[float, Ge(0), AllowInfNan(False)]


@pytest.mark.parametrize(
    "scratch",
    [
        _StrField,
        _AnyField,
        _DictField,
        _ComputedStr,
        _Serialized,
        _Nested,
        _BareMarker,
        _KnownKindNoValidator,
        _ForgedValidator,
        _LaxInt,
        _LaxBool,
        _LaxFloat,
    ],
)
def test_value_free_walk_rejects_a_seeded_violation(scratch: type[BaseModel]) -> None:
    assert value_free_problems(scratch), scratch.__name__


# --- write strict, read tolerant (AT-E3) -------------------------------------------------------


def test_corpus_sample_round_trips() -> None:
    sample = load("run-result-v1.json")
    assert sample["schema"] == RUN_RESULT_SCHEMA == "decoy-run-result/v1"
    assert RunResult.model_validate(sample).to_payload() == sample
    assert RunResultRead.model_validate(sample).model_dump(mode="json", by_alias=True) == sample


def test_write_strict_read_tolerant() -> None:
    sample = load("run-result-v1.json")

    extra = copy.deepcopy(sample)
    extra["zz_extra"] = 1
    extra["tables"][0]["zz_extra"] = 1
    with pytest.raises(ValidationError):
        RunResult.model_validate(extra)
    read = RunResultRead.model_validate(extra)
    assert "zz_extra" not in read.model_dump()
    assert "zz_extra" not in read.tables[0].model_dump()

    for edit, value_of, unknown in (
        (
            lambda d: d["checks"][0].__setitem__("id", "drift_v9"),
            lambda r: r.checks[0].id,
            "drift_v9",
        ),
        (
            lambda d: d["route"].__setitem__("execution_mode", "future_route"),
            lambda r: r.route.execution_mode,
            "future_route",
        ),
        (lambda d: d.__setitem__("status", "paused"), lambda r: r.status, "paused"),
    ):
        doc = copy.deepcopy(sample)
        edit(doc)
        with pytest.raises(ValidationError):
            RunResult.model_validate(doc)
        assert value_of(RunResultRead.model_validate(doc)) == unknown

    wrong = copy.deepcopy(sample)
    wrong["tables"][0]["rows_out"] = "many"
    with pytest.raises(ValidationError):
        RunResult.model_validate(wrong)
    with pytest.raises(ValidationError):
        RunResultRead.model_validate(wrong)


# --- the schema tag (AT-E3c) -------------------------------------------------------------------


@pytest.mark.parametrize("model", [RunResult, RunResultRead])
@pytest.mark.parametrize(
    "tag",
    [
        "decoy-run-result/v2",
        "decoy-run-result/v9",
        "decoy-run-result/v1\n",
        "decoy-run-result/v10",
        "free text from a person",
        "",
        1,
        None,
    ],
)
def test_another_major_or_free_text_tag_is_refused(model: Any, tag: Any) -> None:
    doc = load("run-result-v1.json")
    doc["schema"] = tag
    with pytest.raises(ValidationError):
        model.model_validate(doc)


def test_the_tag_is_required_on_read_and_defaulted_on_write() -> None:
    doc = load("run-result-v1.json")
    del doc["schema"]
    with pytest.raises(ValidationError):
        RunResultRead.model_validate(doc)
    assert RunResult.model_validate(doc).schema_tag == RUN_RESULT_SCHEMA


# --- strict primitives (AT-E3d) ----------------------------------------------------------------

LAX = [
    (("exit_code",), False),
    (("exit_code",), True),
    (("exit_code",), "3"),
    (("exit_code",), 3.0),
    (("tables", 0, "rows_out"), True),
    (("tables", 0, "rows_out"), "5"),
    (("tables", 0, "index"), False),
    (("producer", "event_schema"), "1"),
    (("producer", "event_schema"), True),
    (("route", "chunked"), "false"),
    (("route", "chunked"), "true"),
    (("route", "chunked"), 0),
    (("route", "chunked"), 1),
    (("route", "native", "applicable"), 1),
    (("checks", 0, "blocking"), "false"),
    (("checks", 0, "counts", "pass"), "5"),
    (("warnings", 0, "count"), True),
    (("tables", 0, "elapsed_s"), "0.41"),
    (("tables", 0, "elapsed_s"), True),
    (("timings", "elapsed_s"), "1.92"),
    (("timings", "phases", 0, "elapsed_s"), "0.2"),
]


def _set(doc: dict[str, Any], path: tuple[Any, ...], value: Any) -> None:
    node: Any = doc
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value


@pytest.mark.parametrize("model", [RunResult, RunResultRead])
@pytest.mark.parametrize(("path", "value"), LAX, ids=lambda v: repr(v))
def test_lax_coercion_is_refused(model: Any, path: tuple[Any, ...], value: Any) -> None:
    doc = load("run-result-v1.json")
    _set(doc, path, value)
    with pytest.raises(ValidationError):
        model.model_validate(doc)


def test_an_integer_second_count_is_still_a_valid_duration() -> None:
    doc = load("run-result-v1.json")
    doc["timings"]["elapsed_s"] = 2
    assert RunResult.model_validate(doc).timings.elapsed_s == 2


# --- alias only, one payload form --------------------------------------------------------------


def test_the_count_key_has_one_spelling() -> None:
    doc = load("run-result-v1.json")
    counts = doc["checks"][0]["counts"]
    counts["passed"] = counts.pop("pass")
    for model in (RunResult, RunResultRead):
        with pytest.raises(ValidationError):
            model.model_validate(doc)


def test_both_models_dump_the_wire_form() -> None:
    doc = load("run-result-v1.json")
    assert RunResult.model_validate(doc).to_payload() == doc
    assert RunResultRead.model_validate(doc).to_payload() == doc


def test_reason_payload_is_checked_against_the_registry_on_write() -> None:
    RunResult.model_validate(failed_doc(live_reason()))
    for bad in (
        live_reason(code="zz_not_registered"),
        live_reason(retry_class="never"),
        live_reason(severity="info"),
        live_reason(registry_version="1.999.0"),
        {"code": "unexpected_error"},
    ):
        with pytest.raises(ValidationError):
            RunResult.model_validate(failed_doc(bad))


@pytest.mark.parametrize(
    "edit",
    [
        lambda d: d["tables"][0].__setitem__("name", "a/b"),
        lambda d: d["tables"][0]["delivery"].__setitem__("output", "/abs/out.csv"),
        lambda d: d["tables"][0]["delivery"].__setitem__("output", "../out.csv"),
        lambda d: d["tables"][0]["delivery"].__setitem__("output", "C:/x.csv"),
        lambda d: d["config"].__setitem__("file_sha256", "sha256:zz"),
        lambda d: d["timings"].__setitem__("elapsed_s", float("nan")),
        lambda d: d["timings"].__setitem__("started_at", "yesterday"),
        lambda d: d["producer"].__setitem__("cli_version", "1.0; rm -rf"),
        lambda d: d.__setitem__("invocation_id", "not-a-uuid"),
        lambda d: d["warnings"][0].__setitem__("code", "has a space"),
    ],
)
def test_value_shaped_strings_are_refused(edit: Any) -> None:
    doc = load("run-result-v1.json")
    edit(doc)
    with pytest.raises(ValidationError):
        RunResult.model_validate(doc)


def test_relative_output_path_is_kept() -> None:
    doc = load("run-result-v1.json")
    assert RunResult.model_validate(doc).tables[0].delivery.output == "out/members.csv"


@pytest.mark.parametrize(
    "version", ["1.0", "0.4.0", "1.2.3rc1", "2.0.0.post1", "0.4.0.dev3+g1a2b3c"]
)
def test_release_shaped_versions_are_kept(version: str) -> None:
    doc = load("run-result-v1.json")
    doc["producer"]["cli_version"] = version
    assert RunResult.model_validate(doc).producer.cli_version == version


@pytest.mark.parametrize(
    "version", ["1", "1.0abc", "1.0.0-", "1.0.0\n", "1.0.0.zzzz", "1.0.0+", "1.0.0+UPPER", "1.0 .0"]
)
def test_other_version_shapes_are_refused(version: str) -> None:
    doc = load("run-result-v1.json")
    doc["producer"]["engine_version"] = version
    for model in (RunResult, RunResultRead):
        with pytest.raises(ValidationError):
            model.model_validate(doc)


@pytest.mark.parametrize(
    "edit",
    [
        lambda d: d.__setitem__("status", "has a space"),
        lambda d: d.__setitem__("status", "paused\n"),
        lambda d: d.__setitem__("status", "line one\nline two"),
        lambda d: d["checks"][0].__setitem__("id", "/etc/passwd"),
        lambda d: d["checks"][0].__setitem__("id", "x" * 65),
        lambda d: d["route"].__setitem__("execution_mode", "free text, a person wrote this"),
        lambda d: d["tables"][0].__setitem__("kind", ""),
    ],
)
def test_read_refuses_free_text_where_an_enum_was(edit: Any) -> None:
    doc = load("run-result-v1.json")
    edit(doc)
    with pytest.raises(ValidationError):
        RunResultRead.model_validate(doc)


# --- cross-minor reads (AT-E3b) ----------------------------------------------------------------

CROSS = [
    "next-minor",
    "future-identity",
    "forged-semantics",
    "context-marker",
    "malformed-context",
    "other-major",
    "bad-registry-version",
    "bad-schema",
]


def _read(name: str) -> Any:
    return RunResultRead.model_validate(load(f"run-result-v1-{name}.json"))


@pytest.mark.parametrize("name", CROSS)
def test_cross_minor_documents_read_but_never_write(name: str) -> None:
    doc = _read(name)
    assert doc.status == "failed"
    with pytest.raises(ValidationError):
        RunResult.model_validate(load(f"run-result-v1-{name}.json"))


def test_next_minor_keeps_the_local_identity_and_the_written_version() -> None:
    reason = _read("next-minor").failure.reason
    expected = ReasonRecord.for_reason(decoy.UNEXPECTED_ERROR).to_payload()
    assert reason == {**expected, "written_registry_version": "1.999.0"}
    assert reason["registry_version"] == _registry.REGISTRY_VERSION


def test_future_identity_degrades_with_the_identity_only_in_sanitized_context() -> None:
    reason = _read("future-identity").failure.reason
    assert reason["code"] == "unknown_reason_from_future"
    assert reason["context"]["original_identity"] == ["decoy", "zz_future_reason_code"]


def test_forged_semantics_read_as_the_registry_says() -> None:
    reason = _read("forged-semantics").failure.reason
    expected = ReasonRecord.for_reason(decoy.UNEXPECTED_ERROR).to_payload()
    # The corpus document was written at registry 1.5.0 and is frozen; the reader keeps the
    # version the writer stamped, whatever the current registry is.
    assert reason == {**expected, "written_registry_version": "1.5.0"}
    assert reason["registry_version"] == _registry.REGISTRY_VERSION


def test_context_marker_never_survives() -> None:
    doc = _read("context-marker")
    assert MARKER not in json.dumps(doc.model_dump(mode="json"))
    assert doc.failure.reason["context"]["counts"] == {"rows": 2}


def test_malformed_context_reads_empty() -> None:
    context = _read("malformed-context").failure.reason["context"]
    assert context["counts"] == {}
    assert context["object_ref"] is None
    assert MARKER not in json.dumps(context)


@pytest.mark.parametrize("name", ["other-major", "bad-registry-version", "bad-schema"])
def test_unsupported_reasons_read_as_null(name: str) -> None:
    doc = _read(name)
    assert doc.failure.reason is None
    assert doc.failure.category == "runtime"
    assert doc.tables[1].reason is None


@pytest.mark.parametrize(
    "identity", [decoy.REASON_RECORD_INVALID, decoy.UNSUPPORTED_SCHEMA_VERSION]
)
def test_a_diagnostic_reason_reads_as_null(identity: Any) -> None:
    """A valid-looking record whose code is a parse diagnostic never carried a usable reason."""
    payload = ReasonRecord.for_reason(identity).to_payload()
    doc = failed_doc(payload)
    RunResult.model_validate(doc)
    read = RunResultRead.model_validate(doc)
    assert read.failure.reason is None


def test_a_trailing_newline_is_not_a_version() -> None:
    record, written = ReasonRecord.parse_payload(live_reason(registry_version="1.999.0\n"))
    assert (record.code, written) == ("unsupported_schema_version", None)
    assert (
        RunResultRead.model_validate(
            failed_doc(live_reason(registry_version="1.5.0\n"))
        ).failure.reason
        is None
    )


def test_parse_payload_returns_the_canonical_record_and_the_written_version() -> None:
    good = live_reason()
    record, written = ReasonRecord.parse_payload(good)
    assert record.to_payload() == good
    assert written == _registry.REGISTRY_VERSION

    record, written = ReasonRecord.parse_payload(live_reason(registry_version="1.999.0"))
    assert (record.code, written) == ("unexpected_error", "1.999.0")

    record, written = ReasonRecord.parse_payload(live_reason(retry_class="never"))
    assert record.to_payload() == good and written == _registry.REGISTRY_VERSION

    for bad, code in (
        ("not a mapping", "reason_record_invalid"),
        (live_reason(registry_version="2.0.0"), "unsupported_schema_version"),
        (live_reason(registry_version="1.x.0"), "unsupported_schema_version"),
        (live_reason(registry_version=7), "unsupported_schema_version"),
        (live_reason(schema_version=2), "unsupported_schema_version"),
    ):
        record, written = ReasonRecord.parse_payload(bad)
        assert (record.code, written) == (code, None), bad


def test_from_payload_is_the_parser_without_the_version() -> None:
    for payload in (live_reason(), live_reason(registry_version="1.999.0"), "x", {}):
        assert ReasonRecord.from_payload(payload) == ReasonRecord.parse_payload(payload)[0]


def _function_node(obj: Any) -> ast.FunctionDef:
    tree = ast.parse(textwrap.dedent(inspect.getsource(obj)))
    node = tree.body[0]
    assert isinstance(node, ast.FunctionDef)
    return node


def _calls_named(node: ast.AST, name: str) -> int:
    return sum(
        1
        for n in ast.walk(node)
        if isinstance(n, ast.Call) and getattr(n.func, "attr", getattr(n.func, "id", "")) == name
    )


def _compat_literals(node: ast.AST) -> list[str]:
    hits = [
        n.value
        for n in ast.walk(node)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and n.value in {"schema_version", "registry_version"}
    ]
    hits += [
        n.attr
        for n in ast.walk(node)
        if isinstance(n, ast.Attribute)
        and n.attr in {"_VERSION", "REGISTRY_VERSION", "REGISTRY_VERSION_RE"}
    ]
    hits += [
        n.id
        for n in ast.walk(node)
        if isinstance(n, ast.Name) and n.id in {"_VERSION", "REGISTRY_VERSION_RE"}
    ]
    return hits


def test_one_reason_parser() -> None:
    """G-ONE-REASON-PARSER: ``from_payload`` only delegates; the read side checks nothing itself."""
    body = _function_node(ReasonRecord.from_payload).body
    statements = [
        s for s in body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
    ]
    assert len(statements) == 1 and isinstance(statements[0], ast.Return)
    assert _calls_named(statements[0], "parse_payload") == 1
    assert _compat_literals(statements[0]) == []

    reader = _function_node(_model.read_reason)
    assert _calls_named(reader, "parse_payload") == 1
    assert _compat_literals(reader) == []


# --- read tree derived from the write tree (AT-E18) --------------------------------------------

_ENUM_LIKE = "enum-like"
_READ_KINDS = {"reason_payload_read": "reason_payload", "token": _ENUM_LIKE}


def _shape(ann: Any, meta: tuple[Any, ...], read: bool) -> Any:
    marks = [m for m in meta if isinstance(m, ValueFree)]
    if marks:
        kind = marks[0].kind
        return _READ_KINDS.get(kind, kind) if read else kind
    origin = get_origin(ann)
    if origin is Annotated:
        base, *extra = get_args(ann)
        return _shape(base, meta + tuple(extra), read)
    if _is_union(origin):
        return ("union", tuple(_shape(a, meta, read) for a in get_args(ann)))
    if origin in (list, tuple, set, frozenset, dict):
        return (
            origin.__name__,
            tuple(_shape(a, meta, read) for a in get_args(ann) if a is not Ellipsis),
        )
    if origin is Literal or (isinstance(ann, type) and issubclass(ann, Enum)):
        return "unsubstituted-enum" if read else _ENUM_LIKE
    if isinstance(ann, type) and issubclass(ann, BaseModel):
        return _model_shape(ann, read)
    return getattr(ann, "__name__", repr(ann))


def _model_shape(model: type[BaseModel], read: bool) -> Any:
    want = "ignore" if read else "forbid"
    extra = model.model_config.get("extra")
    fields = {
        name: _shape(f.annotation, tuple(f.metadata), read)
        for name, f in model.model_fields.items()
    }
    return ("model", extra == want, tuple(sorted(fields.items(), key=str)))


def tree_differs(write: type[BaseModel], read: type[BaseModel]) -> bool:
    return _model_shape(write, False) != _model_shape(read, True)


def test_read_tree_derived_from_write_tree() -> None:
    assert not tree_differs(RunResult, RunResultRead)


def test_tree_walk_sees_a_hand_written_read_tree() -> None:
    class W(BaseModel):
        model_config = ConfigDict(extra="forbid")
        a: Literal["x", "y"]
        b: int

    class Inner(BaseModel):
        model_config = ConfigDict(extra="forbid")
        k: Literal["x"]
        n: int

    class WNested(BaseModel):
        model_config = ConfigDict(extra="forbid")
        inner: Inner

    class RGood(BaseModel):
        model_config = ConfigDict(extra="ignore")
        a: Token
        b: int

    class RBareStr(BaseModel):
        model_config = ConfigDict(extra="ignore")
        a: str
        b: int

    class RMissing(BaseModel):
        model_config = ConfigDict(extra="ignore")
        a: Token

    class RUnsub(BaseModel):
        model_config = ConfigDict(extra="ignore")
        a: Literal["x", "y"]
        b: int

    class RStrict(BaseModel):
        model_config = ConfigDict(extra="forbid")
        a: Token
        b: int

    class InnerMissing(BaseModel):
        model_config = ConfigDict(extra="ignore")
        k: Token

    class RNested(BaseModel):
        model_config = ConfigDict(extra="ignore")
        inner: InnerMissing

    assert not tree_differs(W, RGood)
    for broken in (RMissing, RUnsub, RStrict, RBareStr):
        assert tree_differs(W, broken), broken.__name__
    assert tree_differs(WNested, RNested)


# --- AT-E5 ------------------------------------------------------------------------------------


def _profile() -> Profile:
    col = ColumnProfile(
        name="name", dtype="object", row_count=10, null_count=0, distinct_count=10,
        sampled=False, is_candidate_key_sampled=False, declared_pk=False, is_fk=False,
        fk_target=None, pii_class=None,
    )  # fmt: skip
    return Profile(
        schema_version=1,
        tables=(TableProfile(name="customers", row_count=10, columns=(col,)),),
        relationships=(),
        profiled_at=datetime(2026, 5, 27),
        decoy_engine_version="0.1.0",
    )


def _config(**edits: Any) -> dict[str, Any]:
    column = {"name": "name", "strategy": "faker_name", "provider": "person_name",
              "cardinality_mode": "reuse"}  # fmt: skip
    config: dict[str, Any] = {
        "global_settings": {"seed": 42},
        "tables": [{"name": "customers", "columns": [column]}],
    }
    config.update(edits)
    return config


def test_semantic_hash_is_plan_hash() -> None:
    config = _config()
    plan = compile_plan(config, _profile(), decoy_engine_version="0.1.0")
    assert pipeline_config_hash(config) == plan.pipeline_config_hash

    moved_source = _config(sources={"customers": {"path": "elsewhere.csv"}})
    assert pipeline_config_hash(moved_source) == pipeline_config_hash(config)

    changed = _config()
    changed["tables"][0]["columns"][0]["cardinality_mode"] = "one_to_one"
    assert pipeline_config_hash(changed) != pipeline_config_hash(config)
