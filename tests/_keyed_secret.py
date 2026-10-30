"""The ONE shared throwaway secret for tests that exercise keyed strategies (F13 HIGH-1).

Removing the pre-GA keyed `job_seed` fallback means every keyed run needs a secret. Tests that
do not care which secret use `THROWAWAY_KEY_PROVIDER`, installed by the autouse fixture in
`tests/conftest.py` at the public admission seams: it fills in a missing key_provider ONLY when
the work is keyed, so unkeyed behavior is untouched and every keyed assertion still runs against
a real admitted secret. Tests that assert no-secret behavior opt out with
`@pytest.mark.no_default_secret`; tests that pin a secret pass their own `key_provider=`.
"""

from __future__ import annotations

from decoy_engine.keyprovider import SecretKeyProvider

THROWAWAY_SECRET = b"decoy-test-throwaway-secret-0123456789"  # 38 bytes; never a real credential
THROWAWAY_KEY_PROVIDER = SecretKeyProvider(THROWAWAY_SECRET, key_version="v1")
