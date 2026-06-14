# Contributing to decoy-engine

Thanks for considering a contribution. The engine is pre-1.0; the public API and some internal contracts are still moving.

## Reporting bugs and requesting features

[GitHub Issues](https://github.com/louiskeep/decoy-engine/issues) is the right channel for both. A good bug report includes the engine version (`python -c "import decoy_engine; print(decoy_engine.__version__)"`), a minimal `PipelineConfig` or pandas DataFrame that reproduces the issue, and the full traceback.

For security issues, do not file a public issue: see [`SECURITY.md`](SECURITY.md).

## Local development

```
git clone https://github.com/louiskeep/decoy-engine
cd decoy-engine
python -m venv .venv
source .venv/bin/activate    # PowerShell: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

Tests:

```
pytest tests/unit/
pytest tests/integration/
```

The full suite is large; running only the modules you touched plus their nearest integration neighbors is the expected scope for most PRs.

## Pull requests

- One topic per PR. Smaller diffs land faster.
- Use `git commit -s` to sign off (Developer Certificate of Origin). The project is licensed Apache-2.0; contributions are accepted under the same license.
- Public-API changes (`decoy_engine.__init__.__all__`) and YAML-surface changes (new strategy, new transform op, renamed key) need a `CHANGELOG.md` entry under `[Unreleased]`.
- Capability changes (a new mask/generate strategy, synthetic provider, connector, STORM detector, or disguise) must regenerate the capability matrix: run `python scripts/gen_capability_matrix.py` and commit the updated `docs/capability-matrix.md`. The `tests/sentry/test_capability_matrix.py` guard fails CI if you forget, so a new capability cannot ship without its docs entry.
- If a change is more than one PR, file an Issue describing the plan first.

## Compatibility

The [compatibility contract](docs/compatibility-contract.md) defines the frozen surface (persisted artifacts, the vault, the determinism derivation, disguises, the public API and config). Read it before changing anything under those paths. Two CI gates enforce it on PRs:

- `scripts/check_compat_preflight.py` (Compat pre-flight workflow): a PR that touches a frozen-surface path must paste the contract's section-9 checklist into its description with every box ticked.
- `scripts/prove_regression.py` (Prove regression workflow, opt-in via the `bugfix` label): a `@pytest.mark.regression` test must fail against the pre-fix baseline, proving it catches the bug.

The cross-version compatibility corpus (`tests/integration/compat_corpus/`) locks artifacts written by an earlier engine version and asserts the current engine still reads them. Its behaviour keys off `decoy_engine.RELEASE_PHASE`.

## Code style

`ruff` for lint + format. Pre-commit hooks configured in `.pre-commit-config.yaml` run them automatically; install with `pre-commit install`.

## Where things live

See [`CODEMAP.md`](CODEMAP.md) for the package layout.
