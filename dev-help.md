# forge-engine — Developer Reference

## First-time setup

```bash
cd forge-engine
pip install -e .                 # installs forge-engine as editable package
pip install pytest               # test runner (included in dev deps)
```

## Daily development loop

```bash
# 1. Branch
git checkout -b feature/my-change

# 2. Edit src/forge_engine/...

# 3. Test immediately — no server restart needed (editable install)
pytest tests/unit/               # fast feedback
pytest tests/integration/        # slower, full pipeline

# 4. Commit
git add -p                       # stage intentionally, not blindly
git commit -m "feat: describe the change"

# 5. Push and open PR
git push -u origin feature/my-change
# Open PR on GitHub — do not merge without approval
```

## Running a quick smoke test

```bash
python -c "from forge_engine import Pipeline, PipelineConfig; print('import OK')"
```

## Adding a new masking transform

1. Create `src/forge_engine/transforms/your_name.py` implementing `BaseMaskingStrategy`
2. Register it in `src/forge_engine/transforms/factory.py`
3. Export it in `src/forge_engine/transforms/__init__.py`
4. Add unit tests in `tests/unit/test_transforms.py`
5. Do NOT add it to `forge_engine/__init__.__all__` unless it's part of the public API

## Adding a new connector

1. Create `src/forge_engine/connectors/your_connector.py` subclassing `IOHandler`
2. Register it in `src/forge_engine/connectors/factory.py`
3. Add tests in `tests/unit/test_connectors.py`

## What belongs in `internal/` vs public

- If it's an implementation detail (base classes, validators, utilities) → `internal/`
- If CLI or platform code needs to reference it by name → public (add to `__all__`)
- When in doubt → `internal/` first; promote later if needed

## Dependency rule

This package has NO dependency on `forge` (CLI) or `forge-platform`. If you find yourself importing either, stop — that's an architecture violation.

## Package versioning

- `pyproject.toml` holds the version
- Bump patch for fixes, minor for new features, major for breaking `__all__` changes
- A breaking change is: removing an export from `__all__`, changing a function signature, or changing the YAML config schema
