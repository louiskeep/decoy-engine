"""Planted bad sites for the locator-hygiene sentry (tests/sentry/test_locator_hygiene.py).

Deliberately re-creates the three real leak-site shapes that Codex found, so the sentry's
detector is proven to catch the defect class, next to display-safe companions it must leave
alone. This file is never imported or run; it is read as source and scanned. It lives under
tests/sentry/fa_fixtures, which pyproject excludes from ruff, because the leaks are the point.
"""

from __future__ import annotations


class E(Exception):
    pass


# Shape 1: a diagnostic naming a path local directly.
def open_path_leak(path):  # noqa
    raise FileNotFoundError(f"could not open {path}.")


def open_path_safe(path, shown):  # noqa
    raise FileNotFoundError(f"could not open {shown} (basename {path.name}).")


# Shape 2: storm/name_hints/loader.py "does not exist" built from target / fname.
def name_hint_leak(manifest_path, fname, file_path):  # noqa
    raise E(f"{manifest_path} lists {fname!r} but {file_path} does not exist.")


def name_hint_safe(file_display):  # noqa
    raise E(f"the manifest lists a file ({file_display}) that does not exist.")


# Shape 3: storm/model_pack/provenance.py handler naming pack_dir.
def model_pack_leak(pack_dir, exc):  # noqa
    raise ValueError(f"sign_pack: cannot read manifest in {pack_dir}: {exc}")


def model_pack_safe(shown, exc):  # noqa
    raise ValueError(f"sign_pack: cannot read manifest in {shown}: {exc}")
