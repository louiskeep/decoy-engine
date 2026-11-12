### Added (engine import-layer contract, LAYOUT E3, 2026-11-11)

`.importlinter` declares the engine's layer tiers, and `tests/sentry/layout/test_import_layers.py`
holds the code to them. Every exemption names one exact leaf-to-leaf import, so an upward import
through a package facade fails. The check runs in its own `.venv-layers` through
`scripts/dev/gates.sh --steps layers` and a `layers-gate` CI job, and `import-linter` lives in a
separate `layers` extra, so the certified profile is unchanged. `decoy_engine.__version__` now
comes from `release.py` and is re-exported by the package root, with the same value.
