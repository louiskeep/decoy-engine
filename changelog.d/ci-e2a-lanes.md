### Changed (CI runs every lane on pull requests and nightly, CI-E2a, 2026-11-12)

Profile lanes (packaging, ml, ner, dp-certified, determinism), the native companion build and the docs build now run on every pull request, and those plus testflight, the DP dependency matrix and the security audit run nightly on main. The determinism harness has its own `determinism` job and leaves the default test run. A sentry checks that every pytest marker and workflow has an automatic lane or a reasoned entry in `ci/unlaned.toml`. The unused substrate matrix workflow is deleted.
