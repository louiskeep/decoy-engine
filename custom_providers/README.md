# Custom Providers

This directory is the default mount point for user-supplied provider modules. Drop Python files here that register additional providers with `decoy-engine`'s provider registry.

## Loading

Custom provider files load at process start, in the calling process, with the calling user's privileges. Treat any custom-providers directory the same way you would treat any directory of executable Python: only place files here that you trust.

## Registering a provider

Each module in this directory may call `decoy_engine.providers_v2.register(...)` at import time. The registry is closed-checked by the planner; unknown providers fail validation with `code=unknown_provider` and a pointer back to the registry.

For the public registration API and the closed-check contract, see `src/decoy_engine/providers_v2/_registry.py`.

---

Full custom-providers guide lives in the commercial platform repo.
