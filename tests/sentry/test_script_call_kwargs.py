"""Script call-site sentry (CI-E1): scripts pass only keywords `run_pipeline` still accepts.

Commit d011deba deleted the `substrate` keyword of `run_pipeline` and updated src/ and tests/
but not scripts/. The only tests that ran those workers were companion-guarded and dispatch-only,
so nothing noticed until a hosted run failed with a TypeError. This reads the live signature.
Named limit: a call through `**kwargs` is not checked.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from decoy_engine.execution._pipeline import run_pipeline

ROOT = Path(__file__).resolve().parents[2]
SCAN_DIRS = ("scripts", "testflight")


def _valid_keywords() -> set[str]:
    params = inspect.signature(run_pipeline).parameters.values()
    assert not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params), (
        "run_pipeline gained **kwargs: this sentry can no longer name valid keywords; revisit it"
    )
    return {p.name for p in params}


def _callee_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def run_pipeline_calls(source: str) -> list[ast.Call]:
    return [
        n
        for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.Call) and _callee_name(n.func) == "run_pipeline"
    ]


def bad_keywords(source: str, valid: set[str]) -> list[tuple[int, str]]:
    return [
        (kw.lineno, kw.arg)
        for call in run_pipeline_calls(source)
        for kw in call.keywords
        if kw.arg is not None and kw.arg not in valid
    ]


def test_scripts_call_run_pipeline_with_live_keywords() -> None:
    valid = _valid_keywords()
    findings: list[str] = []
    scripts_sites = 0
    for directory in SCAN_DIRS:
        for path in sorted((ROOT / directory).rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            if directory == "scripts":
                scripts_sites += len(run_pipeline_calls(source))
            findings += [
                f"{path.relative_to(ROOT)}:{line} passes {kw}="
                for line, kw in bad_keywords(source, valid)
            ]
    assert scripts_sites >= 4, (
        f"only {scripts_sites} run_pipeline call sites scanned under scripts/"
    )
    assert not findings, "run_pipeline no longer accepts:\n  " + "\n  ".join(findings)


def test_seeded_cases() -> None:
    valid = _valid_keywords()
    assert bad_keywords('run_pipeline(cfg, substrate="pandas")', valid) == [(1, "substrate")]
    assert bad_keywords('mod.run_pipeline(cfg, substrate="pandas")', valid) == [(1, "substrate")]
    assert bad_keywords('run_pipeline(cfg, engine_version="x")', valid) == []
    assert bad_keywords("run_pipeline(cfg, **kw)", valid) == []
