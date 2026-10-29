"""The verifier core must be reusable by a standalone artifact: no engine import, no data libraries."""

from __future__ import annotations

import ast
import subprocess
import sys
import textwrap
from pathlib import Path

PKG = Path(__file__).resolve().parents[3] / "src" / "decoy_engine" / "signed_evidence"
ALLOWED_THIRD_PARTY = {"joserfc", "rfc8785", "cryptography"}
CORE_MODULES = {
    "_errors",
    "_strict_json",
    "_canonical",
    "_jws",
    "_envelope",
    "_statement",
    "_trust",
    "_catalog",
    "_artifacts",
    "_checks",
    "_result",
    "_context",
    "_verifier",
    "_signer",
    "_time",
}


def _imports(path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                out.add(node.module.split(".")[0])
    return out


def test_core_modules_exist_and_import_only_allowed_packages() -> None:
    present = {p.stem for p in PKG.glob("*.py")}
    assert present >= CORE_MODULES, f"missing core modules: {sorted(CORE_MODULES - present)}"
    for path in PKG.glob("*.py"):
        bad = _imports(path) - set(sys.stdlib_module_names) - ALLOWED_THIRD_PARTY - {"__future__"}
        assert not bad, f"{path.name} imports outside the dependency-light allowlist: {sorted(bad)}"


def test_no_absolute_decoy_engine_imports_inside_the_core() -> None:
    for path in PKG.glob("*.py"):
        assert "decoy_engine" not in _imports(path), path.name


def test_loads_without_engine_init_or_data_libraries() -> None:
    code = textwrap.dedent(
        f"""
        import sys, types
        # Synthetic parent: the real decoy_engine/__init__ (which pulls pandas etc.) never runs.
        parent = types.ModuleType("decoy_engine")
        parent.__path__ = [{str(PKG.parent)!r}]
        sys.modules["decoy_engine"] = parent
        import decoy_engine.signed_evidence as se
        assert hasattr(se, "verify_signed_evidence")
        heavy = {{"pandas", "numpy", "pyarrow", "polars", "duckdb", "yaml", "pydantic", "sqlalchemy"}}
        loaded = heavy & set(sys.modules)
        assert not loaded, loaded
        print("ok")
        """
    )
    r = subprocess.run(  # noqa: S603 -- static argv (sys.executable + literals), no untrusted input
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60, check=False
    )
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stderr


def test_low_level_jws_verify_is_not_public() -> None:
    import decoy_engine.signed_evidence as se

    for name in dir(se):
        assert "verify_es256" not in name and name not in {"verify_signature", "jws_verify"}
    assert "verify_signed_evidence" in se.__all__
