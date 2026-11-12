# X0a engine registry record

Status: record

Slice X0a (engine half, part 1b): registering the existing baselines with the ratchet from part
1a (`2026-11-04-x0a-engine-build.md`). Each sentry id is newly added to the registry in this diff,
so its current contents are admitted as the first inventory; no env var or flag is involved.

## What changed

- `scripts/dev/sentry_registry.toml`: sentry id to baseline path. 18 sentries registered, their
  current contents being the initial inventory (`created_in` X0a): the F29 reason baseline,
  producer and consumer inventories, secret-material census, both file-access TOML lists, and
  the in-code allowlists of module-size, public-import-boundary, exception-hierarchy,
  file-access entry seams, locator waivers, log interpolation, eval sites, phase containment,
  validate-no-mutation (empty), the physical-seam connected modules, the Wave B string-target
  exemptions and the Wave B E0 test-file list. The F29 baseline is registered, but
  `test_reason_baseline_frozen.py` (sha256 digest) stays the authoritative, stricter freeze.
  Baselines for in-code lists are mirrors: `test_baseline_registry.py` fails when the live list
  and its baseline differ, so adding an allowlist entry needs a baseline key the ratchet refuses.
  Sources and the generator are in `tests/sentry/_baseline_sources.py`.
- `tests/sentry/test_baseline_registry.py`: also fails when a sentry test reads an unregistered
  allowlist-shaped constant or committed json/toml list (seeded cases for both are red-checked).
  Pinned contract files (F06 matrices, released F29 manifests, proof manifest) are listed with a
  reason in `NOT_A_LIST`.
- `tests/sentry/test_sentry_manifest.py`: adds `test_baseline_registry.py` to `SENTRIES`.

## Clean-environment gates run

Venv bootstrapped from uv.lock, `scripts/dev/gates.sh --keep-going --baseline` on this part's
head, no env var.

| step | branch | baseline |
| --- | --- | --- |
| env | PASS | SKIP |
| ruff, format, mypy | PASS | PASS |
| sentry | PASS (4003) | PASS (3956) |
| licenses | PASS (63) | PASS (63) |
| tests | PASS (44) | SKIP (none present) |

Failing ids: branch 0, baseline 0, branch-only 0.
