"""A10 guard: the copula package draws randomness only from the two seed derivations.

An AST walk over every module of the package: no stdlib `random` import, no `numpy.random`
attribute other than `PCG64` and `SeedSequence`, and `PCG64` constructed exactly once in each of
`fit_gaussian_copula` and `sample_gaussian_copula` (the only frames that bind the seed), with the
same derivation expression apart from the purpose label.
"""

from __future__ import annotations

import ast
from pathlib import Path

import decoy_engine.generation.copula as copula

_PACKAGE = Path(copula.__file__).parent
_ALLOWED = {"PCG64", "SeedSequence"}
_CONSTRUCTORS = {("_fit.py", "fit_gaussian_copula"), ("_sample.py", "sample_gaussian_copula")}


def _is_np_random(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "random"
        and isinstance(node.value, ast.Name)
        and node.value.id in {"np", "numpy"}
    )


def _walk(source: str, filename: str) -> tuple[list[str], dict[tuple[str, str], list[ast.Call]]]:
    tree = ast.parse(source, filename)
    out: list[str] = []
    builds: dict[tuple[str, str], list[ast.Call]] = {}
    owner: dict[ast.AST, str] = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for inner in ast.walk(fn):
                owner.setdefault(inner, fn.name)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "random" or alias.name == "numpy.random":
                    out.append(f"{filename}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.split(".")[0] == "random" or module == "numpy.random":
                out.append(f"{filename}: from {module} import")
            if module == "numpy" and any(a.name == "random" for a in node.names):
                out.append(f"{filename}: from numpy import random")
        elif isinstance(node, ast.Attribute) and _is_np_random(node.value):
            if node.attr not in _ALLOWED:
                out.append(f"{filename}:{node.lineno} np.random.{node.attr}")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "PCG64" and _is_np_random(node.func.value):
                site = (filename, owner.get(node, "<module>"))
                if site not in _CONSTRUCTORS:
                    out.append(f"{filename}:{node.lineno} PCG64 outside the seed derivations")
                builds.setdefault(site, []).append(node)
    return out, builds


def _scan(root: Path) -> tuple[list[str], dict[tuple[str, str], list[ast.Call]]]:
    found: list[str] = []
    builds: dict[tuple[str, str], list[ast.Call]] = {}
    for path in sorted(root.glob("*.py")):
        hits, sites = _walk(path.read_text(encoding="utf-8"), path.name)
        found += hits
        for key, calls in sites.items():
            builds.setdefault(key, []).extend(calls)
    return found, builds


def _normalized(call: ast.Call) -> str:
    text = ast.dump(call, include_attributes=False)
    return text.replace("FIT_LABEL", "LABEL").replace("SAMPLE_LABEL", "LABEL")


def test_package_has_no_other_randomness() -> None:
    found, builds = _scan(_PACKAGE)
    assert found == []
    assert set(builds) == _CONSTRUCTORS
    assert all(len(calls) == 1 for calls in builds.values())
    fit, sample = (_normalized(builds[site][0]) for site in sorted(_CONSTRUCTORS))
    assert fit == sample
    assert "FIT_LABEL" in ast.dump(builds[("_fit.py", "fit_gaussian_copula")][0])
    assert "SAMPLE_LABEL" in ast.dump(builds[("_sample.py", "sample_gaussian_copula")][0])


def test_scanner_sees_seeded_violations(tmp_path: Path) -> None:
    seeded = {
        "a.py": "import numpy as np\nx = np.random.random(3)\n",
        "b.py": "import random\n",
        "c.py": "import numpy as np\ndef helper(s):\n    return np.random.PCG64(1)\n",
        "d.py": "from numpy import random\n",
        "e.py": "from numpy.random import default_rng\n",
        "f.py": "import numpy\nnumpy.random.default_rng()\n",
    }
    for name, source in seeded.items():
        (tmp_path / name).write_text(source, encoding="utf-8")
    (tmp_path / "_fit.py").write_text(
        "import numpy as np\ndef fit_gaussian_copula(seed):\n"
        "    return np.random.PCG64(np.random.SeedSequence(1))\n"
    )
    found, _ = _scan(tmp_path)
    assert {h.split(":")[0] for h in found} == set(seeded)
