"""A10 guard: the copula package draws randomness only from `_rng.stream`.

An AST walk over every module of the package: no stdlib `random` import, no `numpy.random`
attribute other than `PCG64` and `SeedSequence`, and `PCG64` constructed only inside `_rng.py`.
"""

from __future__ import annotations

import ast
from pathlib import Path

import decoy_engine.generation.copula as copula

_PACKAGE = Path(copula.__file__).parent
_ALLOWED = {"PCG64", "SeedSequence"}


def violations(source: str, filename: str) -> list[str]:
    tree = ast.parse(source, filename)
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [
                f"{filename}: import {a.name}"
                for a in node.names
                if a.name.split(".")[0] == "random"
            ]
            out += [f"{filename}: import {a.name}" for a in node.names if a.name == "numpy.random"]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.split(".")[0] == "random" or module == "numpy.random":
                out.append(f"{filename}: from {module} import")
            if module == "numpy" and any(a.name == "random" for a in node.names):
                out.append(f"{filename}: from numpy import random")
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute):
            inner = node.value
            if (
                inner.attr == "random"
                and isinstance(inner.value, ast.Name)
                and inner.value.id in {"np", "numpy"}
            ):
                if node.attr not in _ALLOWED:
                    out.append(f"{filename}:{node.lineno} np.random.{node.attr}")
                elif node.attr == "PCG64" and filename != "_rng.py":
                    out.append(f"{filename}:{node.lineno} PCG64 outside _rng")
    return out


def _scan(root: Path) -> list[str]:
    found: list[str] = []
    for path in sorted(root.glob("*.py")):
        found += violations(path.read_text(encoding="utf-8"), path.name)
    return found


def test_package_has_no_other_randomness() -> None:
    modules = sorted(p.name for p in _PACKAGE.glob("*.py"))
    assert "_rng.py" in modules and "_sample.py" in modules
    assert _scan(_PACKAGE) == []


def test_scanner_sees_seeded_violations(tmp_path: Path) -> None:
    seeded = {
        "a.py": "import numpy as np\nx = np.random.random(3)\n",
        "b.py": "import random\n",
        "c.py": "import numpy as np\ng = np.random.PCG64(1)\n",
        "d.py": "from numpy import random\n",
        "e.py": "from numpy.random import default_rng\n",
        "f.py": "import numpy\nnumpy.random.default_rng()\n",
    }
    for name, source in seeded.items():
        (tmp_path / name).write_text(source, encoding="utf-8")
    (tmp_path / "_rng.py").write_text(
        "import numpy as np\ng = np.random.PCG64(np.random.SeedSequence(1))\n"
    )
    hits = _scan(tmp_path)
    assert {h.split(":")[0] for h in hits} == set(seeded)
