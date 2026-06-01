"""Public custom-Faker-provider API (V2.0-C).

Single canonical location for registering and inspecting custom Faker
providers. Pre-V2.0-C this surface lived inside
``decoy_engine.internal.helpers``; V2.0-C lifts the public functions
here so callers (CLI scripts, platform admin endpoints, user-supplied
custom_providers/ directories) stop reaching into
``decoy_engine.internal.*``.

The implementation backing each function lives in
``decoy_engine.internal.faker_setup`` -- internal because the
Faker-reflection denylist / curated overrides / make_faker plumbing
should not be part of the public contract. This module is a thin
public wrapper: the function names and signatures are the stable
surface.

What's public here:
  - register_faker_provider:        register a callable provider.
  - register_faker_list_provider:   register a list-backed provider
                                     (FK-pool readable).
  - unregister_faker_provider:      remove a previously-registered
                                     provider (mainly used in tests).
  - get_custom_faker_provider_values: read the raw values list for a
                                     list-backed provider; the FK pool
                                     resolver uses this.
  - list_custom_faker_list_providers: enumerate list-backed providers
                                     for the platform's relationship-
                                     authoring UI.
  - load_custom_providers:          scan a directory of .txt / .json
                                     files and register each as a
                                     custom.<stem> provider.
"""

from __future__ import annotations

from decoy_engine.internal.faker_setup import (
    atomic_swap_db_providers,
    get_custom_faker_provider_values,
    list_custom_faker_list_providers,
    load_custom_providers,
    register_faker_list_provider,
    register_faker_provider,
    unregister_faker_provider,
)

__all__ = [
    "atomic_swap_db_providers",
    "get_custom_faker_provider_values",
    "list_custom_faker_list_providers",
    "load_custom_providers",
    "register_faker_list_provider",
    "register_faker_provider",
    "unregister_faker_provider",
]
