"""The public strategy catalog: declarations on the handlers, and the rows built from them.

AT-E0a: every handler declares the four catalog attributes in its own class body, so no
inherited default can stand in for a decision, and `strategy_catalog()` has one row per handler.
AT-E0b: every offered row builds a column entry from its own examples that validates, compiles
and runs on that strategy's existing fixture data; dropping a required option or misplacing an
option fails; the join-safe set is the injective, deterministic pair the native FK route trusts.
Option-value refusals (wrong type, unknown option, invalid choice) belong to the entry builder
that consumes the catalog, not to this module.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pydantic
import pytest

import decoy_engine
from decoy_engine import DecoyError
from decoy_engine.detection.ner import NER_ENTITY_MAP, model_installed, spacy_installed
from decoy_engine.execution._sequential_native import _KEY_STRATEGIES
from decoy_engine.execution._strategies import (
    SCALAR_HANDLERS,
    StrategyInfo,
    StrategyOption,
    _catalog_row,
    strategy_catalog,
)
from tests.unit.execution import _auto_chunk_strategies as chunk_fixtures
from tests.unit.execution import _auto_chunk_support as support
from tests.unit.execution import _multi_table_sentry_fixtures as extra_fixtures

ATTRIBUTES = ("namespace_required", "join_safe", "wizard_options", "offer_in_wizard")
OFFERED = frozenset(
    {
        "passthrough",
        "redact",
        "truncate",
        "faker",
        "hash",
        "bucketize",
        "top_code",
        "shuffle",
        "categorical",
        "date_shift",
        "fpe",
        "text_redact",
        "text_mask",
        "geo_generalize",
        "code_set",
        "bucket_perturb",
    }
)
NOT_OFFERED = frozenset(
    {
        "formula",
        "nested",
        "joint_mask",
        "derived",
        "derived_aggregate",
        "grouped_series",
        "windowed_date",
        "group_key",
        "tokenize",
    }
)
JOIN_SAFE = frozenset({"hash", "fpe"})
NER_CAPABLE = frozenset({"text_redact", "text_mask"})
NER_READY = spacy_installed() and model_installed()

# Offered strategy -> the existing fixture whose "val" column is the right dtype for it.
FIXTURE_KEY = {
    "passthrough": ("chunk", "passthrough"),
    "redact": ("chunk", "redact"),
    "truncate": ("chunk", "truncate"),
    "faker": ("chunk", "faker:deterministic_native"),
    "hash": ("chunk", "hash"),
    "bucketize": ("chunk", "bucketize"),
    "top_code": ("chunk", "top_code"),
    "shuffle": ("extra", "shuffle:deterministic"),
    "categorical": ("chunk", "categorical:deterministic"),
    "date_shift": ("chunk", "date_shift:explicit_format"),
    "fpe": ("chunk", "fpe"),
    "text_redact": ("chunk", "text_redact"),
    "text_mask": ("chunk", "text_mask"),
    "geo_generalize": ("extra", "geo_generalize:zip"),
    "code_set": ("chunk", "code_set:mask"),
    "bucket_perturb": ("chunk", "bucket_perturb:explicit_format"),
}

CATALOG = strategy_catalog()
OFFERED_ROWS = [info for info in CATALOG if info.offer_in_wizard]
REQUIRED_PAIRS = [(i.name, o) for i in OFFERED_ROWS for o in i.required_options]


def _row(name: str) -> StrategyInfo:
    return next(info for info in CATALOG if info.name == name)


def _source(name: str) -> pa.Array:
    group, key = FIXTURE_KEY[name]
    fixtures = (
        chunk_fixtures.STRATEGY_FIXTURES if group == "chunk" else extra_fixtures.EXTRA_FIXTURES
    )
    return fixtures[key][1]["val"]


def _entry(info: StrategyInfo, *, drop: str | None = None, ner: bool = True) -> dict[str, Any]:
    """A column entry for `val` built only from the row's examples and placements."""
    entry: dict[str, Any] = {"name": "val", "strategy": info.name}
    provider_config: dict[str, Any] = {}
    ner_config: dict[str, Any] = {}
    for option in info.options:
        if option.name == drop:
            continue
        value = info.example_options[option.name]
        if option.location == "column":
            entry[option.name] = value
        elif option.location == "provider_config":
            provider_config[option.name] = value
        elif ner:
            ner_config[option.name.removeprefix("ner_")] = value
    if ner_config:
        provider_config["ner"] = ner_config
    if provider_config:
        entry["provider_config"] = provider_config
    if info.namespace_required:
        entry["namespace"] = "val"
    return entry


def _run(entry: dict[str, Any], values: pa.Array, tmp_path: Path) -> pa.Table:
    source = pa.table({"val": values})
    cfg = support.make_cfg([entry], path=support.write_source(source, tmp_path / "s.parquet"))
    return support.run_full_frame(cfg, source).outputs[support.TABLE]


# --- AT-E0a ---------------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(SCALAR_HANDLERS))
def test_every_handler_declares_the_attributes_in_its_own_class_body(name: str) -> None:
    declared = vars(type(SCALAR_HANDLERS[name]))
    for attribute in ATTRIBUTES:
        assert attribute in declared, f"{name} does not declare {attribute}"
    assert isinstance(declared["namespace_required"], bool)
    assert isinstance(declared["join_safe"], bool)
    assert isinstance(declared["offer_in_wizard"], bool)
    assert isinstance(declared["wizard_options"], tuple)
    assert all(isinstance(o, StrategyOption) for o in declared["wizard_options"])


def test_one_row_per_handler_with_matching_names_sorted() -> None:
    names = [info.name for info in CATALOG]
    assert names == sorted(names)
    assert set(names) == set(SCALAR_HANDLERS)
    assert len(names) == len(SCALAR_HANDLERS) == 25


@pytest.mark.parametrize("info", CATALOG, ids=lambda i: i.name)
def test_rows_mirror_their_handler_and_have_unique_option_names(info: StrategyInfo) -> None:
    declared = vars(type(SCALAR_HANDLERS[info.name]))
    assert info.namespace_required is declared["namespace_required"]
    assert info.join_safe is declared["join_safe"]
    assert info.offer_in_wizard is declared["offer_in_wizard"]
    assert info.options == declared["wizard_options"]
    names = [o.name for o in info.options]
    assert len(names) == len(set(names))


def test_a_handler_without_its_own_attributes_is_refused() -> None:
    class Bare:
        name = "bare"

    with pytest.raises(TypeError, match="namespace_required"):
        _catalog_row(Bare())  # type: ignore[arg-type]


def test_inherited_attributes_do_not_satisfy_the_catalog() -> None:
    class Child(type(SCALAR_HANDLERS["hash"])):  # type: ignore[misc]
        name = "child"

    with pytest.raises(TypeError, match="does not declare"):
        _catalog_row(Child())


def test_a_handler_added_to_the_table_gets_a_row_or_breaks_the_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Bare:
        name = "bare"

    monkeypatch.setitem(SCALAR_HANDLERS, "bare", Bare())
    with pytest.raises(TypeError):
        strategy_catalog()


def test_a_duplicate_option_name_is_refused() -> None:
    handler_type = type("Dup", (), {})
    option = StrategyOption("x", "int", False, "provider_config", None, 1)
    handler_type.name = "dup"  # type: ignore[attr-defined]
    handler_type.namespace_required = False  # type: ignore[attr-defined]
    handler_type.join_safe = False  # type: ignore[attr-defined]
    handler_type.wizard_options = (option, option)  # type: ignore[attr-defined]
    handler_type.offer_in_wizard = False  # type: ignore[attr-defined]
    with pytest.raises(TypeError, match="duplicate option names"):
        _catalog_row(handler_type())


def test_the_catalog_is_exported() -> None:
    assert decoy_engine.strategy_catalog is strategy_catalog
    assert decoy_engine.StrategyInfo is StrategyInfo
    assert decoy_engine.StrategyOption is StrategyOption


# --- option metadata ------------------------------------------------------------------------

_EXAMPLE_TYPE = {
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "str_list": tuple,
    "float_list": tuple,
}


@pytest.mark.parametrize("info", CATALOG, ids=lambda i: i.name)
def test_examples_match_their_wire_type_and_choices(info: StrategyInfo) -> None:
    for o in info.options:
        assert type(o.example) is _EXAMPLE_TYPE[o.wire_type], (info.name, o.name)
        if o.wire_type == "str_list":
            assert all(type(e) is str for e in o.example)  # type: ignore[union-attr]
        if o.wire_type == "float_list":
            assert all(type(e) is float for e in o.example)  # type: ignore[union-attr]
        if o.choices is not None:
            elements = o.example if isinstance(o.example, tuple) else (o.example,)
            assert set(elements) <= set(o.choices), (info.name, o.name)
        assert isinstance(
            info.example_options[o.name], list if isinstance(o.example, tuple) else type(o.example)
        )


def test_required_options_lists_exactly_the_required_ones() -> None:
    assert _row("truncate").required_options == ("length",)
    assert _row("top_code").required_options == ("cap", "over_label")
    assert _row("passthrough").required_options == ()


def test_ner_options_are_declared_once_on_the_two_text_strategies() -> None:
    entities = tuple(sorted(set(NER_ENTITY_MAP.values())))
    for info in CATALOG:
        ner = [o for o in info.options if o.location == "provider_config.ner"]
        if info.name not in NER_CAPABLE:
            assert ner == []
            continue
        assert [(o.name, o.wire_type, o.required) for o in ner] == [
            ("ner_model", "str", False),
            ("ner_entities", "str_list", False),
        ]
        assert ner[1].choices == entities == ("location", "person_name")


# --- AT-E0b ---------------------------------------------------------------------------------


def test_the_offered_set_is_the_sixteen_simple_strategies() -> None:
    assert OFFERED_ROWS
    assert {i.name for i in OFFERED_ROWS} == OFFERED
    assert {i.name for i in CATALOG if not i.offer_in_wizard} == NOT_OFFERED
    assert set(SCALAR_HANDLERS) == OFFERED | NOT_OFFERED


def test_every_offered_strategy_has_a_fixture() -> None:
    assert set(FIXTURE_KEY) == OFFERED


@pytest.mark.parametrize("info", OFFERED_ROWS, ids=lambda i: i.name)
def test_an_entry_built_from_the_examples_validates_compiles_and_runs(
    info: StrategyInfo, tmp_path: Path
) -> None:
    values = _source(info.name)
    out = _run(_entry(info, ner=NER_READY), values, tmp_path)
    assert out.num_rows == len(values)


@pytest.mark.parametrize(
    "info", [i for i in OFFERED_ROWS if i.name in NER_CAPABLE], ids=lambda i: i.name
)
def test_the_full_ner_entry_is_valid_config_even_without_the_model(info: StrategyInfo) -> None:
    entry = _entry(info)
    assert entry["provider_config"]["ner"] == {
        "model": "en_core_web_sm",
        "entities": ["person_name"],
    }
    support.make_cfg([entry])


@pytest.mark.skipif(not NER_READY, reason="spaCy and en_core_web_sm are not installed")
@pytest.mark.parametrize("name", sorted(NER_CAPABLE))
def test_the_ner_entry_runs_when_the_model_is_installed(name: str, tmp_path: Path) -> None:
    assert _run(_entry(_row(name)), _source(name), tmp_path).num_rows > 0


@pytest.mark.parametrize(("name", "dropped"), REQUIRED_PAIRS)
def test_dropping_a_required_option_fails(name: str, dropped: str, tmp_path: Path) -> None:
    entry = _entry(_row(name), drop=dropped, ner=False)
    with pytest.raises((DecoyError, pydantic.ValidationError)):
        _run(entry, _source(name), tmp_path)


@pytest.mark.parametrize(
    ("name", "moved"),
    [(i.name, o.name) for i in OFFERED_ROWS for o in i.options if o.location != "column"],
)
def test_an_option_moved_to_the_column_level_is_refused(name: str, moved: str) -> None:
    entry = _entry(_row(name), drop=moved)
    entry[moved] = _row(name).example_options[moved]
    with pytest.raises(pydantic.ValidationError):
        support.make_cfg([entry])


def test_the_native_key_strategies_are_the_join_safe_rows() -> None:
    assert set(_KEY_STRATEGIES) == {i.name for i in CATALOG if i.join_safe} == JOIN_SAFE


def _distinct_values(name: str) -> pa.Array:
    if name == "hash":
        return pa.array([f"user{i}@example.com" for i in range(1000)])
    return pa.array([f"{i:09d}" for i in range(1000)])


@pytest.mark.parametrize("name", sorted(JOIN_SAFE))
def test_a_join_safe_strategy_maps_1000_distinct_values_to_1000_distinct_outputs(
    name: str, tmp_path: Path
) -> None:
    values = _distinct_values(name)
    first = _run(_entry(_row(name)), values, tmp_path)["val"].to_pylist()
    second = _run(_entry(_row(name)), values, tmp_path)["val"].to_pylist()
    assert len(set(first)) == 1000
    assert first == second


@pytest.mark.parametrize("name", sorted(JOIN_SAFE))
def test_a_join_safe_strategy_maps_a_repeated_input_to_one_output(
    name: str, tmp_path: Path
) -> None:
    distinct = _distinct_values(name).to_pylist()
    out = _run(_entry(_row(name)), pa.array(distinct + distinct[:50]), tmp_path)["val"].to_pylist()
    assert out[:50] == out[1000:]


def test_hash_without_a_namespace_fails_with_its_own_error(tmp_path: Path) -> None:
    entry = _entry(_row("hash"))
    del entry["namespace"]
    with pytest.raises(DecoyError) as caught:
        _run(entry, _source("hash"), tmp_path)
    assert "hash_requires_namespace" in str(caught.value) or (
        getattr(caught.value, "code", None) == "hash_requires_namespace"
    )


# Strategies whose example needs a namespace to run; code_set reads it only in gen mode.
NAMESPACE_BITES = sorted(
    i.name for i in OFFERED_ROWS if i.namespace_required and i.name != "code_set"
)


@pytest.mark.parametrize("name", NAMESPACE_BITES)
def test_a_namespace_required_strategy_fails_without_one(name: str, tmp_path: Path) -> None:
    entry = _entry(_row(name), ner=False)
    del entry["namespace"]
    with pytest.raises((DecoyError, pydantic.ValidationError)):
        _run(entry, _source(name), tmp_path)


def test_code_set_needs_a_namespace_only_in_gen_mode(tmp_path: Path) -> None:
    info = _row("code_set")
    assert info.namespace_required
    entry = _entry(info)
    del entry["namespace"]
    _run(entry, _source("code_set"), tmp_path)
    entry["provider_config"]["mode"] = "gen"
    with pytest.raises(DecoyError):
        _run(entry, _source("code_set"), tmp_path)


def test_strategies_that_never_read_a_namespace_run_without_one(tmp_path: Path) -> None:
    for info in OFFERED_ROWS:
        if info.namespace_required:
            continue
        entry = _entry(info, ner=False)
        assert "namespace" not in entry
        _run(entry, _source(info.name), tmp_path)
