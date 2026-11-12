### Changed (reason registry stamped at release, REG-RELEASE E2, 2026-11-12)

New reason codes are declared `since=UNRELEASED` and named in the PR's changelog fragment as the
code span `` `namespace/code` ``; a PR no longer bumps `REGISTRY_VERSION` or writes a minor
fixture. `decoy_engine.reasons` exports `UNRELEASED` and `pending_summary_edits()`. A development
manifest may report `"since": "unreleased"`; an older consumer still reads an unknown identity as
`decoy.unknown_reason_from_future`, and no released manifest carries the text. A released reason
changes only by a first deprecation (`deprecated_in=UNRELEASED` with `replaced_by`) or a summary
correction listed in `PENDING_SUMMARY_EDITS`.

`scripts/release/stamp_release.py` now stamps the registry in the same transaction as the
changelog compile: next minor (and its `reason-manifest-v<m>.<n>-release-<version>.json` fixture)
for new identities, next patch for pending edits alone, otherwise no change, and it rebuilds the
consumer contract and recaptures the release pin. `--registry-minor` may only restate the
automatic minor. `reason-registry-release.json` moves to format 2: one SHA-256 per manifest field
of every released identity, read strictly. `reason_release_pin.py --release-ready`, now run by
`release_checks.sh`, refuses a tag while anything is pending. `registry_renumber.py --check`
rejects a numeric registry bump or minor fixture, and `--to-unreleased` converts an in-flight
identity-addition branch.
