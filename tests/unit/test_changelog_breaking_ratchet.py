"""CHANGELOG ratchet: a consumer-visible break is in its release's Breaking list.

Two named limits:

1. In a release that has a "### Breaking for consumers" list, every entry outside that list
   that says something now fails, is refused or rejected, or raises must be triaged in
   `TRIAGED` by its opening words, with the reason it is not a separate break (normally: the
   Breaking bullet that already covers it). An untriaged entry fails, and so does a stale key.
2. A release section, once a newer release sits above it, is frozen at the digest in
   `tests/fixtures/changelog_released_sections.json`. A post-release change filed under an old
   heading (0.18.0 shipped three breaks filed under `[0.9.0]` this way) changes the digest; a
   genuine correction to an old entry updates the pin in the same diff, so it is reviewed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from scripts.release.stamp_release import release_sections as _releases
from scripts.release.stamp_release import section_digest as _digest

ROOT = Path(__file__).resolve().parents[2]
CHANGELOG = ROOT / "CHANGELOG.md"
PINS = ROOT / "tests" / "fixtures" / "changelog_released_sections.json"

_SUBSECTION = re.compile(r"^### (.+)$", re.M)
_BREAKING = "Breaking for consumers"
_BREAK_WORDS = re.compile(r"now fails?|\brefus(?:e|es|ed)\b|\braises?\b|\brejected\b", re.I)
_KEY_CHARS = 60

# (release, the entry's first 60 normalized characters) -> why it is not a separate break.
TRIAGED: dict[tuple[str, str], str] = {
    ("0.20.0", "A `reference` column with `distribution: fitted` now has its"): (
        "new feature: the refusals apply only to a fitted reference column, new in this release"
    ),
    ("0.20.0", "`decoy_engine.detect_usable_cpus(*, fs_root=None, affinity=N"): (
        "new function; it raises only for new callers"
    ),
    ("0.20.0", '`run_pipeline(execution_mode="stream")` means "must stream".'): (
        "new opt-in mode: only `stream` refuses; `auto` routing is unchanged"
    ),
    ("0.20.0", "A `type: statistical` column whose snapshot carries a `cell_"): (
        "new snapshot block: a snapshot without it generates byte-identical output"
    ),
    ("0.20.0", "Registry 1.17.0 adds four identities for GEN-UNIQUE: `config"): (
        "new reason identities; nothing raises them in this entry"
    ),
    ("0.20.0", "Registry 1.16.0 adds one `plan` identity, `statistical_cell_"): (
        "new reason identity; nothing raises it in this entry"
    ),
    ("0.20.0", "A generate column now accepts only the option keys its `type"): (
        "Breaking: unknown or malformed generate options are refused"
    ),
    ("0.20.0", "Pre-GA hard change: a config that carried an unknown or malf"): (
        "Breaking: unknown or malformed generate options are refused"
    ),
    ("0.20.0", "A pipeline with `global_settings.dp` now refuses a `type: re"): (
        "Breaking: DP refuses a fitted reference column"
    ),
    ("0.20.0", "`PipelineConfig` accepts an optional top-level `name:`. It i"): (
        "new optional key: a config without `name:` dumps and hashes as before"
    ),
    ("0.20.0", "`run_pipeline(..., quarantine_path=None)` takes the path the"): (
        "new keyword with a default; only a bad value is refused"
    ),
    ("0.20.0", "`decoy_engine.profile.profile_relationships_from_config(rela"): (
        "new function; its refusals reach only new callers"
    ),
    ("0.20.0", "A generate run that keeps its output in memory is now refuse"): (
        "Breaking: an in-memory generate run over capacity is refused"
    ),
    ("0.20.0", "Generation reads that marker. A `type: statistical` column t"): (
        "only snapshots fit with the new redaction marker; an older one loads as before"
    ),
    ("0.20.0", "`decoy_engine.quality.check_dp_fit_request(column_schema, *,"): (
        "new function; it raises only for new callers"
    ),
    ("0.20.0", "The out-of-core spill root and its subdirectories, the isola"): (
        "hardening: a caller-named scratch dir that holds files is refused (scratch_unsafe)"
    ),
    ("0.20.0", "`scan_sources(sources, *, tables=None, sample_rows=10_000, s"): (
        "new function; its refusals reach only new callers"
    ),
    ("0.20.0", "- `decoy_engine.parse_pipeline_config(raw)` is the one route"): (
        "error type and text only; the same configs are refused"
    ),
    ("0.20.0", "- Validator text is shown only for the new `ConfigRuleError`"): (
        "error text only; the same configs are refused"
    ),
    ("0.20.0", "- A per-row `derived` failure no longer echoes the cell valu"): (
        "error text only; the same rows fail"
    ),
    ("0.20.0", "- A source table with duplicate field names (caller Arrow ta"): (
        "Breaking: duplicate source field names are refused"
    ),
    ("0.20.0", "- `run_discovery_sql`: `DiscoverySqlError` no longer include"): (
        "error text and class only; the same queries fail"
    ),
    ("0.20.0", "`pre_scan.detectors` and `post_scan.detectors` choose betwee"): (
        "new optional config keys; a config without them dumps and hashes as before"
    ),
    ("0.20.0", "Behavior change: the local protected quarantine arena goes t"): (
        "Breaking: the quarantine arena goes through the allocator"
    ),
    ("0.19.0", "A column can now declare `logical_type`, `parse_format`, `se"): (
        "new opt-in feature: a route refuses only a job that declares casting, "
        "a config that does not is unchanged (PLAN_VERSION is in the Breaking list)"
    ),
    ("0.19.0", "`decoy_engine.read_csv_source(path, dialect, *, table, dtype"): (
        "Breaking: a column_names that is not a list or tuple of strings is refused"
    ),
    ("0.18.0", "- Output: versus 3.10 output, at least one threshold moves i"): (
        "Breaking: weighted categorical masking moves masked output"
    ),
    ("0.18.0", "- Weights must be `int`, a numpy integer, `float` or numpy `"): (
        "Breaking: weighted categorical masking (refused weight types)"
    ),
    ("0.18.0", "- Error text: a refused FK key, and a failed FK chunk cast, "): (
        "error text only; the refusal itself is the Breaking FK key bullet"
    ),
    ("0.18.0", "- A `file:` mask secret is read through the installed file-a"): (
        "Breaking: a `file:` mask secret must be owner-only"
    ),
    ("0.18.0", "- Under an installed policy, an explicit `file_access=` (and"): (
        "Breaking: the 42 file-access entry points bullet"
    ),
    ("0.18.0", "- `secret_providers.parse_ref` refuses with no cause or cont"): (
        "exception chaining only; the same inputs are refused as in 0.17.0"
    ),
    ("0.18.0", '- `build_profile_source` names "the value at <position>" ins'): (
        "error text only; the same inputs are refused as in 0.17.0"
    ),
    ("0.18.0", "`write_fixed_width`, `estimate_job`, `compile_pinned_plan`, "): (
        "refusals apply only under an installed file-access policy, new in 0.18.0; "
        "Breaking: the 42 file-access entry points bullet"
    ),
    ("0.18.0", "With no policy argument the engine behaves as before: paths,"): (
        "states that nothing changes without a policy"
    ),
    ("0.18.0", "- A bare `joint_mask.reference` name must be one of the ship"): (
        "Breaking: a bare `joint_mask.reference` name must be a shipped table"
    ),
    ("0.18.0", "Smaller changes under the same program: the three unused hel"): (
        "internal/ helpers are not public; write_jsonl_staged is a Breaking bullet; the "
        "FileAccessUnavailable refusals apply only under a policy, new in 0.18.0"
    ),
    ("0.18.0", "- A shipped corpus name resolves only if it is a registry ke"): (
        "Breaking: a shipped code-set corpus name must be a registered corpus"
    ),
    ("0.18.0", "- Under a policy other than the ambient one, no engine error"): (
        "error text under a policy only, new in 0.18.0"
    ),
    ("0.18.0", "- Under such a policy every handler that translates, stores "): (
        "exception class under a policy only, new in 0.18.0"
    ),
    ("0.17.0", "`pyproject.toml` `[project].version` is the single source of"): (
        "release tooling (cut-release.sh refusals), not a consumer surface"
    ),
    ("0.17.0", "The reason registry ships at `registry_version` 1.1.0 (891 i"): (
        "a repository test that fails, not a consumer surface"
    ),
    ("0.17.0", "`type` is added to the frozen `RESERVED_NAMES` of the `when`"): (
        "Breaking: `when:` grammar, `type` is a reserved name"
    ),
    ("0.17.0", "- Pipeline config `version: 2` is required (`version: 1` is "): (
        "Breaking: pipeline config `version: 2` is required"
    ),
}


def _entries_outside_breaking(section: str) -> list[str] | None:
    """Normalized entries (bullets and paragraphs) outside the Breaking list, or None when the
    release has no Breaking list."""
    subs = list(_SUBSECTION.finditer(section))
    if not any(s.group(1).startswith(_BREAKING) for s in subs):
        return None
    out: list[str] = []
    for i, sub in enumerate(subs):
        if sub.group(1).startswith(_BREAKING):
            continue
        body = section[sub.end() : subs[i + 1].start() if i + 1 < len(subs) else len(section)]
        for entry in re.split(r"\n(?=- )|\n\s*\n", body):
            normalized = " ".join(entry.split())
            if normalized:
                out.append(normalized)
    return out


def _untriaged_and_stale(text: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    seen: set[tuple[str, str]] = set()
    untriaged: list[tuple[str, str]] = []
    for version, section in _releases(text):
        entries = _entries_outside_breaking(section)
        for entry in entries or []:
            if not _BREAK_WORDS.search(entry):
                continue
            key = (version, entry[:_KEY_CHARS])
            seen.add(key)
            if key not in TRIAGED:
                untriaged.append(key)
    return untriaged, sorted(set(TRIAGED) - seen)


def test_every_break_phrase_outside_the_breaking_list_is_triaged() -> None:
    untriaged, _stale = _untriaged_and_stale(CHANGELOG.read_text(encoding="utf-8"))
    assert not untriaged, (
        "entries that say something now fails, is refused or raises sit outside their release's "
        "Breaking list: add the break to that list, then key the entry in TRIAGED with the reason "
        f"it is covered: {untriaged}"
    )


def test_no_triage_key_is_stale() -> None:
    _untriaged, stale = _untriaged_and_stale(CHANGELOG.read_text(encoding="utf-8"))
    assert not stale, f"TRIAGED keys that match no entry any more; remove them: {stale}"


def test_released_sections_match_their_pins() -> None:
    releases = _releases(CHANGELOG.read_text(encoding="utf-8"))
    actual = {version: _digest(section) for version, section in releases[1:]}
    pinned = json.loads(PINS.read_text(encoding="utf-8"))
    missing = sorted(set(actual) - set(pinned))
    changed = sorted(v for v in actual if v in pinned and pinned[v] != actual[v])
    gone = sorted(set(pinned) - set(actual))
    assert not (missing or changed or gone), (
        "a released CHANGELOG section changed or lost its pin. A change made after a release "
        "belongs in the newest section; a correction to an old entry updates "
        f"{PINS.relative_to(ROOT)} in the same diff. missing={missing} changed={changed} "
        f"gone={gone}"
    )


def test_the_ratchet_catches_a_break_filed_outside_the_list() -> None:
    text = CHANGELOG.read_text(encoding="utf-8")
    newest = _releases(text)[0][0]
    marker = f"## [{newest}]"
    head, tail = text.split(marker, 1)
    planted = "\n### Changed (planted)\n\n- A planted option now fails with `planted_code`.\n"
    first_sub = tail.index("\n### ")
    mutated = head + marker + tail[:first_sub] + planted + tail[first_sub:]
    untriaged, _ = _untriaged_and_stale(mutated)
    assert (newest, "- A planted option now fails with `planted_code`.") in untriaged


def test_the_pins_catch_an_entry_filed_under_an_old_release() -> None:
    text = CHANGELOG.read_text(encoding="utf-8")
    old_version, old_section = _releases(text)[-1]
    mutated = text.replace(old_section, old_section + "\n- filed late\n", 1)
    actual = {v: _digest(s) for v, s in _releases(mutated)[1:]}
    pinned = json.loads(PINS.read_text(encoding="utf-8"))
    assert actual[old_version] != pinned[old_version]
