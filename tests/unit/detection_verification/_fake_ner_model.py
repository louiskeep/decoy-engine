"""A throwaway spaCy-model-shaped distribution for tests: package dir, meta.json, dist-info."""

from __future__ import annotations

import json
from pathlib import Path


def make_fake_model(
    root: Path,
    name: str,
    spacy_range: str = ">=3.0.0,<5.0.0",
    version: str = "1.2.3",
    *,
    with_meta: bool = True,
    init_code: str = "",
) -> str:
    """Write package `name` under `root` (put `root` on sys.path to find it) and return `name`."""
    package = root / name
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(init_code)
    record = [f"{name}/__init__.py,,"]
    if with_meta:
        meta = {"lang": "xx", "name": "fake", "version": version, "spacy_version": spacy_range}
        (package / "meta.json").write_text(json.dumps(meta))
        record.append(f"{name}/meta.json,,")
    dist = root / f"{name}-{version}.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
    (dist / "RECORD").write_text("\n".join(record) + "\n")
    return name
