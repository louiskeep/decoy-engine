"""Where each engine sentry's committed allowlist or baseline lives, as a set of keys.

``SOURCES`` is the single table the registry test and the one-off ``--write-initial``
generator share. Entries here are the X0a initial inventory; after that the ratchet
(``scripts/dev/ratchet_check.py``) lets a baseline only shrink.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[2]
SENTRY = ROOT / "tests" / "sentry"
BASELINES = SENTRY / "baselines"
CREATED_IN = "X0a"


def _flat(item: object) -> str:
    return "::".join(map(str, item)) if isinstance(item, (tuple, list)) else str(item)


@dataclass(frozen=True)
class InCode:
    """Keys of a module-level dict or tuple in a sentry test file."""

    test_file: str
    constant: str
    # Module attributes naming a removed surface. The residual-surface sentry bars that token in
    # tracked files, so the baseline stores a placeholder for it.
    masks: tuple[str, ...] = ()

    def _module(self) -> object:
        path = SENTRY / self.test_file
        spec = importlib.util.spec_from_file_location(f"_bl_{path.stem}", path)
        if spec is None or spec.loader is None:
            raise ImportError(path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def __call__(self) -> set[str]:
        mod = self._module()
        tokens = [getattr(mod, name) for name in self.masks]
        out = set()
        for k in getattr(mod, self.constant):
            key = _flat(k)
            for token in tokens:
                key = key.replace(token, "<removed-surface>")
            out.add(key)
        return out

    def maxes(self) -> dict[str, int]:
        """Integer values of the constant, keyed like ``__call__``: the live ``max`` per entry."""
        return {_flat(k): int(v) for k, v in getattr(self._module(), self.constant).items()}


in_code = InCode


def json_keys(rel: str, section: str, fields: tuple[str, ...]) -> Callable[[], set[str]]:
    def keys() -> set[str]:
        rows = json.loads((ROOT / rel).read_text(encoding="utf-8"))[section]
        return {"::".join(str(r.get(f, "")) for f in fields) for r in rows}

    return keys


def toml_keys(rel: str) -> Callable[[], set[str]]:
    def keys() -> set[str]:
        data = tomllib.loads((ROOT / rel).read_text(encoding="utf-8"))
        out: set[str] = set()
        for section, rows in data.items():
            for row in rows:
                if isinstance(row, dict):
                    ident = (
                        row.get("module"),
                        row.get("function"),
                        row.get("sink"),
                        row.get("ordinal"),
                    )
                    out.add(f"{section}::" + "::".join(str(p) for p in ident if p is not None))
                else:
                    out.add(f"{section}::{row}")
        return out

    return keys


def import_layer_keys(rel: str, contract: str) -> Callable[[], set[str]]:
    """``<contract>::<importer> -> <imported>`` for each ignore line of an import-linter config.

    Layer membership is not keyed here: it is the exact snapshot in ``test_import_layers.py``.
    """

    def keys() -> set[str]:
        path = SENTRY / "layout" / "_layout.py"
        spec = importlib.util.spec_from_file_location("_bl_layout", path)
        if spec is None or spec.loader is None:
            raise ImportError(path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod  # dataclasses in the module look themselves up here
        spec.loader.exec_module(mod)
        text = (ROOT / rel).read_text(encoding="utf-8")
        return {f"{contract}::{key}" for key in mod.ignore_keys(text)}

    return keys


@dataclass(frozen=True)
class Source:
    sentry_id: str
    where: str  # file the sentry reads, for the reason text
    keys: Callable[[], set[str]]
    note: str = ""
    # Live numeric value per key, for a list whose entries are counts (recorded as ``max``).
    maxes: Callable[[], dict[str, int]] | None = None


F29 = "tests/fixtures/f29"
SOURCES: tuple[Source, ...] = (
    Source(
        "f29-reason-baseline",
        f"{F29}/reason-baseline.json",
        json_keys(f"{F29}/reason-baseline.json", "identities", ("namespace", "code")),
        "frozen: test_reason_baseline_frozen.py digest stays authoritative",
    ),
    Source(
        "f29-producer-inventory",
        f"{F29}/producer-inventory.json",
        json_keys(f"{F29}/producer-inventory.json", "sites", ("file", "function", "kind", "expr")),
    ),
    Source(
        "f29-consumer-inventory",
        f"{F29}/producer-inventory.json",
        json_keys(f"{F29}/producer-inventory.json", "consumers", ("file", "function", "expr")),
    ),
    Source(
        "secret-material-census",
        "tests/sentry/secret_material_census.json",
        json_keys(
            "tests/sentry/secret_material_census.json", "entries", ("file", "function", "name")
        ),
    ),
    Source(
        "reason-unchecked-helpers",
        "tests/sentry/baselines/reason-unchecked-helpers.json",
        in_code("test_reason_registry.py", "UNCHECKED_HELPERS"),
    ),
    Source("module-size", "test_module_size.py", in_code("test_module_size.py", "ALLOWLIST")),
    Source(
        "package-fanout",
        "layout/test_package_fanout.py",
        in_code("layout/test_package_fanout.py", "FANOUT_ALLOWLIST"),
        maxes=in_code("layout/test_package_fanout.py", "FANOUT_ALLOWLIST").maxes,
    ),
    Source(
        "table-kind-truth-tests",
        "test_table_kind_classifier.py",
        in_code("test_table_kind_classifier.py", "TRUTH_TEST_BASELINE"),
        maxes=in_code("test_table_kind_classifier.py", "TRUTH_TEST_BASELINE").maxes,
    ),
    Source("import-layers", ".importlinter", import_layer_keys(".importlinter", "layers")),
    Source(
        "public-import-boundary",
        "test_public_import_boundary.py",
        in_code("test_public_import_boundary.py", "PUBLIC_BOUNDARY_ALLOWLIST"),
    ),
    Source(
        "exception-hierarchy",
        "test_exception_hierarchy.py",
        in_code("test_exception_hierarchy.py", "_ALLOWLIST"),
    ),
    Source(
        "reason-policy-single-source",
        "test_reason_policy_single_source.py",
        in_code("test_reason_policy_single_source.py", "EXEMPT"),
    ),
    Source(
        "file-access-entry-seams",
        "test_file_access_entry_points.py",
        in_code("test_file_access_entry_points.py", "INTERNAL_SEAMS"),
    ),
    Source(
        "post-scan-route-exempt",
        "test_post_scan_route_guard.py",
        in_code("test_post_scan_route_guard.py", "EXEMPT"),
    ),
    Source(
        "locator-waivers", "test_locator_hygiene.py", in_code("test_locator_hygiene.py", "WAIVERS")
    ),
    Source(
        "log-interpolation",
        "test_log_interpolation.py",
        in_code("test_log_interpolation.py", "ALLOWLIST"),
    ),
    Source(
        "eval-sites", "test_source_hygiene.py", in_code("test_source_hygiene.py", "_EVAL_ALLOWLIST")
    ),
    Source(
        "phase-containment",
        "test_phase_containment.py",
        in_code("test_phase_containment.py", "EXEMPT"),
    ),
    Source(
        "validate-no-mutation",
        "test_validate_no_mutation.py",
        in_code("test_validate_no_mutation.py", "ALLOWLIST"),
    ),
    Source(
        "physical-seam-connected",
        "test_physical_seam_disconnection.py",
        in_code("test_physical_seam_disconnection.py", "DELIBERATELY_CONNECTED_MODULES"),
    ),
    Source(
        "wave-b-string-target-exempt",
        "test_engine_string_targets.py",
        in_code("test_engine_string_targets.py", "EXEMPT", ("_REMOVED",)),
    ),
    Source(
        "wave-b-e0-test-files",
        "test_wave_b_no_registry_version_literal.py",
        in_code("test_wave_b_no_registry_version_literal.py", "E0_TEST_FILES"),
    ),
    Source(  # live keys: the registry rows marked private that are not on write_private yet
        "private-file-sinks",
        "tests/sentry/baselines/private-file-sinks.json",
        lambda: {
            "entry::" + "::".join(str(e[f]) for f in ("module", "function", "sink", "ordinal"))
            for e in tomllib.loads(
                (SENTRY / "file_access_registry.toml").read_text(encoding="utf-8")
            )["entry"]
            if e.get("disposition") == "private"
        },
    ),
    Source(  # keys of the ratchet-schema baseline itself
        "serial-resources",
        "tests/sentry/baselines/serial-resources.json",
        lambda: set(
            json.loads((BASELINES / "serial-resources.json").read_text(encoding="utf-8"))["entries"]
        ),
    ),
    Source(  # keys of the ratchet-schema baseline itself
        "exception-context",
        "tests/sentry/baselines/exception-context.json",
        lambda: set(
            json.loads((BASELINES / "exception-context.json").read_text(encoding="utf-8"))[
                "entries"
            ]
        ),
    ),
    Source(  # keys of the ratchet-schema baseline itself
        "validation-name-messages",
        "tests/sentry/baselines/validation-name-messages.json",
        lambda: set(
            json.loads((BASELINES / "validation-name-messages.json").read_text(encoding="utf-8"))[
                "entries"
            ]
        ),
    ),
    Source(  # keys of the ratchet-schema baseline itself
        "gcs-call-kwargs",
        "tests/sentry/baselines/gcs-call-kwargs.json",
        lambda: set(
            json.loads((BASELINES / "gcs-call-kwargs.json").read_text(encoding="utf-8"))["entries"]
        ),
    ),
    Source(  # keys of the ratchet-schema baseline itself
        "post-scan-blocking",
        "tests/sentry/baselines/post-scan-blocking.json",
        lambda: set(
            json.loads((BASELINES / "post-scan-blocking.json").read_text(encoding="utf-8"))[
                "entries"
            ]
        ),
    ),
    Source(  # keys of the ratchet-schema baseline itself
        "post-scan-pass-through",
        "tests/sentry/baselines/post-scan-pass-through.json",
        lambda: set(
            json.loads((BASELINES / "post-scan-pass-through.json").read_text(encoding="utf-8"))[
                "entries"
            ]
        ),
    ),
    Source(  # keys of the ratchet-schema baseline itself
        "post-scan-key-core",
        "tests/sentry/baselines/post-scan-key-core.json",
        lambda: set(
            json.loads((BASELINES / "post-scan-key-core.json").read_text(encoding="utf-8"))[
                "entries"
            ]
        ),
    ),
    Source(  # keys of the ratchet-schema baseline itself
        "post-scan-reason-identity",
        "tests/sentry/baselines/post-scan-reason-identity.json",
        lambda: set(
            json.loads((BASELINES / "post-scan-reason-identity.json").read_text(encoding="utf-8"))[
                "entries"
            ]
        ),
    ),
    Source(  # keys of the ratchet-schema baseline itself
        "post-scan-gate-applies",
        "tests/sentry/baselines/post-scan-gate-applies.json",
        lambda: set(
            json.loads((BASELINES / "post-scan-gate-applies.json").read_text(encoding="utf-8"))[
                "entries"
            ]
        ),
    ),
    Source(  # keys of the ratchet-schema baseline itself
        "post-scan-commit",
        "tests/sentry/baselines/post-scan-commit.json",
        lambda: set(
            json.loads((BASELINES / "post-scan-commit.json").read_text(encoding="utf-8"))["entries"]
        ),
    ),
    Source(  # keys of the ratchet-schema baseline itself
        "post-scan-model-predicate",
        "tests/sentry/baselines/post-scan-model-predicate.json",
        lambda: set(
            json.loads((BASELINES / "post-scan-model-predicate.json").read_text(encoding="utf-8"))[
                "entries"
            ]
        ),
    ),
    Source(  # keys of the ratchet-schema baseline itself
        "post-scan-readability",
        "tests/sentry/baselines/post-scan-readability.json",
        lambda: set(
            json.loads((BASELINES / "post-scan-readability.json").read_text(encoding="utf-8"))[
                "entries"
            ]
        ),
    ),
    Source(
        "post-scan-route-coverage",
        "tests/sentry/baselines/post-scan-route-coverage.json",
        lambda: set(
            json.loads((BASELINES / "post-scan-route-coverage.json").read_text(encoding="utf-8"))[
                "entries"
            ]
        ),
    ),
)

# Committed data a sentry reads that is a pinned contract, not an allowlist that could grow.
# Each is held by its own digest or golden test.
NOT_A_LIST: dict[str, str] = {
    "route-format-matrix-v1.json": "pinned F06 route matrix, compared whole by test_f06_route_matrix",
    "capability-manifest-v1.json": "pinned F06 capability catalog, compared whole",
    "legacy-compat-baseline.json": "pinned F06 legacy config fixture, compared whole",
    "proof-manifest.json": "generated proof page data, regenerated and compared by test_proof_manifest",
    "reason-manifest-v1.json": "released reason manifest, frozen by the F29 registry tests",
    "reason-manifest-v1.4-f06.json": "released reason manifest, frozen by the F29 registry tests",
    "reason-manifest-v1.6-sub-stop.json": "released reason manifest, frozen by the F29 registry tests",
    "reason-manifest-v1.7-lib-l0-1.json": "released reason manifest, frozen by the F29 registry tests",
    "reason-manifest-v1.19-config-explicit-e2a.json": (
        "released reason manifest, frozen by the F29 registry tests"
    ),
    "reason-manifest-v1.15-config-explicit-e1.json": (
        "released reason manifest, frozen by the F29 registry tests"
    ),
    "reason-manifest-v1.8-scanml-m1.json": "released reason manifest, frozen by the F29 registry tests",
    "reason-registry-release.json": "released reason registry, frozen by the F29 registry tests",
    "consumer-contract.json": "F29 consumer contract, compared whole by test_reason_registry",
    "openapi.json": "generated schema used as a negative fixture in a residual-surface test",
    "file_access_registry.toml": "file access registry, allowed to grow; guarded by its Count header",
    "file_access_modules.toml": "file access module list, allowed to grow with new access modules",
}


def baseline_path(sentry_id: str) -> Path:
    return BASELINES / f"{sentry_id}.json"


def write_initial() -> None:
    """One-off: write each baseline from its live source. Refuses to overwrite (no add mode)."""
    BASELINES.mkdir(exist_ok=True)
    rows = []
    for src in SOURCES:
        path = baseline_path(src.sentry_id)
        if path.exists():
            raise SystemExit(f"{path} exists; baselines only shrink after the first inventory")
        reason = f"initial X0a inventory of {src.where}; existing entry, reviewed by its own sentry"
        entries: dict[str, dict[str, object]] = {k: {"reason": reason} for k in sorted(src.keys())}
        for k, value in (src.maxes() if src.maxes else {}).items():
            entries[k]["max"] = value
        doc = {"sentry_id": src.sentry_id, "created_in": CREATED_IN, "entries": entries}
        path.write_text(json.dumps(doc, indent=1, sort_keys=False) + "\n", encoding="utf-8")
        rows.append(
            f'[[sentry]]\nid = "{src.sentry_id}"\nbaseline = "tests/sentry/baselines/{path.name}"\n'
        )
    reg = ROOT / "scripts" / "dev" / "sentry_registry.toml"
    reg.write_text(
        reg.read_text(encoding="utf-8").rstrip("\n") + "\n\n" + "\n".join(rows), encoding="utf-8"
    )


if __name__ == "__main__":
    if sys.argv[1:] == ["--write-initial"]:
        write_initial()
    else:
        raise SystemExit("usage: _baseline_sources.py --write-initial")
