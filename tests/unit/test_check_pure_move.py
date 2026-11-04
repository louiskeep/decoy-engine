"""Acceptance tests for ``scripts/check_pure_move.py`` (H-1 Wave B plan 1.3).

The excerpt is a verbatim cut of ``src/decoy_engine/detection/detectors.py`` at blob
6f771b2309ddef91868a17ddace4af79fe00d0ff, lines 26-69, 84-89, 95-97, 216-224, 241,
243-245, 261-262, 264-267, 337-342, 345-348, 350-361, 411-418, 421-429, 869, 872-884,
894-942, 943-952 and 968-980. Every split is built from it in memory; split modules are
named by repo path or bare stem so no planned module appears as a dotted string.
"""

from __future__ import annotations

import ast
import copy
import json
from pathlib import Path

from scripts.check_pure_move import check_move

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "pure_move"
EXCERPT = (FIXTURES / "detectors_excerpt.py.txt").read_text()
MANIFEST = json.loads((FIXTURES / "split_manifest.json").read_text())
IMPORTERS = json.loads((FIXTURES / "importers.json").read_text())
OLD = MANIFEST["old"]
PART_A_RULES = frozenset({"1a", "1b", "1c", "import-position", "2", "3", "4"})


def _chunks() -> dict[str, str]:
    lines = EXCERPT.splitlines(keepends=True)
    out = {}
    for stmt in ast.parse(EXCERPT).body:
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            key = stmt.names[0].asname or stmt.names[0].name
        elif isinstance(stmt, ast.Assign):
            key = stmt.targets[0].id
        else:
            key = getattr(stmt, "name", None) or stmt.target.id
        start = min([stmt.lineno, *(d.lineno for d in getattr(stmt, "decorator_list", []))])
        out[key] = "".join(lines[start - 1 : stmt.end_lineno])
    return out


CHUNKS = _chunks()
CONSTS = (
    "DEFAULT_MIN_MATCH_RATE",
    "NAME_HINT_MIN_RATE",
    "LOW_CONFIDENCE_NO_HINT_FLOOR",
    "HIGH_CONFIDENCE_NO_HINT_FLOOR",
    "HIGH_CONFIDENCE_WITH_HINT_FLOOR",
    "SAMPLE_MISS_LIMIT",
)
HEAD_IMPORTS = ("annotations", "re", "Callable", "pd", "_ADDRESS_RE", "_IBAN_COUNTRIES")
HEAD_IMPORTS += ("domain_for", "CustomDetectorSpec")
BUILTIN_NAMES = ("_variant", "_SSN_VARIANTS", "_US_PHONE_VARIANTS", "detect_email", "detect_ssn")


def path(stem: str) -> str:
    return f"src/decoy_engine/detection/{stem}.py"


def link(stem: str, *names: str) -> str:
    return f"from decoy_engine.detection.{stem} import {', '.join(names)}"


SCORING = ("annotations", "pd", *CONSTS, "_series_str")
HINTS = ("annotations", "re", "_hint", "load_name_hint_terms", "_NAME_HINTS")
HINTS += ("contextmanager", "ContextVar", "_NAME_HINT_EXTRAS")
BUILTIN = (
    "annotations",
    "re",
    "pd",
    "from decoy_engine.detection._patterns import _EMAIL_RE, _SSN_RE",
    "from decoy_engine.detection.types import DetectorMatch",
    link("_scoring", "DEFAULT_MIN_MATCH_RATE", "_series_str"),
    *BUILTIN_NAMES,
)
LINKS = (
    link("_scoring", *CONSTS, "_series_str"),
    link("_hints", "_hint", "_NAME_HINTS", "_NAME_HINT_EXTRAS"),
    link("builtin", *BUILTIN_NAMES),
)
REST = ("dataclass", "Span", "_USPS_STREET_SUFFIXES", "_STREET_ADDRESS_RE", "_SPAN_DETECTORS")
KEPT = (*HEAD_IMPORTS, *LINKS, *REST)


def src(*items: str) -> str:
    return "\n\n".join(CHUNKS.get(item, item) for item in items) + "\n"


def split(**parts: tuple[str, ...]) -> dict[str, str]:
    layout = {"_scoring": SCORING, "_hints": HINTS, "builtin": BUILTIN, "detectors": KEPT}
    return {path(stem): src(*items) for stem, items in {**layout, **parts}.items()}


def swap(items: tuple[str, ...], a: str, b: str) -> tuple[str, ...]:
    out = list(items)
    i, j = out.index(a), out.index(b)
    out[i], out[j] = out[j], out[i]
    return tuple(out)


def drop(items: tuple[str, ...], *names: str) -> tuple[str, ...]:
    return tuple(item for item in items if item not in names)


def manifest(new: tuple[str, ...] = (), drop_names: tuple[str, ...] = (), **owners: str) -> dict:
    out = copy.deepcopy(MANIFEST)
    out["new"] += [path(stem) for stem in new]
    for name in drop_names:
        del out["map"][name]
    out["map"].update({name: path(stem) for name, stem in owners.items()})
    return out


def run(head, man=None, base=EXCERPT, only=PART_A_RULES) -> list[str]:
    return check_move(man or MANIFEST, {OLD: base}.get, head.get, sorted(head), only=only)


def tags(problems: list[str]) -> set[str]:
    return {problem.split(":", 1)[0] for problem in problems}


def _address_case():
    kept = drop(KEPT, "_USPS_STREET_SUFFIXES", "_STREET_ADDRESS_RE")
    moved = ("annotations", "re", "_USPS_STREET_SUFFIXES", "_STREET_ADDRESS_RE")
    owners = {"_USPS_STREET_SUFFIXES": "_address", "_STREET_ADDRESS_RE": "_address"}
    return split(detectors=kept, _address=moved), manifest(("_address",), **owners)


def _unforced_case():
    extra = ("annotations", link("builtin", "_variant"), "_SSN_VARIANTS")
    names = drop(BUILTIN_NAMES, "_SSN_VARIANTS")
    kept = (*HEAD_IMPORTS, *LINKS[:2], link("builtin", *names), *REST)
    head = split(builtin=drop(BUILTIN, "_SSN_VARIANTS"), detectors=kept, _extra=extra)
    return head, manifest(("_extra",), _SSN_VARIANTS="_extra")


def _scoring_without(name: str) -> dict[str, str]:
    names = drop((*CONSTS, "_series_str"), name)
    kept = (*HEAD_IMPORTS, link("_scoring", *names), *LINKS[1:], *REST)
    builtin = tuple(
        link("_scoring", *drop(("DEFAULT_MIN_MATCH_RATE", "_series_str"), name))
        if item.startswith("from decoy_engine.detection._scoring")
        else item
        for item in BUILTIN
    )
    return split(_scoring=drop(SCORING, name), detectors=kept, builtin=builtin)


CHANGED = CHUNKS["DEFAULT_MIN_MATCH_RATE"].replace("= 0.5 ", "= 0.51 ")
WRONG_SOURCE = (
    *BUILTIN[:5],
    "from decoy_engine.detection._validators import DEFAULT_MIN_MATCH_RATE",
    link("_scoring", "_series_str"),
    *BUILTIN_NAMES,
)
RED_CASES = {
    "reorder_isolated": (split(builtin=swap(BUILTIN, "_SSN_VARIANTS", "_US_PHONE_VARIANTS")),),
    "reorder_span": (split(detectors=swap(KEPT, "_STREET_ADDRESS_RE", "_SPAN_DETECTORS")),),
    "wrong_source": (split(builtin=WRONG_SOURCE),),
    "dropped": (_scoring_without("SAMPLE_MISS_LIMIT"), manifest(drop_names=("SAMPLE_MISS_LIMIT",))),
    "dropped_call": (split(), None, EXCERPT + '\n_hint("x")\n'),
    "hoisted": (split(_hints=(*HINTS[:4], "ContextVar", *HINTS[4:6], HINTS[7])),),
    "duplicated": (split(detectors=(*HEAD_IMPORTS, *LINKS, "_series_str", *REST)),),
    "missing": (_scoring_without("_series_str"),),
    "changed": (split(_scoring=(*SCORING[:2], CHANGED, *SCORING[3:])),),
    "cycle_isolated": (
        split(
            _scoring=(*SCORING, link("_hints", "_hint")),
            _hints=(*HINTS[:2], link("_scoring", "_series_str"), *HINTS[2:]),
        ),
    ),
    "cycle_back": (split(_scoring=(*SCORING, link("detectors", "_SPAN_DETECTORS"))),),
    "use_before_bind_isolated": (
        split(_hints=(*HINTS[:2], "load_name_hint_terms", "_NAME_HINTS", "_hint", *HINTS[5:])),
    ),
    "use_before_bind_address": _address_case(),
    "unforced": _unforced_case(),
}


def red(case: str) -> list[str]:
    return run(*RED_CASES[case])


def test_accepts_a_pure_split():
    assert run(split()) == []


def test_full_verdict_reports_only_the_pending_rules():
    assert run(split(), only=None) == []


def test_detects_reordered_statement():
    assert tags(red("reorder_isolated")) == {"1a"}
    assert "1a" in tags(red("reorder_span"))


def test_detects_wrong_import_source():
    assert tags(red("wrong_source")) == {"2"}


def test_detects_dropped_statement():
    assert tags(red("dropped")) == {"1a"}
    assert tags(red("dropped_call")) == {"1a"}


def test_detects_hoisted_import():
    assert tags(red("hoisted")) == {"import-position"}


def test_detects_duplicated_definition():
    assert "3" in tags(red("duplicated"))


def test_detects_missing_definition():
    assert "1b" in tags(red("missing"))


def test_detects_changed_constant():
    assert tags(red("changed")) == {"1a"}


def test_detects_import_cycle():
    assert tags(red("cycle_isolated")) == {"4"}
    assert "4" in tags(red("cycle_back"))


def test_detects_use_before_bind():
    assert tags(red("use_before_bind_isolated")) == {"1c"}
    assert "1c" in tags(red("use_before_bind_address"))


def test_detects_unforced_non_inert_module():
    assert "4" in tags(red("unforced"))


def test_accepts_inert_definition_reorder():
    assert run(split(builtin=swap(BUILTIN, "detect_email", "detect_ssn"))) == []


def test_problems_carry_no_source_text():
    source_lines = {line.strip() for line in EXCERPT.splitlines()}
    source_lines = {line for line in source_lines if len(line) > 3 and not line.isidentifier()}
    for case in RED_CASES:
        problems = red(case)
        assert problems, case
        for problem in problems:
            assert not [line for line in source_lines if line in problem], (case, problem)


def with_importers(listed: list[str], files: dict[str, tuple[str, str]]) -> list[str]:
    man = manifest()
    man["importers"] = listed
    base = {OLD: EXCERPT, **{p: texts[0] for p, texts in files.items()}}
    head = {**split(), **{p: texts[1] for p, texts in files.items()}}
    return check_move(man, base.get, head.get, sorted(head), only=None)


def test_accepts_import_only_hunks():
    consumer = IMPORTERS["consumer"]
    files = {consumer["path"]: (consumer["base"], consumer["head"])}
    assert with_importers([consumer["path"]], files) == []


def test_detects_extra_importer_hunk():
    consumer = IMPORTERS["consumer"]
    files = {consumer["path"]: (consumer["base"], consumer["head_extra_hunk"])}
    assert tags(with_importers([consumer["path"]], files)) == {"5"}


def test_detects_unlisted_importer():
    other = IMPORTERS["unlisted"]
    assert tags(with_importers([], {other["path"]: (other["base"], other["head"])})) == {"5"}
    stale = {other["path"]: (other["base"], other["base"])}
    assert tags(with_importers([], stale)) == {"5"}


def test_rejects_manifest_over_file_bound():
    paths = [f"src/decoy_engine/detection/consumer_{i}.py" for i in range(7)]
    assert len({*MANIFEST["new"], *paths}) == 11
    assert tags(with_importers(paths, {p: ("X = 1\n", "X = 1\n") for p in paths})) == {"6"}


RED_TAGS = {
    "reorder_isolated": {"1a"},
    "reorder_span": {"1a"},
    "wrong_source": {"2"},
    "dropped": {"1a"},
    "dropped_call": {"1a"},
    "hoisted": {"import-position"},
    "duplicated": {"3"},
    "missing": {"1b"},
    "changed": {"1a"},
    "cycle_isolated": {"4"},
    "cycle_back": {"4"},
    "use_before_bind_isolated": {"1c"},
    "use_before_bind_address": {"1c"},
    "unforced": {"4"},
}


def test_part_a_cases_unchanged():
    from tests.unit import test_check_pure_move_scope as scope

    assert run(split(), only=None) == []
    assert run(split(builtin=swap(BUILTIN, "detect_email", "detect_ssn")), only=None) == []
    for case, expected in RED_TAGS.items():
        assert expected <= tags(run(*RED_CASES[case], only=None)), case
    shadow = ("def moved(item):", "    value = item", "    return value")
    free = ("def moved():", "    x = 1", "    def inner():", "        return x", "    return inner")
    builtin = ("def moved(x):", "    if not len(x):", "        raise ValueError(x)", "    return x")
    assert scope.moved_only(("value = 1", "item = 2"), shadow, None) == set()
    assert scope.moved_only((), free, None) == set()
    assert scope.moved_only((), builtin, None) == set()
    for moved in (scope.TOP, scope.NESTED, scope.CLASS_BODY):
        dest = scope.src(scope.FUTURE, *moved)
        base, kept = scope.src(scope.FUTURE, *moved), scope.src(scope.FUTURE, scope.LINK)
        assert scope.run(base, kept, dest, None) == set()
    assert scope.moved_only(("import re",), ("def moved():", "    return re.x"), None) == {"7"}
