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
    ("0.23.0", "`decoy_engine.connectors` exports `S3ObjectStore`, `ObjectSt"): (
        "new classes; the match is `rejected` as a put_if result name, not a refusal"
    ),
    ("0.23.0", "- `VaultWriter`, `load_vault` and the run-entry key-match gu"): (
        "Breaking: a vault opens and writes only under the 32-byte mask root"
    ),
    ("0.23.0", "- `vault_writer_for_config` never keys a vault on `job_seed`"): (
        "Breaking: a vault opens and writes only under the 32-byte mask root"
    ),
    ("0.23.0", "A bound iterator (`bind_iterator`) now runs every step, incl"): (
        "Breaking: a bound iterator runs in a private context copied when it was bound"
    ),
    ("0.23.0", "A generate column can say how its values are printed with `o"): (
        "new opt-in key: the refusals apply only to a column that sets `output_type`"
    ),
    ("0.23.0", "A relationship whose parent table is generated is refused wi"): (
        "Breaking: a generated relationship parent whose key is not provably unique is refused"
    ),
    ("0.23.0", "Four reasons in the `decoy` namespace, all pending release: "): (
        "new reason codes and a new delivery input check; no existing call changes"
    ),
    ("0.23.0", "On Linux and macOS, every directory the engine creates on an"): (
        "retry refusal for a directory left unopenable by a crash under a restrictive umask; no change for a normal run"
    ),
    ("0.23.0", "Plan only, no code change. `docs/plans/2026-10-30-f22-public"): (
        "plan only, no code change; the refusals described are the planned publication guarantee"
    ),
    ("0.23.0", "The `fpe` strategy reported two failures under codes the rea"): (
        "the same two failures now carry registered codes; which inputs fail is unchanged"
    ),
    ("0.23.0", "The declaration gate refuses a column the config declares fo"): (
        "Breaking: a column declared for a mask table but missing from the source is refused"
    ),
    ("0.23.0", "The entries that mask without running the post-scan (`run_se"): (
        "Breaking: the mask entries that run no post-scan refuse a model post-scan config"
    ),
    ("0.23.0", "The file is read once at compile time and its bytes are stor"): (
        "new `lookup` type: the refusal applies only to a plan that carries a lookup column"
    ),
    ("0.23.0", "The local record library's `evidence` payload (`cli-local-1`"): (
        "new optional key pair; the reader refuses only a half pair, which no earlier writer produced"
    ),
    ("0.23.0", "The post-scan report's `fk_preservation` findings now come f"): (
        "Breaking: one orphan the run introduced now fails at any table size"
    ),
    ("0.23.0", "The shared staging writer now raises `out_of_core_temp_disk_"): (
        "Breaking: a full scratch disk during staging raises a code in CAPACITY_EXCLUSIONS"
    ),
    ("0.23.0", "What a caller has to do: build a vault writer from an admitt"): (
        "Breaking: a vault opens and writes only under the 32-byte mask root"
    ),
    ("0.23.0", "`ConnectorError` takes a keyword `kind`, one of `access_deni"): (
        "Breaking: S3 and GCS connector errors carry fixed text and a `kind`"
    ),
    ("0.23.0", "`config.post_scan_gate_applies(config)` is the one decision "): (
        "new gate core; no route calls it yet, so no existing run is refused"
    ),
    ("0.23.0", "`declared_casting` gains the `boolean/text/v1` format (gener"): (
        "new opt-in cast format: a config that does not declare it is unchanged"
    ),
    ("0.23.0", "`decoy_engine.fileaccess` exports `ScratchArena`, an A-4 `St"): (
        "new class; no engine route uses it yet, so no existing run is refused"
    ),
    ("0.23.0", "`decoy_engine.run_result` exports `RunResultCollector`, a pr"): (
        "new class; the match is the sentence saying it never raises"
    ),
    ("0.23.0", "`decoy_engine.subset` gains `sample_request(config, *, rows,"): (
        "new function and optional fields; refusals reach only a caller that asks for a sample"
    ),
    ("0.23.0", "`decoy_engine.subset` gains `select_subset(request, inputs, "): (
        "new function; standalone subsetting refuses what it refused before"
    ),
    ("0.23.0", "`decoy_engine.verification.canary.scan_canary_artifacts` sca"): (
        "new function; its refusals reach only new callers"
    ),
    ("0.23.0", "`decoy_engine.verification.postmask` gains `post_scan_verdic"): (
        "new function; the loader check refuses only a batch wider than its declared type, a new input"
    ),
    ("0.23.0", "`derived` expressions accept `if(condition, value, other)`, "): (
        "new syntax; a missing column in an unchosen branch is refused as it was in other branches"
    ),
    ("0.23.0", "`generate_option_catalog()` lists `derived_aggregate`'s `op`"): (
        "catalog listing only; config parse is unchanged"
    ),
    ("0.23.0", "`plan_delivery` now plans every declared table whose target "): (
        "callers pass an outcome per deliverable table; the change removes a raise (s3 and gcs outcomes) rather than adding one"
    ),
    ("0.23.0", "`profile_source`, `run_pipeline` and `run_pipeline_isolated`"): (
        "Breaking: a source column or table the config does not declare is refused before any row is read"
    ),
    ("0.23.0", "`run_mask_pipeline_chunked`, `run_mask_chunked` and `run_nat"): (
        "Breaking: the three chunked entries refuse an undeclared table or column"
    ),
    ("0.23.0", "`run_pipeline` now applies a `subset:` block: it selects the"): (
        "new capability: a config with a `subset:` block used to be refused and now runs"
    ),
    ("0.23.0", "`runtime/out_of_core_reorder_disk_budget_too_small` is a new"): (
        "Breaking: the reorder disk case raises the new code, in CAPACITY_EXCLUSIONS"
    ),
    ("0.23.0", "`scikit-learn==1.7.2` and `lightgbm==4.6.0` moved from the `"): (
        "Breaking: the `ml` extra is removed; the match says a missing library does not raise"
    ),
    ("0.23.0", "`scripts/release/stamp_release.py` now stamps the registry i"): (
        "release tooling only; it refuses a tag, not a runtime call"
    ),
    ("0.23.0", "`type: lookup` draws each row's value from a list kept in a "): (
        "new `lookup` type: the refusals apply only to the new type"
    ),
    ("0.22.0", "`type: distribution` with `family: uniform` and `low`, `high"): (
        "new feature: the family and key refusals apply only to the new distribution type"
    ),
    ("0.22.0", "`run_config_only_checks` and `compile_plan` now call one che"): (
        "Breaking: a generate column that fails the per-type checks now fails at validate and run"
    ),
    ("0.22.0", "A `formula` that uses syntax the runtime evaluator cannot ru"): (
        "Breaking: a formula with an unknown name now fails at validate; the unevaluable-syntax "
        "case used to generate a column of nulls"
    ),
    ("0.21.0", "Generation now raises `warning.generation_condition_parent_u"): (
        "new warning record; generated bytes do not change"
    ),
    ("0.21.0", "The generation fidelity gate now hands a structured `Quality"): (
        "new warning channel and a stricter warn trigger; nothing is refused"
    ),
    ("0.21.0", "`decoy_engine.generation.generation_order(config)` returns e"): (
        "new function; output order only, per-table bytes unchanged, and the bad "
        "reference_table error keeps its ValueError type"
    ),
    ("0.21.0", "`contingency_top_k` on `compute_distribution_snapshot` and `"): (
        "Breaking: contingency_top_k outside 1..1000 or a bool is refused"
    ),
    ("0.21.0", "A cap above the default is refused in a plain snapshot (`inc"): (
        "Breaking: an over-default cap on a non-categorical joint is refused in a plain snapshot"
    ),
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
        "Breaking: a caller-named scratch dir that is unsafe is refused (scratch_unsafe)"
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
