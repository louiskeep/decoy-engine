"""Shell-command parsing and cross-repository identity checks shared byte-for-byte by the CLI and
engine CI sentries. Everything here is repository-neutral; the policy lives in each repository's
own scanner (tests/sentry/_ci_lock_scan.py)."""
