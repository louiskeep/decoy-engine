"""Source-hygiene sentry (V2.0 Closeout Sprint 3).

Extra grep-style guards beyond what ruff catches, per the V2.0
foundation-closeout expected-tests list:

  - unsafe eval / exec / compile callsites
  - mojibake (utf-8 em-dash double-encoded as 'â€"')
  - old brand aliases (forge, Forge, FORGE -- legacy pre-Decoy names)
  - stale source paths in first-line comments (e.g. # decoy_engine/io/...)

Each check has a narrow ALLOWLIST tuned to V2.0 reality. Adding a new
entry without a removal-sprint comment is a review blocker by
convention.
"""

from __future__ import annotations

import re
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parents[2] / "src" / "decoy_engine"

# ── 1. eval / exec / compile ──────────────────────────────────────────


# Allow these to use eval / exec / compile because they are themselves
# the safe-eval implementation or test fixtures that probe it.
_EVAL_ALLOWLIST: dict[str, str] = {
    "expressions.py": (
        "Implements safe_eval() / compile() with AST sanitization for "
        "the expression DSL. The eval / compile callsites are the "
        "library; the safety check is in this module."
    ),
    "transforms/formula.py": (
        "Pre-V2 legacy strategy that delegates to safe_eval from "
        "expressions.py. V2.1 audit decides whether to keep or fold "
        "into the graph derive op."
    ),
}


# Negative lookbehind for `.` so `re.compile(...)`, `pattern.compile(...)`,
# `proc.exec(...)`, etc. are NOT matched. Only the bare builtin form
# counts (eval(...), exec(...), compile(...)).
_DANGEROUS_RE = re.compile(r"(?<![\w.])(eval|exec|compile)\s*\(")


def _imports_eval_exec_compile(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    # Cheap filter first.
    if not any(kw in text for kw in ("eval(", "exec(", "compile(")):
        return False
    # Skip docstring matches by stripping triple-quoted blocks first.
    no_docstrings = re.sub(r'"""[\s\S]*?"""', "", text)
    no_docstrings = re.sub(r"'''[\s\S]*?'''", "", no_docstrings)
    no_docstrings = re.sub(r"#[^\n]*", "", no_docstrings)
    return bool(_DANGEROUS_RE.search(no_docstrings))


def test_no_eval_exec_compile_outside_allowlist() -> None:
    """Source files must not call eval / exec / compile unless they
    are on the allowlist. The allowlist is the engine's safe-eval
    implementation surface; everything else uses it through the
    expressions module API."""
    offenders: list[str] = []
    for path in ENGINE_ROOT.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        if not _imports_eval_exec_compile(path):
            continue
        rel = path.relative_to(ENGINE_ROOT).as_posix()
        if rel in _EVAL_ALLOWLIST:
            continue
        offenders.append(rel)
    assert not offenders, (
        "Source files calling eval / exec / compile without an "
        "allowlist entry:\n  - " + "\n  - ".join(offenders) + "\n"
        "Fix: route through decoy_engine.expressions.safe_eval, or "
        "add an entry to _EVAL_ALLOWLIST in this file with a comment "
        "explaining why the direct call is necessary."
    )


# ── 2. Mojibake ───────────────────────────────────────────────────────


# Byte sequence that utf-8 em-dash (U+2014) produces when it gets read
# as Latin-1 then re-encoded as utf-8 -- the same garbled pattern that
# V2.0-prep cleanup found in 25 files (database.py docstring, etc.).
_MOJIBAKE_EM_DASH = b"\xc3\xa2\xe2\x82\xac\xe2\x80\x9d"


def test_no_mojibake_in_source() -> None:
    """No file in src/ contains the 'â€"' mojibake sequence. This is
    the same pattern V2.0-prep swept; the sentry stops it from
    re-creeping in via copy/paste from documents."""
    offenders: list[str] = []
    for path in ENGINE_ROOT.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        raw = path.read_bytes()
        if _MOJIBAKE_EM_DASH in raw:
            offenders.append(path.relative_to(ENGINE_ROOT).as_posix())
    assert not offenders, (
        "Mojibake (utf-8 em-dash double-encoded as 'â€\"') in:\n  - "
        + "\n  - ".join(offenders)
        + "\nReplace with a regular hyphen or en-dash."
    )


# ── 3. Old brand aliases ──────────────────────────────────────────────


# Identifiers and module paths the pre-Decoy 'forge'/'forge-engine'
# rebrand left behind. The taxonomy is locked at decoy / decoy-engine /
# decoy-platform / decoy-web (see [[decoy_rebrand]] memory). Catching
# stragglers here prevents accidental reintroduction.
_BRAND_RE = re.compile(
    r"\b(forge_engine|forge\-engine|ForgeEngine|FORGE_ENGINE|"
    r"ForgeError|forge_platform|forge\-platform|ForgePlatform|"
    r"forge_web|forge\-web|ForgeWeb)\b"
)


def test_no_legacy_brand_aliases() -> None:
    """Source files must not reference legacy 'forge' brand identifiers.
    The taxonomy is decoy / decoy-engine / decoy-platform / decoy-web.
    Adding a new occurrence is a review blocker; document any
    legitimate one inline with a noqa-style explanation and update
    this sentry to ignore the specific line."""
    offenders: list[str] = []
    for path in ENGINE_ROOT.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if _BRAND_RE.search(text):
            offenders.append(path.relative_to(ENGINE_ROOT).as_posix())
    assert not offenders, (
        "Legacy 'forge*' brand aliases in:\n  - "
        + "\n  - ".join(offenders)
        + "\nRename to the decoy / decoy_engine equivalent."
    )


# ── 4. Stale first-line path comments ──────────────────────────────────


# Pre-2026 path comments pointed at folders that were renamed during
# the V1 reorg (io -> connectors, strategies -> transforms,
# generator -> generators, core/utils -> internal). V2.0-prep cleared
# 25 stragglers; this sentry stops a new one from landing.
_STALE_PATH_PREFIXES = (
    "decoy_engine/io/",
    "decoy_engine/strategies/",
    "decoy_engine/generator/",
    "decoy_engine/core/",
    "decoy_engine/utils/",
)


def test_no_stale_path_comments() -> None:
    """No file's first-line comment may reference a legacy module
    location. The rename map above lists every pre-V1 folder name that
    must not appear in path comments anymore."""
    offenders: list[str] = []
    for path in ENGINE_ROOT.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        first_line = text.splitlines()[0] if text else ""
        if not first_line.startswith("#"):
            continue
        for stale in _STALE_PATH_PREFIXES:
            if stale in first_line:
                offenders.append(f"{path.relative_to(ENGINE_ROOT).as_posix()}: {first_line!r}")
                break
    assert not offenders, (
        "Stale path comments referencing legacy module locations:\n"
        "  - " + "\n  - ".join(offenders) + "\n"
        "Either delete the comment (file paths are obvious from "
        "their location on disk) or update to the current path."
    )
