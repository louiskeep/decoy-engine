"""No strict-reachable engine diagnostic interpolates a raw file locator.

The D5/E1 guarantee is that under a strict policy a file locator (a path or a
``/proc/self/fd/<n>`` descriptor) never crosses in an error or a log line; only text the
principal was allowed to read may. X2, X3 and X4 fixed the known leak sites one by one, and
each time the same defect class resurfaced somewhere new. This sentry makes the class
non-recurring: it is a ratchet over the strict-reachable modules.

Mechanism. A module is strict-reachable when it names the file-access policy (imports
``fileaccess`` or uses ``FileAccess``/``FileHandle``/``DirHandle``/``OutputDir``/``OutputTxn``/
``FileRequest``/``file_access=``) OR imports a capability-reader helper that opens files through
the policy on a caller's behalf without the caller naming the policy itself (``_packio``;
``fileaccess._read`` importers already carry the ``fileaccess`` name). That second clause is load
bearing: pre-fix ``storm/model_pack/provenance.py`` read packs through ``_packio`` without the
word ``fileaccess``, so a name-only scope would have SKIPPED it, and the class could recur in a
reader module unseen. In those modules every ``raise`` and every logger call is scanned: when its
message interpolates a local whose name is a known path- or locator-bearing one (``path``,
``pack_dir``, ``manifest_path``, ``file_path``, ``target``, ``fname``, ``locator``, ... -- the
names the real leak sites used), the site must either render a display-derived value instead
(``display``/``shown``/``*_display``/``*_shown``, a ``.display`` attribute, or a
``fileaccess.shown``/``redact_locators`` call) or carry a waiver in ``WAIVERS`` with a reason. An
unwaived path-interpolating diagnostic fails the test. A path routed through an unrecognized
helper call (``_render_path(path)``, ``str(path).strip()``) stays visible: the scan recurses into
every non-sanitizing call's arguments and receiver, preferring a false positive (one waiver) to a
miss, since this is a security ratchet.

Known limits (documented in the E1 build record):
  - It is a name heuristic, like the log-interpolation sentry. A locator held in a variable
    that is NOT in ``PATH_NAMES`` is not seen; the set is kept current from the real leak sites.
    Generic identifiers (``name``, ``root``, ``src``) are left out on purpose (overloaded with
    table/column/distribution names); the one locator-as-lookup-key site (reference_tables'
    ``name``) is covered by its own AT-X2/AT-X4 test and the strict fixed-message rule.
  - A waived site, keyed by (module, function, variable) with a count, may still regress within
    its waiver, so each waiver carries a reason that justifies why its interpolation is safe
    (an ambient-only branch, a non-path config field, or a local named ``path`` that in fact
    holds ``handle.display``).
  - Scope is the modules that name the policy or import ``_packio``, not the full transitive call
    graph; this is the surface a strict policy reaches and keeps the sentry precise.
  - A basename (``.name``/``.stem``/``.suffix``) is treated as not a locator, matching the engine's
    existing disclosure (disguises and faker log ``path.name``) and the AT-X2 tests.
  - Compile-time plan metadata is out of the model: ``plan/_serialize.py`` echoes an embedded
    ``source_path`` in serialized plan JSON, but that is an in-memory plan object, not a live file
    operation under a policy, so it is outside the D5 live-locator class this sentry guards.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "decoy_engine"

# Local names that hold a whole filesystem path or locator by convention. This is the plan's
# enumerated list (path, pack_dir, manifest_path, file_path, directory, lock, target, locator,
# fname, pack_root) plus near-spellings. Generic identifiers (`name`, `root`, `src`) are left out
# on purpose: they name a table/column/distribution far more often than a file, so including them
# would need a dozen tautological waivers and make the sentry noisy rather than precise. A locator
# smuggled through such an identifier (reference_tables' `name` lookup key) is caught by that
# site's own AT-X2/AT-X4 test and the strict fixed-message rule, not by this name heuristic.
PATH_NAMES = frozenset(
    {
        "path",
        "paths",
        "filepath",
        "file_path",
        "pack_dir",
        "pack_root",
        "manifest_path",
        "directory",
        "dir_path",
        "lock",
        "lock_path",
        "lockfile",
        "target",
        "target_path",
        "locator",
        "fname",
        "out_path",
        "staging_dir",
        "output_dir",
        "temp_dir",
        "tmp_dir",
        "scratch_root",
    }
)

# Attributes that read only the final path component (a basename), not the location. The engine
# discloses these by design (disguises and faker log `path.name`), and the AT-X2 tests pass with a
# basename present, so the sentry treats them as not a locator.
_BASENAME_ATTRS = frozenset({"name", "stem", "suffix"})

_LEVELS = frozenset({"debug", "info", "warning", "warn", "error", "exception", "critical", "log"})
# Calls that return a display-safe value: the locator never survives them. `member_display` is the
# model-pack reader's own display helper (it wraps `fileaccess.shown`), so a reader may interpolate
# `_packio.member_display(pack_dir)` directly.
_SAFE_CALLS = frozenset(
    {"shown", "redact_locators", "encode_field_key", "position_key", "scrub", "member_display"}
)
# Keywords of a raised error whose value is a human message (never the structured position field
# `path=`/`code=` of a PlanCompileError, which carries a config position, not a locator).
_MESSAGE_KEYWORDS = frozenset({"message", "msg"})

# A module is strict-reachable when it names the policy OR imports a capability-reader helper
# that opens files through the policy on a caller's behalf without the caller naming the policy
# itself. The second group matters: pre-fix `provenance.py` read packs via `_packio` and would
# have been SKIPPED, so the ratchet must follow the helper, not only the word "fileaccess".
# `fileaccess._read` importers already carry the "fileaccess" marker, so `_packio` is the one that
# adds coverage today (its importers: trainer.py, provenance.py).
_POLICY_MARKERS = (
    "fileaccess",
    "FileAccess",
    "FileHandle",
    "DirHandle",
    "OutputDir",
    "OutputTxn",
    "FileRequest",
    "file_access",
    "_packio",
    "fileaccess._read",
)

# (module path relative to decoy_engine, qualified function, variable) -> (count, reason).
WAIVERS: dict[tuple[str, str, str], tuple[int, str]] = {
    ("keyprovider.py", "resolve_mask_secret_ref", "path"): (
        2,
        "ambient-only branch: under a strict policy the read returns via _strict_secret_from_file "
        "(X1), so these two raises only run on the ambient CLI, which may name the secret file",
    ),
    ("profile/_readers.py", "_build_file_source", "path"): (
        1,
        "reports a malformed non-string `path` config field (repr of a non-str), not a locator",
    ),
    ("profile/_fixed_width_reader.py", "_resolve_layout", "path"): (
        1,
        "the `path` parameter holds handle.display (read_fixed_width reassigns path = "
        "handle.display before the call); _render_path only escapes control chars, it does not "
        "reveal a location",
    ),
}


def _is_display(name: str) -> bool:
    low = name.lower()
    return (
        low in {"display", "shown"}
        or low.startswith(("display", "shown"))
        or low.endswith(("_display", "_shown"))
    )


def _root_name(value: ast.expr) -> str | None:
    """The variable an expression reads through attributes and subscripts. A `.display`/`.shown`
    attribute makes it display-derived, and a trailing `.name`/`.stem`/`.suffix` reads only the
    basename; both are safe, so both return None."""
    if isinstance(value, ast.Attribute) and value.attr in _BASENAME_ATTRS:
        return None
    while isinstance(value, (ast.Attribute, ast.Subscript)):
        if isinstance(value, ast.Attribute) and value.attr in {"display", "shown"}:
            return None
        value = value.value
    return value.id if isinstance(value, ast.Name) else None


def _exposed_paths(value: ast.expr) -> list[str]:
    """Every PATH_NAMES local the message exposes as text, directly or through formatting.
    Display-derived values and the sanitizing calls (`shown`, `redact_locators`, ...) expose
    nothing."""
    if isinstance(value, (ast.Name, ast.Attribute, ast.Subscript)):
        root = _root_name(value)
        if root is None:
            return []
        return [root] if root in PATH_NAMES and not _is_display(root) else []
    children: list[ast.expr] = []
    if isinstance(value, ast.JoinedStr):
        children = [p.value for p in value.values if isinstance(p, ast.FormattedValue)]
    elif isinstance(value, ast.BinOp):
        children = [value.left, value.right]
    elif isinstance(value, ast.IfExp):
        children = [value.body, value.orelse]
    elif isinstance(value, ast.BoolOp):
        children = list(value.values)
    elif isinstance(value, (ast.Tuple, ast.List, ast.Set)):
        children = list(value.elts)
    elif isinstance(value, ast.Call):
        func = value.func
        called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if called in _SAFE_CALLS:
            return []  # a display/redaction call replaces the locator
        # Any other call: recurse into its arguments and, for a method, its receiver. A path
        # laundered through an unrecognized helper (`_render_path(path)`, `str(path).strip()`)
        # stays visible. A security ratchet prefers a false positive (one waiver) to a miss.
        children = [*value.args, *(k.value for k in value.keywords)]
        if isinstance(func, ast.Attribute):
            children.append(func.value)
    return [hit for child in children for hit in _exposed_paths(child)]


def _message_args(node: ast.Call) -> list[ast.expr]:
    args = list(node.args)
    args += [k.value for k in node.keywords if k.arg in _MESSAGE_KEYWORDS]
    return args


def _is_logger_call(node: ast.Call) -> bool:
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr in _LEVELS):
        return False
    recv = func.value
    if isinstance(recv, ast.Call):  # logging.getLogger(__name__).info(...)
        inner = recv.func
        return isinstance(inner, ast.Attribute) and inner.attr == "getLogger"
    name = recv.attr if isinstance(recv, ast.Attribute) else getattr(recv, "id", "")
    return name == "logging" or name.lower().lstrip("_") in {"log", "logger", "audit"}


def _findings(source: str, module: str) -> dict[tuple[str, str, str], int]:
    tree = ast.parse(source)
    found: dict[tuple[str, str, str], int] = {}

    def record(call_args: list[ast.expr], qualname: str) -> None:
        for arg in call_args:
            for variable in _exposed_paths(arg):
                key = (module, qualname, variable)
                found[key] = found.get(key, 0) + 1

    def visit(node: ast.AST, qualname: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = qualname
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = child.name if qualname == "<module>" else f"{qualname}.{child.name}"
            if isinstance(child, ast.Raise) and isinstance(child.exc, ast.Call):
                record(_message_args(child.exc), name)
            elif isinstance(child, ast.Call) and _is_logger_call(child):
                record(_message_args(child), name)
            visit(child, name)

    visit(tree, "<module>")
    return found


def _strict_reachable(source: str) -> bool:
    return any(marker in source for marker in _POLICY_MARKERS)


def _engine_findings() -> dict[tuple[str, str, str], int]:
    found: dict[tuple[str, str, str], int] = {}
    for path in sorted(SRC.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if not _strict_reachable(source):
            continue
        found.update(_findings(source, path.relative_to(SRC).as_posix()))
    return found


def test_no_strict_reachable_diagnostic_interpolates_a_locator() -> None:
    found = _engine_findings()
    new = sorted(
        (key, count) for key, count in found.items() if count > WAIVERS.get(key, (0, ""))[0]
    )
    assert not new, (
        "a strict-reachable raise or log interpolates a file locator; name the config position "
        "via fileaccess.shown(...) / a *_display local, or add a justified WAIVERS entry (D5/X2): "
        f"{new}"
    )


def test_every_waiver_still_exists() -> None:
    found = _engine_findings()
    stale = sorted(
        (key, allowed) for key, (allowed, _reason) in WAIVERS.items() if found.get(key, 0) < allowed
    )
    assert not stale, f"waived sites were removed or reduced; lower the counts: {stale}"


# -- the detector itself: it flags each real leak-site shape and no display-safe form ----------

_M = "m.py"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        # The name-hint and model-pack leak shapes (before their fixes).
        (
            "def f():\n    raise E(f'{manifest_path} lists {fname!r} but {file_path} missing.')\n",
            {(_M, "f", "manifest_path"): 1, (_M, "f", "fname"): 1, (_M, "f", "file_path"): 1},
        ),
        (
            "def f():\n    raise ValueError(f'cannot read manifest in {pack_dir}: {exc}')\n",
            {(_M, "f", "pack_dir"): 1},
        ),
        # A logged locator is a leak too, including the getLogger(...) receiver form.
        ("def f():\n    _log.warning(f'could not read {path}')\n", {(_M, "f", "path"): 1}),
        (
            "def f():\n    logging.getLogger(__name__).info('at %s', target)\n",
            {(_M, "f", "target"): 1},
        ),
        # Other message forms.
        ("def f():\n    raise E('bad %s' % target)\n", {(_M, "f", "target"): 1}),
        ("def f():\n    raise E('{}'.format(file_path))\n", {(_M, "f", "file_path"): 1}),
        ("def f():\n    raise E('x ' + str(path))\n", {(_M, "f", "path"): 1}),
        ("def f():\n    raise E(str(path).strip())\n", {(_M, "f", "path"): 1}),
        # LOW-1: a path laundered through an unrecognized helper call is still seen.
        ("def f():\n    raise E(f'{_render_path(path)}')\n", {(_M, "f", "path"): 1}),
        ("def f():\n    raise E(f'{wrap(file_path)}')\n", {(_M, "f", "file_path"): 1}),
        # ... but a display passed to that same helper is not.
        ("def f():\n    raise E(f'{_render_path(handle.display)}')\n", {}),
        ("def f():\n    raise E(f'{_render_path(shown)}')\n", {}),
        (
            "def f():\n    raise PlanCompileError(path='a.b', message=f'bad {directory}')\n",
            {(_M, "f", "directory"): 1},
        ),
        # Counts and qualnames.
        (
            "class C:\n    def f(self):\n        raise E(f'{path}')\n        raise E(f'{path}')\n",
            {(_M, "C.f", "path"): 2},
        ),
        # Display-safe forms: not flagged.
        ("def f():\n    raise ValueError(f'cannot read in {shown}: {exc}')\n", {}),
        ("def f():\n    raise E(f'lists ({file_display}) missing.')\n", {}),
        ("def f():\n    raise E(f'at {handle.display}')\n", {}),
        ("def f():\n    raise E(f'at {fileaccess.shown(path, key)}')\n", {}),
        ("def f():\n    raise E(f'at {redact_locators(text, pairs)}')\n", {}),
        ("def f():\n    raise E(f'in {_packio.member_display(pack_dir)}')\n", {}),
        # A basename (`.name`/`.stem`/`.suffix`) is a component, not the location.
        ("def f():\n    raise E(f'failed on {path.name}')\n", {}),
        ("def f():\n    _log.warning('bad %s', file_path.stem)\n", {}),
        # The PlanCompileError position field is a config position, never a locator.
        ("def f():\n    raise PlanCompileError(path='joint_masks.reference', message='bad')\n", {}),
        # A generic identifier is out of scope on purpose (overloaded, mostly a config name).
        ("def f():\n    raise E(f'no table {name!r}')\n", {}),
        ("def f():\n    raise E(f'column {col!r}')\n", {}),
        ("def f():\n    _log.info('rows=%d', len(values))\n", {}),
        # A print is not a diagnostic sink.
        ("def f():\n    print(f'{path}')\n", {}),
    ],
)
def test_the_detector(source: str, expected: dict[tuple[str, str, str], int]) -> None:
    assert _findings(source, _M) == expected


def test_the_detector_flags_a_planted_fixture_module() -> None:
    """A fixture module reproducing the leak-site shapes is flagged at exactly those sites, and
    the display-safe companions beside them are not."""
    fixture = Path(__file__).resolve().parent / "fa_fixtures" / "locator_leak.py"
    found = _findings(fixture.read_text(encoding="utf-8"), "locator_leak.py")
    bad = {(fn, var) for (_mod, fn, var) in found}
    assert ("open_path_leak", "path") in bad
    assert ("name_hint_leak", "file_path") in bad
    assert ("model_pack_leak", "pack_dir") in bad
    assert ("trainer_pack_leak", "pack_dir") in bad  # the _packio-reader shape (MEDIUM-1)
    assert ("wrapped_path_leak", "path") in bad  # laundered through a helper call (LOW-1)
    # the display-safe and basename companions raise nothing the sentry sees
    assert not any(fn.endswith("_safe") for (fn, _var) in bad), bad


def test_scope_follows_the_capability_reader_helper() -> None:
    """A module that reads through the `_packio` capability helper is strict-reachable even when
    it never names the policy (the pre-fix provenance/trainer hole, MEDIUM-1)."""
    assert _strict_reachable("from decoy_engine.storm.model_pack import _packio\n")
    assert _strict_reachable("from decoy_engine import fileaccess\n")
    assert not _strict_reachable("import json\n\ndef f():\n    raise ValueError('x')\n")
