"""Loader-specific tests for decoy_engine.storm.name_hints.loader.

The snapshot harness (tests/snapshots/test_name_hints_baseline.py)
covers the happy-path: shipped YAML files load + produce the same
hits matrix as the pre-extraction Python dict. These tests cover the
error paths so a malformed contribution fails CI with a clear message
rather than booting an engine that silently has the wrong patterns.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from decoy_engine.storm.name_hints.loader import (
    NameHintLoaderError,
    load_name_hint_terms,
)


def _write(dir_: Path, name: str, body: str) -> Path:
    p = dir_ / name
    p.write_text(body, encoding="utf-8")
    return p


# ── happy path ────────────────────────────────────────────────────────────


class TestHappyPath:
    def test_minimal_valid_library(self, tmp_path: Path) -> None:
        _write(tmp_path, "manifest.yaml", """
library_version: "0.1.0"
files:
  - one.yaml
""")
        _write(tmp_path, "one.yaml", """
detectors:
  - detector_id: foo
    description: Foo column.
    patterns:
      - foo
      - foo_bar
""")
        terms = load_name_hint_terms(tmp_path)
        assert terms == {"foo": ["foo", "foo_bar"]}

    def test_multiple_files_merge(self, tmp_path: Path) -> None:
        _write(tmp_path, "manifest.yaml", """
files:
  - first.yaml
  - second.yaml
""")
        _write(tmp_path, "first.yaml", """
detectors:
  - detector_id: a
    patterns: [a1, a2]
""")
        _write(tmp_path, "second.yaml", """
detectors:
  - detector_id: b
    patterns: [b1]
""")
        terms = load_name_hint_terms(tmp_path)
        assert terms == {"a": ["a1", "a2"], "b": ["b1"]}

    def test_empty_detector_file_is_no_op(self, tmp_path: Path) -> None:
        """A YAML file with no `detectors:` key is allowed -- it's a
        no-op contribution, useful for placeholder files before a
        domain pack lands."""
        _write(tmp_path, "manifest.yaml", """
files:
  - filled.yaml
  - empty.yaml
""")
        _write(tmp_path, "filled.yaml", """
detectors:
  - detector_id: a
    patterns: [a1]
""")
        _write(tmp_path, "empty.yaml", "# placeholder\n")
        terms = load_name_hint_terms(tmp_path)
        assert terms == {"a": ["a1"]}


# ── manifest errors ──────────────────────────────────────────────────────


class TestManifestErrors:
    def test_missing_manifest(self, tmp_path: Path) -> None:
        with pytest.raises(NameHintLoaderError, match="manifest not found"):
            load_name_hint_terms(tmp_path)

    def test_manifest_missing_files_key(self, tmp_path: Path) -> None:
        _write(tmp_path, "manifest.yaml", "library_version: 1.0.0\n")
        with pytest.raises(NameHintLoaderError, match="non-empty `files:` list"):
            load_name_hint_terms(tmp_path)

    def test_manifest_empty_files_list(self, tmp_path: Path) -> None:
        _write(tmp_path, "manifest.yaml", "files: []\n")
        with pytest.raises(NameHintLoaderError, match="non-empty `files:` list"):
            load_name_hint_terms(tmp_path)

    def test_manifest_files_entry_not_string(self, tmp_path: Path) -> None:
        _write(tmp_path, "manifest.yaml", "files:\n  - 42\n")
        with pytest.raises(NameHintLoaderError, match="entry must be a string"):
            load_name_hint_terms(tmp_path)

    def test_listed_file_missing(self, tmp_path: Path) -> None:
        _write(tmp_path, "manifest.yaml", "files:\n  - ghost.yaml\n")
        with pytest.raises(NameHintLoaderError, match="ghost.yaml.*does not exist"):
            load_name_hint_terms(tmp_path)

    def test_manifest_yaml_parse_error(self, tmp_path: Path) -> None:
        _write(tmp_path, "manifest.yaml", "files:\n  - one.yaml\n  - : invalid\n")
        with pytest.raises(NameHintLoaderError, match="could not parse"):
            load_name_hint_terms(tmp_path)


# ── per-file errors ──────────────────────────────────────────────────────


class TestPerFileErrors:
    def _wrap_manifest(self, tmp_path: Path) -> None:
        _write(tmp_path, "manifest.yaml", "files:\n  - bad.yaml\n")

    def test_detectors_not_a_list(self, tmp_path: Path) -> None:
        self._wrap_manifest(tmp_path)
        _write(tmp_path, "bad.yaml", "detectors: not-a-list\n")
        with pytest.raises(NameHintLoaderError, match="`detectors:` must be a list"):
            load_name_hint_terms(tmp_path)

    def test_entry_not_a_mapping(self, tmp_path: Path) -> None:
        self._wrap_manifest(tmp_path)
        _write(tmp_path, "bad.yaml", "detectors:\n  - just_a_string\n")
        with pytest.raises(NameHintLoaderError, match="entry #0 must be a mapping"):
            load_name_hint_terms(tmp_path)

    def test_missing_detector_id(self, tmp_path: Path) -> None:
        self._wrap_manifest(tmp_path)
        _write(tmp_path, "bad.yaml", "detectors:\n  - patterns: [x]\n")
        with pytest.raises(NameHintLoaderError, match="missing a non-empty `detector_id:`"):
            load_name_hint_terms(tmp_path)

    def test_empty_detector_id_string(self, tmp_path: Path) -> None:
        self._wrap_manifest(tmp_path)
        _write(tmp_path, "bad.yaml", "detectors:\n  - detector_id: \"\"\n    patterns: [x]\n")
        with pytest.raises(NameHintLoaderError, match="missing a non-empty `detector_id:`"):
            load_name_hint_terms(tmp_path)

    def test_missing_patterns(self, tmp_path: Path) -> None:
        self._wrap_manifest(tmp_path)
        _write(tmp_path, "bad.yaml", "detectors:\n  - detector_id: foo\n")
        with pytest.raises(NameHintLoaderError, match="non-empty `patterns:` list"):
            load_name_hint_terms(tmp_path)

    def test_empty_patterns_list(self, tmp_path: Path) -> None:
        self._wrap_manifest(tmp_path)
        _write(tmp_path, "bad.yaml", "detectors:\n  - detector_id: foo\n    patterns: []\n")
        with pytest.raises(NameHintLoaderError, match="non-empty `patterns:` list"):
            load_name_hint_terms(tmp_path)

    def test_pattern_term_not_string(self, tmp_path: Path) -> None:
        self._wrap_manifest(tmp_path)
        _write(tmp_path, "bad.yaml", "detectors:\n  - detector_id: foo\n    patterns: [valid, 42]\n")
        with pytest.raises(NameHintLoaderError, match="pattern #1 must be a non-empty string"):
            load_name_hint_terms(tmp_path)

    def test_empty_pattern_string(self, tmp_path: Path) -> None:
        self._wrap_manifest(tmp_path)
        _write(tmp_path, "bad.yaml", 'detectors:\n  - detector_id: foo\n    patterns: ["valid", ""]\n')
        with pytest.raises(NameHintLoaderError, match="pattern #1 must be a non-empty string"):
            load_name_hint_terms(tmp_path)


# ── duplicate detector_id ────────────────────────────────────────────────


class TestDuplicateDetectorId:
    def test_duplicate_within_one_file(self, tmp_path: Path) -> None:
        _write(tmp_path, "manifest.yaml", "files:\n  - one.yaml\n")
        _write(tmp_path, "one.yaml", """
detectors:
  - detector_id: dup
    patterns: [a]
  - detector_id: dup
    patterns: [b]
""")
        with pytest.raises(NameHintLoaderError, match="dup.*twice"):
            load_name_hint_terms(tmp_path)

    def test_duplicate_across_files(self, tmp_path: Path) -> None:
        _write(tmp_path, "manifest.yaml", "files:\n  - one.yaml\n  - two.yaml\n")
        _write(tmp_path, "one.yaml", "detectors:\n  - detector_id: dup\n    patterns: [a]\n")
        _write(tmp_path, "two.yaml", "detectors:\n  - detector_id: dup\n    patterns: [b]\n")
        with pytest.raises(NameHintLoaderError, match="dup.*declared in both"):
            load_name_hint_terms(tmp_path)


# ── shipped library still loads ──────────────────────────────────────────


class TestShippedLibrary:
    def test_v1_loads_with_all_25_detectors(self) -> None:
        """Default call returns the actual shipped v1/ contents.

        Pins the count so a future YAML edit that accidentally drops
        a detector breaks CI immediately. If you add/remove a detector
        in v1/ (a real intent, not an accident), update this number.
        """
        terms = load_name_hint_terms()
        assert len(terms) == 25, (
            f"shipped v1/ library has {len(terms)} detectors, expected 25. "
            "If a detector was intentionally added or removed, update this assertion."
        )

    def test_v1_returns_strings_for_every_pattern(self) -> None:
        terms = load_name_hint_terms()
        for det_id, patterns in terms.items():
            assert isinstance(det_id, str) and det_id
            assert isinstance(patterns, list) and patterns
            for t in patterns:
                assert isinstance(t, str) and t
