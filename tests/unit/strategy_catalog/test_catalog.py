"""The public strategy catalog: declarations on the handlers, and the rows built from them.

Every handler declares the four catalog attributes in its own class body, so no inherited
default can stand in for a decision, and `strategy_catalog()` has one row per handler.
Every offered row builds a column entry from its own examples that validates, compiles and runs
on that strategy's existing fixture data; dropping a required option or misplacing an option
fails; the join-safe set is the injective, deterministic pair the native FK route trusts.
Option-value refusals (wrong type, unknown option, invalid choice) belong to the entry builder
that consumes the catalog, not to this module.
"""

from __future__ import annotations

from collections.abc import Collection
from pathlib import Path
from typing import Any

import pyarrow as pa
import pydantic
import pytest

import decoy_engine
from decoy_engine import DecoyError
from decoy_engine.detection.ner import NER_ENTITY_MAP, model_installed, spacy_installed
from decoy_engine.execution._chunked_fk_vocab import (
    CHUNK_SAFE_STRATEGIES,
    NAMESPACE_REQUIRING_STRATEGIES,
)
from decoy_engine.execution._sequential_native import _KEY_STRATEGIES
from decoy_engine.execution._strategies import (
    SCALAR_HANDLERS,
    StrategyInfo,
    StrategyOption,
    _catalog_row,
    strategy_catalog,
)
from tests._ner import needs_ner
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


def _entry(
    info: StrategyInfo,
    *,
    drop: str | None = None,
    ner: bool = True,
    keep: Collection[str] | None = None,
) -> dict[str, Any]:
    """A column entry for `val` built only from the row's examples and placements.

    `keep` limits the entry to those option names (None keeps every option).
    """
    entry: dict[str, Any] = {"name": "val", "strategy": info.name}
    provider_config: dict[str, Any] = {}
    ner_config: dict[str, Any] = {}
    for option in info.options:
        if option.name == drop or (keep is not None and option.name not in keep):
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


def _run(
    entry: dict[str, Any], values: pa.Array, tmp_path: Path, *, seed: int | None = None
) -> pa.Table:
    source = pa.table({"val": values})
    cfg = support.make_cfg([entry], path=support.write_source(source, tmp_path / "s.parquet"))
    if seed is not None:
        cfg["global_settings"]["seed"] = seed
    return support.run_full_frame(cfg, source).outputs[support.TABLE]


# --- declarations and rows ---


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


# --- option metadata ---

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


# --- offered rows build and run ---


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


@needs_ner
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


def test_the_chunked_fk_namespace_set_is_the_namespace_required_chunk_safe_rows() -> None:
    expected = {i.name for i in CATALOG if i.namespace_required} & CHUNK_SAFE_STRATEGIES
    assert expected == NAMESPACE_REQUIRING_STRATEGIES == {"hash", "fpe", "date_shift"}


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


def test_no_join_safe_row_offers_an_option_that_shortens_its_output() -> None:
    shortening = {"truncate", "length", "mask_char"}
    for info in CATALOG:
        if info.join_safe:
            assert not shortening & {o.name for o in info.options}, info.name
    assert "truncate" not in {o.name for o in _row("hash").options}


def test_hash_without_a_namespace_fails_with_its_own_error(tmp_path: Path) -> None:
    entry = _entry(_row("hash"))
    del entry["namespace"]
    with pytest.raises(DecoyError) as caught:
        _run(entry, _source("hash"), tmp_path)
    assert caught.value.code == "hash_requires_namespace"  # type: ignore[attr-defined]


# code_set reads the namespace only in gen mode, which is its example mode.
NAMESPACE_BITES = sorted(i.name for i in OFFERED_ROWS if i.namespace_required)


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
    assert entry["provider_config"]["mode"] == "gen"
    del entry["namespace"]
    with pytest.raises(DecoyError) as caught:
        _run(entry, _source("code_set"), tmp_path)
    assert caught.value.code == "code_set_gen_requires_namespace"  # type: ignore[attr-defined]
    entry["provider_config"]["mode"] = "mask"
    _run(entry, _source("code_set"), tmp_path)


def test_strategies_that_never_read_a_namespace_run_without_one(tmp_path: Path) -> None:
    for info in OFFERED_ROWS:
        if info.namespace_required:
            continue
        entry = _entry(info, ner=False)
        assert "namespace" not in entry
        _run(entry, _source(info.name), tmp_path)


# --- the expected table, written out by hand from the handlers ---
# (name, wire_type, required, location, choices, example), compared by repr so that 1, 1.0 and
# True stay distinct and a choices tuple must match exactly, including its order.

_PC = "provider_config"
_NER = "provider_config.ner"
_NER_ROWS = (
    ("ner_model", "str", False, _NER, None, "en_core_web_sm"),
    ("ner_entities", "str_list", False, _NER, ("location", "person_name"), ("person_name",)),
)
EXPECTED_OPTIONS: dict[str, tuple[tuple[Any, ...], ...]] = {
    "passthrough": (),
    "redact": (("redact_with", "str", False, _PC, None, "XXXX"),),
    "truncate": (
        ("length", "int", True, _PC, None, 3),
        ("keep", "str", False, _PC, ("head", "tail"), "tail"),
        ("mask_char", "str", False, _PC, None, "*"),
    ),
    "faker": (
        ("provider", "str", True, "column", None, "person_first_name"),
        ("deterministic", "bool", False, "column", None, True),
    ),
    "hash": (),
    "bucketize": (
        ("width", "int", True, _PC, None, 10),
        ("format", "str", False, _PC, ("lower", "midpoint", "range"), "range"),
    ),
    "top_code": (
        ("cap", "int", True, _PC, None, 89),
        ("over_label", "str", True, _PC, None, "90+"),
        ("floor", "int", False, _PC, None, 40),
        ("under_label", "str", False, _PC, None, "under 40"),
    ),
    "shuffle": (("deterministic", "bool", False, "column", None, True),),
    "categorical": (
        ("categories", "str_list", True, _PC, None, ("gold", "silver", "bronze")),
        ("weights", "float_list", False, _PC, None, (0.5, 0.3, 0.2)),
        ("deterministic", "bool", False, "column", None, True),
    ),
    "date_shift": (
        ("min_days", "int", False, _PC, None, -30),
        ("max_days", "int", False, _PC, None, 30),
        ("date_format", "str", False, _PC, None, "%Y-%m-%d"),
    ),
    "fpe": (
        ("charset", "str", False, _PC, None, "digits"),
        ("preserve_separators", "bool", False, _PC, None, False),
    ),
    "text_redact": (
        ("token", "str", False, _PC, None, "[GONE]"),
        ("label_token", "bool", False, _PC, None, True),
        *_NER_ROWS,
    ),
    "text_mask": (
        ("token", "str", False, _PC, None, "[GONE]"),
        (
            "unmatched_span_policy",
            "str",
            False,
            _PC,
            ("redact", "replace_with_token"),
            "replace_with_token",
        ),
        *_NER_ROWS,
    ),
    "geo_generalize": (
        ("type", "str", True, _PC, ("lat_lng", "zip"), "zip"),
        (
            "cascade",
            "str_list",
            True,
            _PC,
            (
                "h3_resolution_5",
                "h3_resolution_7",
                "h3_resolution_9",
                "state",
                "suppress",
                "zip3",
                "zip5",
            ),
            ("zip3", "state", "suppress"),
        ),
        ("k_threshold", "int", False, _PC, None, 20000),
    ),
    "code_set": (
        ("code_set", "str", True, _PC, ("hcpcs", "icd10", "mcc", "ndc"), "icd10"),
        ("mode", "str", False, _PC, ("mask", "gen"), "gen"),
        ("chapter_preserve", "bool", False, _PC, None, True),
    ),
    "bucket_perturb": (
        ("bucket", "str", False, _PC, ("month", "quarter", "week"), "quarter"),
        ("date_format", "str", False, _PC, None, "%Y-%m-%d"),
    ),
}


def test_the_expected_table_covers_exactly_the_offered_strategies() -> None:
    assert set(EXPECTED_OPTIONS) == OFFERED


@pytest.mark.parametrize("info", OFFERED_ROWS, ids=lambda i: i.name)
def test_each_offered_row_equals_the_expected_table_exactly(info: StrategyInfo) -> None:
    actual = tuple(
        (o.name, o.wire_type, o.required, o.location, o.choices, o.example) for o in info.options
    )
    assert repr(actual) == repr(EXPECTED_OPTIONS[info.name])


# --- an optional option changes the output ---
# Options whose effect the shared fixture cannot show; each reason is why, and where a probe
# value can show it, a test below does.
NOT_VISIBLE_ON_FIXTURE = {
    ("date_shift", "date_format"): "the fixture already uses the auto-detected format",
    ("bucket_perturb", "date_format"): "the fixture already uses the auto-detected format",
    ("fpe", "charset"): "digits is the default and the fixture holds only digits",
    ("fpe", "preserve_separators"): "the digit-only fixture has no separators to preserve",
    ("geo_generalize", "k_threshold"): "the example is the engine default; probed with 1 below",
    ("text_redact", "ner_model"): "needs the spaCy model",
    ("text_redact", "ner_entities"): "needs the spaCy model",
    ("text_mask", "ner_model"): "needs the spaCy model",
    ("text_mask", "ner_entities"): "needs the spaCy model",
}
# A pair that is only valid together is added together.
PARTNERS = {("top_code", "floor"): ("under_label",), ("top_code", "under_label"): ("floor",)}
OPTIONAL_PROBES = [
    (i.name, o.name)
    for i in OFFERED_ROWS
    for o in i.options
    if not o.required
    and o.name != "deterministic"
    and (i.name, o.name) not in NOT_VISIBLE_ON_FIXTURE
]


def _behavior_source(name: str) -> pa.Array:
    # Three source values would let two category mappings coincide by chance.
    if name == "categorical":
        return pa.array([f"v{i}" for i in range(200)])
    return _source(name)


def test_the_exemptions_name_real_options() -> None:
    real = {(i.name, o.name) for i in CATALOG for o in i.options}
    assert set(NOT_VISIBLE_ON_FIXTURE) <= real
    assert set(PARTNERS) <= real


@pytest.mark.parametrize(("name", "option"), OPTIONAL_PROBES)
def test_adding_an_optional_option_changes_the_output(
    name: str, option: str, tmp_path: Path
) -> None:
    info = _row(name)
    required = set(info.required_options)
    values = _behavior_source(name)
    base = _run(_entry(info, keep=required, ner=False), values, tmp_path)
    keep = required | {option, *PARTNERS.get((name, option), ())}
    probed = _run(_entry(info, keep=keep, ner=False), values, tmp_path)
    assert probed["val"].to_pylist() != base["val"].to_pylist()


def test_geo_generalize_k_threshold_is_read(tmp_path: Path) -> None:
    # The zip5 level is kept only when enough rows share the ZIP, so it shows the threshold.
    info = _row("geo_generalize")
    entry = _entry(info, keep=set(info.required_options))
    entry["provider_config"]["cascade"] = ["zip5", "zip3", "suppress"]
    base = _run(entry, _source("geo_generalize"), tmp_path)
    entry["provider_config"]["k_threshold"] = 1
    probed = _run(entry, _source("geo_generalize"), tmp_path)
    assert probed["val"].to_pylist() != base["val"].to_pylist()


@pytest.mark.parametrize("name", ["shuffle", "categorical"])
def test_a_deterministic_column_is_stable_across_run_seeds(name: str, tmp_path: Path) -> None:
    values = _behavior_source(name)
    entry = _entry(_row(name), ner=False)
    assert entry["deterministic"] is True
    first = _run(entry, values, tmp_path, seed=1)["val"].to_pylist()
    second = _run(entry, values, tmp_path, seed=2)["val"].to_pylist()
    assert first == second


def test_a_deterministic_categorical_column_maps_equal_inputs_to_equal_outputs(
    tmp_path: Path,
) -> None:
    # A non-deterministic column keys on row position, so equal inputs at different rows differ.
    values = pa.array([f"v{i % 7}" for i in range(70)])
    out = _run(_entry(_row("categorical")), values, tmp_path)["val"].to_pylist()
    assert all(out[i] == out[i % 7] for i in range(70))
    assert len(set(out)) > 1


def test_a_deterministic_faker_column_maps_equal_inputs_to_equal_outputs(tmp_path: Path) -> None:
    # The faker pool is built from the run seed, so only the same-seed guarantee holds.
    entry = _entry(_row("faker"))
    assert entry["deterministic"] is True
    values = pa.array([f"name{i % 7}" for i in range(70)])
    out = _run(entry, values, tmp_path, seed=1)["val"].to_pylist()
    again = _run(entry, values, tmp_path, seed=1)["val"].to_pylist()
    assert out == again
    assert all(out[i] == out[i % 7] for i in range(70))
