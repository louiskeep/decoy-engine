"""Plan-time checks for generate columns: what the config layer cannot see (GEN-STRICT E2).

``config/_generate_options.py`` refuses unknown keys and bad static values at parse. This module
owns the rest, which needs the Faker registry, Arrow type inference, a transform's own parse
rules or the other columns of the table. ``decoy validate`` (``run_config_only_checks``) and
``decoy run`` (``compile_plan``) both call ``check_generate_columns``, so they refuse the same
configs.

Every refusal is a ``PlanCompileError`` whose message is the reason's fixed summary plus the
table, column and option names. A submitted value (a category, a formula, an op) never reaches
the text, and a translated library error is raised after its ``except`` block so no
value-bearing ``__context__`` survives.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Mapping
from functools import partial
from typing import Any

from decoy_engine import reasons
from decoy_engine.errors import ValidationError
from decoy_engine.plan._checks import check_derived_column_refs
from decoy_engine.plan._checks_derived_aggregate import check_derived_aggregate_refs
from decoy_engine.plan._checks_group_key import check_group_key_refs
from decoy_engine.plan._checks_grouped_series import check_grouped_series_refs
from decoy_engine.plan._checks_windowed_date import check_windowed_date_refs
from decoy_engine.plan._errors import PlanCompileError

__all__ = ["check_generate_columns"]

_REF_CHECKS = (
    check_derived_column_refs,
    check_derived_aggregate_refs,
    check_grouped_series_refs,
    check_windowed_date_refs,
    check_group_key_refs,
)
_PARSE_ERRORS = (PlanCompileError, ValidationError, ValueError, TypeError, OverflowError, KeyError)
_ARITHMETIC = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)
_ROW_NAMES = frozenset({"i", "index"})
# Where a translated parse error points when the library error names no option.
_PRIMARY_KEY = {"derived": "expression"}


def _code(symbol: Any) -> str:
    return str(symbol)


def _located(code: str, path: str, where: str) -> PlanCompileError:
    summary = reasons.lookup(code)[0].summary
    return PlanCompileError(code=code, path=path, message=f"{summary} At {where}.")


def _refusal(code: Any, table: str, column: str, key: str | None = None) -> PlanCompileError:
    path = f"tables.{table}.generate_columns.{column}" + (f".{key}" if key else "")
    where = f"table {table!r}, column {column!r}" + (f", option {key!r}" if key else "")
    return _located(_code(code), path, where)


def _run_ref_checks(config: dict[str, Any]) -> None:
    """The five existing reference checks, with their value-bearing messages made fixed text.

    They also cover masking columns, so this runs once on the whole config. The registered code
    and path survive; the message is rebuilt after the ``except`` block.
    """
    for check in _REF_CHECKS:
        failed: tuple[str, str | None] | None = None
        try:
            check(config)
        except PlanCompileError as exc:
            failed = (exc.code, exc.path)
        if failed is not None:
            code, path = failed
            raise _located(code, path or "tables", path or "the config")


def check_generate_columns(
    config: dict[str, Any],
    *,
    provider_snapshot: Mapping[str, Callable[..., Any]] | None = None,
) -> None:
    """Refuse a generate column the run would ignore, crash on or fill with nulls.

    Raises ``PlanCompileError`` on the first problem, naming table, column and option.
    ``provider_snapshot`` is the custom-provider view the run will use (``None``: the live one).
    """
    _run_ref_checks(config)
    tables = config.get("tables")
    if not isinstance(tables, list):
        return
    by_name = {
        t["name"]: t for t in tables if isinstance(t, dict) and isinstance(t.get("name"), str)
    }
    fakers = _FakerView(provider_snapshot)
    for table_name, table in by_name.items():
        columns = table.get("generate_columns")
        if not isinstance(columns, list):
            continue
        cols = [c for c in columns if isinstance(c, dict) and isinstance(c.get("name"), str)]
        for index in range(len(cols)):
            _check_column(table_name, cols, index, by_name, fakers)


def _check_column(
    table: str,
    cols: list[dict[str, Any]],
    index: int,
    tables: Mapping[str, dict[str, Any]],
    fakers: _FakerView,
) -> None:
    col = cols[index]
    kind = col.get("type")
    if kind == "faker":
        fakers.check(table, col)
    elif kind == "categorical":
        _check_categories(table, col)
    elif kind == "reference":
        _check_reference_weights(table, col, tables)
    _check_parse_rules(table, col)
    _check_forward_references(table, cols, index)
    if kind == "formula":
        _check_formula(table, cols, index)


# ---- faker ----------------------------------------------------------------------------------


class _FakerView:
    """Locale and provider lookups for one check pass, built once per locale."""

    def __init__(self, snapshot: Mapping[str, Callable[..., Any]] | None) -> None:
        self._snapshot = snapshot
        self._by_locale: dict[Any, tuple[Any, frozenset[str]]] = {}

    def _locale(self, locale: Any) -> tuple[Any, frozenset[str]]:
        key = tuple(locale) if isinstance(locale, list) else (locale or None)
        if key not in self._by_locale:
            from decoy_engine.internal.faker_setup import get_faker_providers, make_faker

            # One Faker per entry: a multi-locale Faker proxies attribute reads and some raise.
            fakes = [
                make_faker(entry or None)
                for entry in (locale if isinstance(locale, list) else [locale])
            ]
            names = frozenset().union(
                *(get_faker_providers(f, provider_snapshot=self._snapshot) for f in fakes)
            )
            self._by_locale[key] = (fakes[0], names)
        return self._by_locale[key]

    def check(self, table: str, col: dict[str, Any]) -> None:
        name = col["name"]
        locale = col.get("locale")
        if not self._locales_known(locale):
            raise _refusal(reasons.plan.GENERATE_LOCALE_UNKNOWN, table, name, "locale")
        fake, names = self._locale(locale)
        faker_type = col.get("faker_type", "word")
        if faker_type not in names:
            raise _refusal(reasons.plan.GENERATE_FAKER_TYPE_UNKNOWN, table, name, "faker_type")
        kwargs = col.get("faker_kwargs")
        if isinstance(kwargs, dict) and kwargs and not self._kwargs_known(fake, faker_type, kwargs):
            raise _refusal(reasons.plan.GENERATE_FAKER_KWARGS_UNKNOWN, table, name, "faker_kwargs")

    @staticmethod
    def _locales_known(locale: Any) -> bool:
        from faker.config import AVAILABLE_LOCALES

        entries = locale if isinstance(locale, list) else [locale]
        # Faker reads "en-US" as "en_US", so the check normalizes the same way.
        return all(
            not e or (isinstance(e, str) and e.replace("-", "_") in AVAILABLE_LOCALES)
            for e in entries
        )

    def _kwargs_known(self, fake: Any, faker_type: str, kwargs: dict[str, Any]) -> bool:
        from decoy_engine.generation._dob_anchor import anchored_date_of_birth
        from decoy_engine.internal.faker_setup import has_custom_faker_override, reflected_kwargs

        if has_custom_faker_override(faker_type, self._snapshot):
            return True  # a custom provider's contract is fn(faker); kwargs are not read
        attr = getattr(fake, faker_type, None)
        method = partial(anchored_date_of_birth, fake) if faker_type == "date_of_birth" else attr
        if not callable(method):
            return True
        takes_any, accepted = reflected_kwargs(method)
        return takes_any or all(k in accepted for k in kwargs)


def _check_categories(table: str, col: dict[str, Any]) -> None:
    """Refuse a list Arrow cannot type, the inference ``pa.table`` applies at run."""
    import pyarrow as pa

    categories = col.get("categories")
    if not isinstance(categories, list):
        return
    mixed = False
    try:
        pa.array(categories)
    except Exception:
        mixed = True
    if mixed:
        raise _refusal(reasons.plan.GENERATE_CATEGORIES_MIXED, table, col["name"], "categories")


# ---- transform parse rules --------------------------------------------------------------------


def _parse_rule(kind: str, col: dict[str, Any]) -> None:
    """Call the type's own ``from_dict`` with the dict its generator builds."""
    if kind == "derived":
        from decoy_engine.transforms.derived import DerivedConfig

        DerivedConfig.from_dict(
            {
                "expression": col.get("expression", ""),
                "bounds": col.get("bounds"),
                "null_propagation": col.get("null_propagation", "explicit_null"),
            }
        )
    elif kind == "derived_aggregate":
        from decoy_engine.transforms.derived_aggregate import DerivedAggregateConfig

        DerivedAggregateConfig.from_dict({"op": col.get("op", ""), "column": col.get("column", "")})
    elif kind == "grouped_series":
        from decoy_engine.transforms.grouped_series import GroupedSeriesConfig

        cfg = {
            "group_by": col.get("group_by", ""),
            "order_by": col.get("order_by", ""),
            "generator": col.get("generator", "cumcount"),
            "step": col.get("step", 1),
            "max_step": col.get("max_step", 10),
        }
        if "start" in col:
            cfg["start"] = col["start"]
        GroupedSeriesConfig.from_dict(cfg)
    elif kind == "windowed_date":
        from decoy_engine.transforms.windowed_date import WindowedDateConfig

        keys = ("min_days", "max_days", "distribution")
        WindowedDateConfig.from_dict(
            {"anchor": col.get("anchor", ""), **{k: col[k] for k in keys if k in col}}
        )
    elif kind == "group_key":
        from decoy_engine.transforms.group_key import GroupKeyConfig

        GroupKeyConfig.from_dict(
            {
                "group_by": col.get("group_by", ""),
                "length": col.get("length", 16),
                "prefix": col.get("prefix", ""),
            }
        )


def _check_parse_rules(table: str, col: dict[str, Any]) -> None:
    kind = col.get("type")
    if not isinstance(kind, str):
        return
    failure: tuple[Any, str | None] | None = None
    try:
        _parse_rule(kind, col)
    except _PARSE_ERRORS as exc:
        original = getattr(exc, "code", None)
        code = (
            original
            if reasons.is_registered(original)
            else _code(reasons.plan.GENERATE_OPTION_INVALID)
        )
        path = getattr(exc, "path", None)
        key = (
            path.split(".", 1)[1]
            if isinstance(path, str) and path.startswith("provider_config.")
            else None
        )
        failure = (code, key or _PRIMARY_KEY.get(kind))
    # Raised here, outside the except block, so the library's value-bearing text is not chained.
    if failure is not None:
        raise _refusal(failure[0], table, col["name"], failure[1])


# ---- forward references -----------------------------------------------------------------------


def _expression_refs(expression: str) -> frozenset[str]:
    from decoy_engine.expressions import compile_expr
    from decoy_engine.transforms.derived import _get_column_refs

    try:
        return frozenset(_get_column_refs(compile_expr(expression)))
    except Exception:
        return frozenset()


def _referenced(kind: str | None, value: Any) -> frozenset[str]:
    if kind == "column" and isinstance(value, str):
        return frozenset({value})
    if kind == "column_list" and isinstance(value, list):
        return frozenset(v for v in value if isinstance(v, str))
    if kind == "expression" and isinstance(value, str):
        return _expression_refs(value)
    return frozenset()


def _check_forward_references(table: str, cols: list[dict[str, Any]], index: int) -> None:
    """A column the registry marks as a reference must be generated earlier in the table.

    A generate table holds only generate columns, so any name not among the earlier ones is
    missing or declared later. The generators read siblings in declared order, so a later or
    missing name gives nulls, an empty aggregate, one group or a crash, never the intended value.
    This is the single owner of that rule, including `statistical.condition_on`.
    """
    from decoy_engine.config._generate_options import GENERATE_OPTIONS

    col = cols[index]
    spec = GENERATE_OPTIONS.get(str(col.get("type")))
    if spec is None:
        return
    earlier = {c["name"] for c in cols[:index]}
    for key, option in spec.keys.items():
        if any(r not in earlier for r in _referenced(option.ref_kind, col.get(key))):
            raise _refusal(reasons.plan.GENERATE_COLUMN_FORWARD_REFERENCE, table, col["name"], key)


# ---- formulas ---------------------------------------------------------------------------------


def _unbound_reads(tree: ast.AST) -> set[str]:
    """Names the expression reads that it does not bind itself (comprehension targets, lambdas)."""
    bound: set[str] = set()
    loads: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            (loads if isinstance(node.ctx, ast.Load) else bound).add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
    return loads - bound


def _braced_sibling(node: ast.AST, siblings: set[str]) -> bool:
    """The `{a}` form: a one-element set holding a bare sibling column name."""
    return (
        isinstance(node, ast.Set)
        and len(node.elts) == 1
        and isinstance(node.elts[0], ast.Name)
        and node.elts[0].id in siblings
    )


def _uses_braced_form(tree: ast.Expression, siblings: set[str]) -> bool:
    if _braced_sibling(tree.body, siblings):
        return True
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, _ARITHMETIC):
            for mine, other in ((node.left, node.right), (node.right, node.left)):
                if _braced_sibling(mine, siblings) and not isinstance(other, ast.Set):
                    return True
    return False


def _has_references(col: dict[str, Any]) -> bool:
    return col.get("type") == "formula" and bool(col.get("formula")) and bool(col.get("references"))


def _check_formula(table: str, cols: list[dict[str, Any]], index: int) -> None:
    from decoy_engine.generators._formula import FORMULA_SCOPE_NAMES

    col = cols[index]
    text = col.get("formula")
    if not isinstance(text, str):
        return
    try:
        tree = ast.parse(text.strip(), mode="eval")
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return  # the config layer refuses an unparsable formula
    name = col["name"]
    names = [c["name"] for c in cols]
    siblings = set(names)
    raw = col.get("references")
    declared = [r for r in raw if isinstance(r, str)] if isinstance(raw, list) else []

    for position, ref in enumerate(declared):
        if ref not in siblings:
            code = reasons.plan.GENERATE_FORMULA_REFERENCE_UNKNOWN
            raise _refusal(code, table, name, f"references.{position}")
    if _uses_braced_form(tree, siblings):
        raise _refusal(reasons.plan.GENERATE_FORMULA_TEMPLATE_SYNTAX, table, name, "formula")
    reads = _unbound_reads(tree)
    for read in sorted(reads):
        if read in declared or read in FORMULA_SCOPE_NAMES or read in _ROW_NAMES:
            continue
        code = (
            reasons.plan.GENERATE_FORMULA_REFERENCE_MISSING
            if read in siblings
            else reasons.plan.GENERATE_FORMULA_NAME_UNKNOWN
        )
        raise _refusal(code, table, name, "formula")
    # The post-pass fills reference-bearing formulas once, in declared order; a read of the
    # column itself or of one declared at or after it sees the None placeholder.
    for position, ref in enumerate(declared):
        if ref not in reads:
            continue
        target = names.index(ref)
        if target == index or (target > index and _has_references(cols[target])):
            code = reasons.plan.GENERATE_COLUMN_FORWARD_REFERENCE
            raise _refusal(code, table, name, f"references.{position}")


# ---- reference weights ------------------------------------------------------------------------


def _check_reference_weights(
    table: str, col: dict[str, Any], tables: Mapping[str, dict[str, Any]]
) -> None:
    """Weights against a `sequence` parent must match its row count; other parents fall back at run."""
    weights = col.get("weights")
    if col.get("distribution") != "weighted" or not isinstance(weights, list):
        return
    parent_name = col.get("reference_table")
    parent = tables.get(parent_name) if isinstance(parent_name, str) else None
    rows = parent.get("row_count") if parent else None
    if parent is None or not isinstance(rows, int) or isinstance(rows, bool):
        return
    for candidate in parent.get("generate_columns") or []:
        if not isinstance(candidate, dict) or candidate.get("name") != col.get("reference_column"):
            continue
        keeps_all_keys = not (candidate.get("null_probability") or 0) > 0
        if candidate.get("type") == "sequence" and keeps_all_keys and len(weights) != rows:
            raise _refusal(
                reasons.plan.REFERENCE_WEIGHTS_LENGTH_MISMATCH, table, col["name"], "weights"
            )
