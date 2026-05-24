"""Faker provider registration + reflection (V2.0-C internal impl).

Split out of the bundled internal/helpers.py. The public surface --
the registration / lookup functions callers reach for -- lives in
``decoy_engine.providers``. This module owns the implementation
details: the registry dicts, the Faker reflection denylist, the
reflected-provider wrapping, the make_faker locale fallback, and the
custom_providers/ directory scanner.

Why the public/internal split:
  - Callers should depend on the small public surface
    (register_faker_provider etc.). Adding to that surface is a
    deliberate API change.
  - The reflection denylist / _make_reflected_provider plumbing /
    make_faker locale handling are implementation details that may
    change without notice. Keeping them under internal lets V2.x
    tune them without breaking callers.
"""

from __future__ import annotations

import inspect as _inspect
import json
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from faker import Faker

_log = logging.getLogger(__name__)


_CUSTOM_FAKER_PROVIDERS: dict[str, Callable[[Faker], Any]] = {}
# Side registry of raw values per list-backed provider. Populated by
# _register_list_provider so the engine's FK pool resolver can read the
# values directly when a relationship targets custom_provider: <name>
# (tier-4 audit, 2026-05-20). Closure-only providers (operator-supplied
# callables that don't come from a list) won't appear here -- only list-
# backed providers expose their pool to the FK channel.
_CUSTOM_FAKER_PROVIDER_VALUES: dict[str, list[Any]] = {}


# ── Public-API implementations (re-exported via decoy_engine.providers) ──


def register_faker_provider(name: str, fn: Callable[[Faker], Any]) -> None:
    """Register a custom faker provider so `faker_type: <name>` resolves to
    `fn(faker_instance)`. Lets enterprise users add domain-specific providers
    (medical record numbers in a known shape, internal employee IDs, regional
    bank routing numbers, etc.) without forking the engine. Names overwrite
    on collision -- last registration wins; pass a no-op or unique prefix if
    the host process can re-import. Determinism: `fn` should derive its
    output from the seeded `Faker` instance only -- using `random.random()` /
    `time.time()` will break cross-run reproducibility."""
    if not isinstance(name, str) or not name:
        raise ValueError("custom faker provider name must be a non-empty string")
    if not callable(fn):
        raise TypeError("custom faker provider fn must be callable")
    _CUSTOM_FAKER_PROVIDERS[name] = fn


def unregister_faker_provider(name: str) -> None:
    """Remove a previously registered custom provider. No-op if the name was
    never registered. Mostly useful in tests."""
    _CUSTOM_FAKER_PROVIDERS.pop(name, None)
    _CUSTOM_FAKER_PROVIDER_VALUES.pop(name, None)


def get_custom_faker_provider_values(name: str) -> list[Any] | None:
    """Return the raw values list backing a list-backed custom provider,
    or None when the name isn't registered as a list provider."""
    values = _CUSTOM_FAKER_PROVIDER_VALUES.get(name)
    if values is None:
        return None
    return list(values)


def list_custom_faker_list_providers() -> list[str]:
    """Return names of all list-backed custom providers currently
    registered, sorted."""
    return sorted(_CUSTOM_FAKER_PROVIDER_VALUES.keys())


def register_faker_list_provider(name: str, values: list[str]) -> None:
    """Register a custom Faker provider backed by a fixed list of values."""
    if not isinstance(values, list):
        raise TypeError("custom faker provider values must be a list")
    frozen = [str(v) for v in values if v is not None]
    if not frozen:
        raise ValueError("custom faker provider values must not be empty")
    _register_list_provider(name, frozen)


def load_custom_providers(
    custom_dir: Path | None = None,
) -> dict[str, list[str]]:
    """Scan *custom_dir* for user-supplied word lists and register them as
    custom Faker providers under the ``custom.<stem>`` namespace.

    Supported file formats:
      .txt   - one value per line; blank lines and lines starting with `#` ignored.
      .json  - must be a JSON array of strings.

    Provider name = ``custom.<filename_stem>``. After this returns,
    pipelines can reference the lists via ``faker_type: custom.<stem>``.

    Default directory comes from the ``CUSTOM_PROVIDERS_DIR`` env-var,
    falling back to ``custom_providers`` relative to the cwd.

    Returns a {provider_name: values} mapping for callers that want to
    inspect or log what was registered.
    """
    if custom_dir is None:
        custom_dir = Path(os.getenv("CUSTOM_PROVIDERS_DIR", "custom_providers"))

    custom_dir = Path(custom_dir)

    if not custom_dir.exists():
        _log.debug("custom_providers: directory %s does not exist; skipping", custom_dir)
        return {}

    if not custom_dir.is_dir():
        _log.warning("custom_providers: %s exists but is not a directory; skipping", custom_dir)
        return {}

    loaded: dict[str, list[str]] = {}

    for path in sorted(custom_dir.iterdir()):
        if path.suffix.lower() == ".txt":
            values = _load_txt(path)
        elif path.suffix.lower() == ".json":
            values = _load_json(path)
        else:
            continue

        if not values:
            _log.warning("custom_providers: %s produced no values; skipping", path.name)
            continue

        provider_name = f"custom.{path.stem}"
        _register_list_provider(provider_name, values)
        loaded[provider_name] = values
        _log.info(
            "custom_providers: registered '%s' with %d values from %s",
            provider_name,
            len(values),
            path.name,
        )

    return loaded


# ── Internal: loaders + list-provider registration shared helper ──


def _load_txt(path: Path) -> list[str]:
    """Read a .txt file; one value per line. Ignores blank lines and comments."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]
    except Exception as exc:
        _log.warning("custom_providers: failed to read %s: %s", path.name, exc)
        return []


def _load_json(path: Path) -> list[str]:
    """Read a .json file containing a JSON array of strings."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            _log.warning(
                "custom_providers: %s must contain a JSON array; got %s",
                path.name,
                type(raw).__name__,
            )
            return []
        return [str(v) for v in raw if v is not None]
    except Exception as exc:
        _log.warning("custom_providers: failed to parse %s: %s", path.name, exc)
        return []


def _register_list_provider(provider_name: str, values: list[str]) -> None:
    """Register a random-choice provider for *values* under *provider_name*.

    Snapshots the list at registration time so later mutations to the
    caller's list don't affect generation behaviour. Also populates
    _CUSTOM_FAKER_PROVIDER_VALUES so the FK pool resolver can read the
    values list directly via parent: {custom_provider: <name>}.
    """
    frozen = list(values)
    _CUSTOM_FAKER_PROVIDER_VALUES[provider_name] = list(frozen)

    def _provider(fake: Faker) -> str:
        return str(fake.random.choice(frozen))

    register_faker_provider(provider_name, _provider)


# ── Faker reflection: providers exposed to the engine's masking surface ──
#
# Methods on a Faker instance that the engine never wants to surface as a
# masking provider, even though they're public. Two categories:
#   1. Wrong return type for a cell value: bytes, tuples, dicts, generators.
#   2. Configuration / introspection helpers that aren't providers.
# Anything not in this set + that is callable + that isn't a dunder is
# eligible. Reflection plus the curated overrides below gives the UI ~200
# safe providers without a hand-maintained whitelist.
_FAKER_DENYLIST: set[str] = {
    # Bytes / binary outputs.
    "binary",
    "image",
    "image_url",
    "tar",
    "zip",
    "json_bytes",
    # Non-scalar returns (tuples / dicts / generators / typed objects).
    "profile",
    "simple_profile",
    "time_series",
    "cryptocurrency",
    "currency",
    "latlng",
    "local_latlng",
    "location_on_land",
    "passport_owner",
    "pytimezone",
    "time_delta",
    "time_object",
    "pylist",
    "pyset",
    "pytuple",
    "pydict",
    "pyiterable",
    "pyobject",
    "pystruct",
    "enum",  # requires an enum class arg
    # Faker internals / configuration helpers (not masking providers).
    "add_provider",
    "add_arguments",
    "optional",
    "unique",
    "random",
    "get_arguments",
    "get_providers",
    "factories",
    "factory",
    "seed",
    "seed_instance",
    "seed_locale",
    "cache",
    "parse",
    "format",
    "pystr_format",
    "set_arguments",
    "set_formatter",
    "generator_attrs",
    "locales",
    "weights",
    "generator_method",
    "items",
}


def _coerce_to_str(value: Any) -> Any:
    """Best-effort: pandas cells take strings cleanly; everything else
    (Decimal, datetime, date) gets str()'d. Returning None for None
    preserves the engine's existing null-handling expectations."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    return str(value)


def _make_reflected_provider(method: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a bound Faker method so the engine can call it with arbitrary
    keyword args from a YAML ``faker_kwargs:`` block. Invalid kwargs are
    silently dropped rather than raised so a stale YAML doesn't kill a
    job -- the masker emits a warning at the call site."""
    try:
        sig = _inspect.signature(method)
        has_var_keyword = any(
            p.kind == _inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )
        param_names = {
            p.name
            for p in sig.parameters.values()
            if p.kind
            in (
                _inspect.Parameter.KEYWORD_ONLY,
                _inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
        } - {"self"}
    except (TypeError, ValueError):
        has_var_keyword = True
        param_names = set()

    def call(**kwargs: Any) -> Any:
        if not kwargs:
            return _coerce_to_str(method())
        if has_var_keyword:
            accepted = kwargs
        else:
            accepted = {k: v for k, v in kwargs.items() if k in param_names}
        return _coerce_to_str(method(**accepted))

    return call


def get_faker_providers(faker_instance: Faker) -> dict[str, Callable[..., Any]]:
    """Return a dict of every safe Faker provider on ``faker_instance``.

    Built by reflection: each public method that isn't in
    ``_FAKER_DENYLIST`` becomes an entry. Each value is a callable
    that accepts ``**kwargs`` so per-provider arguments flow through
    from YAML's ``faker_kwargs:`` block. Returns are str-coerced so a
    Decimal/datetime/date method works inline.

    Two intentional overrides on top of reflection:
      * ``address`` joins multiline output with ``, `` -- masking a CSV
        cell with an embedded newline breaks downstream consumers.
      * ``postcode`` aliases ``zipcode`` so older YAMLs don't generate
        "Unknown faker_type" warnings.

    Custom providers registered via ``register_faker_provider`` are
    added last and override reflection on name collision.
    """
    fake = faker_instance
    providers: dict[str, Callable[..., Any]] = {}

    for name in dir(fake):
        if name.startswith("_"):
            continue
        if name in _FAKER_DENYLIST:
            continue
        attr = getattr(fake, name, None)
        if not callable(attr):
            continue
        providers[name] = _make_reflected_provider(attr)

    def _addr(**kwargs: Any) -> str:
        out = fake.address(**kwargs) if kwargs else fake.address()
        return out.replace("\n", ", ") if isinstance(out, str) else _coerce_to_str(out)

    providers["address"] = _addr

    if "zipcode" in providers and "postcode" not in providers:
        providers["postcode"] = providers["zipcode"]

    # Custom providers wrap the seeded ``fake`` instance so user-supplied
    # functions can call any Faker method and inherit the per-value seed.
    # Override built-ins on name collision -- last registration wins.
    for name, fn in _CUSTOM_FAKER_PROVIDERS.items():
        providers[name] = lambda fn=fn, fake=fake: fn(fake)

    return providers


def make_faker(locale: str | list[str] | None = None) -> Faker:
    """Construct a `Faker` instance with optional locale override. Locale
    can be a single string (`'en_GB'`) or a list of strings -- Faker mixes
    them in the order given. `None` or empty returns the default `en_US`
    locale. Invalid locales fall back to `en_US` so a single bad pipeline
    rule doesn't poison the run.

    Caller is responsible for seeding the returned instance via
    `seed_instance(...)` for deterministic output."""
    if not locale:
        return Faker()
    try:
        return Faker(locale)
    except (AttributeError, ValueError, TypeError):
        return Faker()
