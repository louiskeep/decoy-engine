"""DPS Scope B step 1: prove the OpenDP + dp_accounting dependency shape
before any adapter code exists to violate it (guide section 3.2/5 step 1).

These are the mechanical form of the section 3.1 STOP condition: they must
land before `quality/dp.py`/`quality/dp_budget.py` are touched, so a later
regression that reaches for `opendp[polars]` or `opendp.extras` fails here
first, on a clear assertion, rather than as a runtime FFI crash.
"""

from __future__ import annotations

import ast
import importlib
import importlib.metadata
import pathlib

import opendp.prelude as dp
import pytest

import decoy_engine


def test_opendp_supported_python_and_core_mechanisms_import():
    """Both mechanism chains (guide sections 4.4/4.5) build and run under
    `contrib` alone, and each reports its privacy loss via `Measurement.map(1)`
    with the shape the guide requires: numeric/count chains report a scalar
    epsilon, the thresholded categorical chain reports an (epsilon, delta)
    pair. Built on an empty vector too (section 3.2 item 3): OpenDP's
    thresholded/count mechanisms accept degenerate input directly, so there
    is no fit-time special case for all-null / all-empty columns."""
    dp.enable_features("contrib")
    import opendp.measurements as meas
    import opendp.transformations as tf

    # Numeric chain (section 4.4): make_find_bin >> then_count_by_categories
    # >> then_laplace. Interior edges only (3 edges -> 4 bins).
    domain = dp.vector_domain(dp.atom_domain(T=float, nan=False))
    metric = dp.symmetric_distance()
    numeric_bins = 4
    interior_edges = [10.0, 20.0, 30.0]
    numeric_transform = tf.make_find_bin(domain, metric, edges=interior_edges) >> (
        tf.then_count_by_categories(categories=list(range(numeric_bins)), null_category=False)
    )
    numeric_meas = numeric_transform >> meas.then_laplace(scale=1.0)
    assert len(numeric_meas.invoke([])) == numeric_bins
    numeric_loss = numeric_meas.map(1)
    assert isinstance(numeric_loss, float)

    # Categorical grouped-count chain (section 4.5): make_count_by >>
    # then_laplace_threshold. Empty input returns {} (no separate branch).
    cat_domain = dp.vector_domain(dp.atom_domain(T=str))
    count_by = tf.make_count_by(cat_domain, metric)
    grouped_meas = count_by >> meas.then_laplace_threshold(scale=1.0, threshold=3)
    assert grouped_meas.invoke([]) == {}
    grouped_loss = grouped_meas.map(1)
    assert isinstance(grouped_loss, tuple) and len(grouped_loss) == 2

    # Categorical non-null-total count chain (section 4.5): make_count >>
    # then_laplace.
    counter = tf.make_count(cat_domain, metric, TO=int)
    count_meas = counter >> meas.then_laplace(scale=1.0)
    assert isinstance(count_meas.invoke([]), int)
    count_loss = count_meas.map(1)
    assert isinstance(count_loss, float)


def test_dp_accounting_composes_mixed_epsilon_and_epsilon_delta_certificates():
    """`dp_accounting.pld` composes one scalar-epsilon certificate and one
    (epsilon, delta) certificate into one fit-wide epsilon at a fixed delta
    (guide section 3.3). Both certificates go through the SAME dominating-
    pair constructor (`from_privacy_parameters`) -- there is no separate
    `DpEvent` path for pure-epsilon columns, per section 3.3's "one uniform
    representation for every column" rule."""
    from dp_accounting.pld import common
    from dp_accounting.pld import privacy_loss_distribution as pldist

    fit_delta = 1e-6
    scalar_epsilon_certificate = 0.4  # e.g. a numeric column's certified map(1)
    pair_certificate = (0.3, 5e-7)  # e.g. a thresholded categorical column's map(1)

    pld_a = pldist.from_privacy_parameters(
        common.DifferentialPrivacyParameters(scalar_epsilon_certificate, 0.0),
        value_discretization_interval=1e-4,
    )
    pld_b = pldist.from_privacy_parameters(
        common.DifferentialPrivacyParameters(*pair_certificate),
        value_discretization_interval=1e-4,
    )
    composed = pld_a.compose(pld_b)
    epsilon_total = composed.get_epsilon_for_delta(fit_delta)

    assert isinstance(epsilon_total, float)
    # Composition strictly weakens (or exactly preserves at the floor); a
    # correct composed loss over two nonzero certificates must exceed either
    # one alone.
    assert epsilon_total > scalar_epsilon_certificate
    assert epsilon_total > pair_certificate[0]

    # self_compose(k) matches manually composing k identical certificates
    # (guide section 3.3: "the same result and much faster").
    manual = pld_a
    for _ in range(2):
        manual = manual.compose(pld_a)
    assert pld_a.self_compose(3).get_epsilon_for_delta(fit_delta) == pytest.approx(
        manual.get_epsilon_for_delta(fit_delta)
    )


def _imported_top_level_names(module_name: str) -> set[str]:
    """Every module named by a top-level `import`/`from ... import` in the
    given module's SOURCE (not its runtime `sys.modules`, which is
    contaminated by `decoy_engine/__init__.py` eagerly loading the Polars
    execution adapter for unrelated reasons -- the package legitimately
    depends on Polars for its execution substrate; the DP path must not).
    A static, source-level check is the only way to ask "does this specific
    module reach for polars/opendp.extras" independent of what the rest of
    the package does."""
    module = importlib.import_module(module_name)
    with open(module.__file__, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_dp_stack_does_not_import_polars_or_opendp_extras():
    """Mechanical form of the section 3.1 STOP condition (guide step 1,
    landed before any adapter code exists to violate it). Neither
    `quality/dp.py` nor `quality/dp_budget.py` may name `polars` or
    `opendp.extras` in a top-level import statement. `opendp[polars]` must
    also be absent from Decoy's own declared dependencies."""
    for module_name in ("decoy_engine.quality.dp", "decoy_engine.quality.dp_budget"):
        imported = _imported_top_level_names(module_name)
        assert not any(name == "polars" or name.startswith("polars.") for name in imported), (
            module_name,
            imported,
        )
        assert not any(
            name == "opendp.extras" or name.startswith("opendp.extras.") for name in imported
        ), (module_name, imported)

    # `opendp[polars]` is not part of this build's declared dependencies
    # (guide section 3.1 STOP condition): Decoy's own pyproject.toml must
    # never spell the extra.
    reqs = importlib.metadata.requires("decoy-engine") or []
    assert not any("opendp[polars]" in r for r in reqs)


def _every_top_level_import(path: pathlib.Path) -> set[str]:
    """Every module named by a top-level `import`/`from ... import` in one
    source file, walked statically via `ast` (not `sys.modules`, for the
    same contamination reason `_imported_top_level_names` above avoids
    it)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_quality_dp_budget_is_the_only_module_that_imports_opendp_anywhere():
    """H1: guide section 4.3.5 mitigation 1 requires `OpenDpReleaseSession`
    (in `quality/dp_budget.py`) to be the SOLE construction and invocation
    site for OpenDP measurements in the whole codebase -- not merely that
    `quality/dp.py` and `quality/dp_budget.py` themselves avoid `polars`/
    `opendp.extras` (the previous, narrower check). Walking every source
    file under `src/decoy_engine` and asserting `opendp` is named ONLY by
    `dp_budget.py` is the mechanical pin: a later contributor adding a
    second STATIC `opendp` import anywhere in the package fails this test.

    M-1: this is a static, `ast`-based import-shape check, and it cannot
    do better than that -- it catches a second top-level `import opendp`
    or `from opendp... import`, not a second call site reached some other
    way. `importlib.import_module("opendp.prelude")` inside a function
    body, or `from decoy_engine.quality.dp_budget import _dp` (re-
    exporting the already-imported module object rather than importing
    `opendp` by name), both construct or reach a second OpenDP entry point
    while walking clean here. Catching those needs a runtime or call-graph
    check (an `import-linter` forbidden-contract would be the natural
    enforcement point if that dependency is ever added; it is not a
    dependency of this build today, so it is not added in this pass), not
    a wider version of this static walk."""
    package_root = pathlib.Path(decoy_engine.__file__).resolve().parent
    offenders: dict[str, set[str]] = {}
    for path in sorted(package_root.rglob("*.py")):
        imported = _every_top_level_import(path)
        opendp_names = {n for n in imported if n == "opendp" or n.startswith("opendp.")}
        if not opendp_names:
            continue
        rel = path.relative_to(package_root).as_posix()
        if rel != "quality/dp_budget.py":
            offenders[rel] = opendp_names
    assert offenders == {}, (
        f"only quality/dp_budget.py may import opendp (guide section 4.3.5 mitigation 1); "
        f"found: {offenders!r}"
    )
