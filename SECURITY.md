# Security Policy

`decoy-engine` is the masking + generation library that ships standalone (PyPI) and is consumed by `decoy-platform` and `decoy` (CLI).

## Reporting a vulnerability

Email **security@decoy.so** with subject line `[SECURITY] <one-line summary>`.

We will acknowledge receipt within 3 business days and aim to provide an initial assessment within 7 business days. Please do not file a public issue or contact support channels for security disclosures.

> **Note:** R8-REV-03 (dedicated security inbox) is in progress. Until it is live, reports sent to this address are forwarded to the security team directly.

Include as much detail as you can:

- The affected version(s) of `decoy-engine`.
- Steps to reproduce, or proof-of-concept code.
- The impact you believe the issue has.
- Any mitigations or workarounds you are aware of.

## Coordinated disclosure

We follow a 90-day coordinated-disclosure window from the date of initial report. We will work with you on a public-disclosure timeline that gives users a reasonable opportunity to update.

If a critical issue is being actively exploited, we may shorten this window.

## Supported versions

Only the most recent minor release of `decoy-engine` receives security fixes. Older versions are not patched.

## Security posture summary

`decoy-engine` is a library: it has no network surface, no auth boundary, and no background process of its own. It runs inside the caller's Python process with the caller's privileges.

- **Input data and output data stay local** to the process running the engine. No telemetry, no remote logging.
- **Keyed masking** derives from a caller-supplied secret referenced by `global_settings.mask_secret_ref` (`env:NAME` or `file:/PATH`) and resolved through `decoy_engine.keyprovider`. The secret must be at least 32 bytes; per-purpose keys are derived via HKDF-SHA256. At GA the engine fails closed: any plan with a keyed re-identification surface (FPE, hash-for-join, code_set, joint_mask, date_shift, `vault: true` columns, including nested children) requires a resolved secret of that strength, or the run raises `KeyedStrategyRequiresSecret`. The secret is never serialized into plans, logs, or manifests. See [`docs/security/key-derivation.md`](docs/security/key-derivation.md).
- **SQL surfaces** in the engine are parameter-bound. Source connectors that accept user SQL are documented in [`docs/security/sql-surfaces.md`](docs/security/sql-surfaces.md).
- **Custom provider files** load at process start with the user's privileges. Treat a custom-providers directory the same way you would treat any directory of executable Python.
- **Pre-1.0 caveat.** The engine is at version 0.1.0. The public API and the keyed-mask secret contract are not yet frozen; breaking changes are possible until 1.0.0 ships.
