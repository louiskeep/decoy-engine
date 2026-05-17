# Security Policy

`decoy-engine` is the masking + generation library that ships standalone (PyPI) and is consumed by `decoy-platform` and `decoy` (CLI).

## Reporting a vulnerability

**Use GitHub Private Vulnerability Reporting:**

[https://github.com/louiskeep/decoy-engine/security/advisories/new](https://github.com/louiskeep/decoy-engine/security/advisories/new)

The report is private and visible only to repository maintainers. We will acknowledge receipt within 3 business days and aim to provide an initial assessment within 7 business days. Please do not file a public issue or contact support channels for security disclosures.

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

## Where to read more

Central security review: see the platform repo's [Security Architecture](../decoy-platform/docs/security/SECURITY_ARCHITECTURE.md) for trust boundaries, sensitive assets, threat model, key model, and named V1 limits.

Engine-specific security notes: [engine-security.md](../decoy-platform/docs/guides/engine-security.md).
