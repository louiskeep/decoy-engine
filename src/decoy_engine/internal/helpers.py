# decoy_engine/utils/helpers.py
"""
General helper functions for the decoy_engine package.
"""

import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
from typing import Dict, Any, List, Optional, Callable
from faker import Faker

_log = logging.getLogger(__name__)


def deterministic_hash(value, seed=0):
    """
    Legacy SHA256(value + seed) hash. Kept for backwards compatibility when
    no master key is configured. Prefer ``hmac_hex`` (keyed) for any new
    code path so output is per-tenant and not derivable from the value alone.

    Args:
        value: The value to hash
        seed: A seed to ensure consistent hashing across runs

    Returns:
        A deterministic hash string
    """
    if value is None:
        return None

    # Convert to string and add seed
    value_str = f"{value}{seed}"

    # Create hash
    hash_obj = hashlib.sha256(value_str.encode())
    return hash_obj.hexdigest()


def hmac_hex(key: bytes, value) -> str:
    """HMAC-SHA256(key, value) as a 64-char hex string.

    The "Path B" deterministic primitive: same key + same input always
    yields the same output, with no per-tenant secret leakage (unlike
    SHA256(value + seed) where the seed is recoverable by brute force on
    a single known mapping).
    """
    if value is None:
        return None
    msg = str(value).encode("utf-8", errors="replace")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def hmac_seed(key: bytes, value) -> int:
    """Derive a 32-bit integer seed for Faker.seed_instance(...) from
    HMAC-SHA256(key, value). Same input + same key → same seed → same
    Faker output, with zero state stored anywhere.
    """
    if value is None:
        return 0
    msg = str(value).encode("utf-8", errors="replace")
    digest = hmac.new(key, msg, hashlib.sha256).digest()
    return int.from_bytes(digest[:4], "big")


_CUSTOM_FAKER_PROVIDERS: Dict[str, Callable[[Faker], Any]] = {}


def register_faker_provider(name: str, fn: Callable[[Faker], Any]) -> None:
    """Register a custom faker provider so `faker_type: <name>` resolves to
    `fn(faker_instance)`. Lets enterprise users add domain-specific providers
    (medical record numbers in a known shape, internal employee IDs, regional
    bank routing numbers, etc.) without forking the engine. Names overwrite
    on collision — last registration wins; pass a no-op or unique prefix if
    the host process can re-import. Determinism: `fn` should derive its
    output from the seeded `Faker` instance only — using `random.random()` /
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


def load_custom_providers(
    custom_dir: Path | None = None,
) -> Dict[str, List[str]]:
    """Scan *custom_dir* for user-supplied word lists and register them as
    custom Faker providers under the ``custom.<stem>`` namespace.

    Supported file formats
    ~~~~~~~~~~~~~~~~~~~~~~
    * ``.txt`` — one value per line; blank lines and lines starting with
      ``#`` are ignored.
    * ``.json`` — must be a JSON array of strings.

    The filename stem (without extension) becomes the provider name:

    .. code-block:: text

        custom_providers/
            dog_breeds.txt        →  custom.dog_breeds
            internal_codes.json   →  custom.internal_codes

    After calling this function, pipelines can reference the lists via::

        # masking YAML
        type: faker
        faker_type: "custom.dog_breeds"

        # generation column config
        type: faker
        faker_type: "custom.dog_breeds"

    The default directory is resolved from the ``CUSTOM_PROVIDERS_DIR``
    environment variable, falling back to ``custom_providers`` relative to
    the current working directory.

    Parameters
    ----------
    custom_dir:
        ``pathlib.Path`` to the directory to scan. When ``None``, the
        directory is read from the ``CUSTOM_PROVIDERS_DIR`` env-var or
        defaults to ``Path("custom_providers")``.

    Returns
    -------
    Dict[str, List[str]]
        Mapping of provider name (``custom.<stem>``) to the loaded value
        list.  Useful for callers that want to inspect or log what was
        registered without re-reading the files.
    """
    if custom_dir is None:
        custom_dir = Path(os.getenv("CUSTOM_PROVIDERS_DIR", "custom_providers"))

    custom_dir = Path(custom_dir)

    if not custom_dir.exists():
        _log.debug(
            "custom_providers: directory %s does not exist; skipping", custom_dir
        )
        return {}

    if not custom_dir.is_dir():
        _log.warning(
            "custom_providers: %s exists but is not a directory; skipping", custom_dir
        )
        return {}

    loaded: Dict[str, List[str]] = {}

    for path in sorted(custom_dir.iterdir()):
        if path.suffix.lower() == ".txt":
            values = _load_txt(path)
        elif path.suffix.lower() == ".json":
            values = _load_json(path)
        else:
            continue

        if not values:
            _log.warning(
                "custom_providers: %s produced no values; skipping", path.name
            )
            continue

        provider_name = f"custom.{path.stem}"
        # Capture values in a closure. Faker instance is passed so the
        # provider behaves like every other entry returned by
        # get_faker_providers(): it honours the seeded state but can
        # choose at random from the list via the seeded Faker instance.
        _register_list_provider(provider_name, values)
        loaded[provider_name] = values
        _log.info(
            "custom_providers: registered '%s' with %d values from %s",
            provider_name, len(values), path.name,
        )

    return loaded


def _load_txt(path: Path) -> List[str]:
    """Read a .txt file; one value per line. Ignores blank lines and comments."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        return [
            line.strip()
            for line in lines
            if line.strip() and not line.strip().startswith("#")
        ]
    except Exception as exc:
        _log.warning("custom_providers: failed to read %s: %s", path.name, exc)
        return []


def _load_json(path: Path) -> List[str]:
    """Read a .json file containing a JSON array of strings."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            _log.warning(
                "custom_providers: %s must contain a JSON array; got %s",
                path.name, type(raw).__name__,
            )
            return []
        return [str(v) for v in raw if v is not None]
    except Exception as exc:
        _log.warning("custom_providers: failed to parse %s: %s", path.name, exc)
        return []


def _register_list_provider(provider_name: str, values: List[str]) -> None:
    """Register a random-choice provider for *values* under *provider_name*.

    The provider function receives the seeded Faker instance so it can use
    ``fake.random.choice`` for deterministic selection — same seed → same
    pick, consistent with every other Faker-backed provider in the engine.
    """
    import random as _random

    # Snapshot the list at registration time so later mutations to the
    # caller’s list don’t affect generation behaviour.
    frozen = list(values)

    def _provider(fake: Faker) -> str:
        # Faker’s random attribute is the seeded Random instance — use it
        # so the selection inherits the per-value or per-row seed set by
        # the strategy / column generator before calling this provider.
        return fake.random.choice(frozen)

    register_faker_provider(provider_name, _provider)


# ── Faker reflection denylist ─────────────────────────────────
#
# Methods on a Faker instance that the engine never wants to surface as a
# masking provider, even though they’re public. Two categories:
#
#   1. Wrong return type for a cell value: bytes (binary/image/tar/zip/
#      json_bytes), tuples (currency, latlng, cryptocurrency,
#      passport_owner), dicts (profile, simple_profile), generators
#      (time_series), and other non-stringy returns (pytimezone,
#      time_delta, time_object, pyiterable, pylist, pyset, pytuple,
#      pyobject, pystruct, pydict).
#
#   2. Configuration / introspection helpers that aren’t providers
#      (random, factories, parse, format, set_arguments, etc.).
#
# Anything not in this set + that is callable + that isn’t a dunder is
# eligible. Reflection plus the curated overrides below gives the UI ~200
# safe providers without a hand-maintained whitelist.
_FAKER_DENYLIST: set[str] = {
    # Bytes / binary outputs.
    'binary', 'image', 'image_url', 'tar', 'zip', 'json_bytes',
    # Non-scalar returns (tuples / dicts / generators / typed objects).
    'profile', 'simple_profile', 'time_series',
    'cryptocurrency', 'currency',
    'latlng', 'local_latlng', 'location_on_land', 'passport_owner',
    'pytimezone', 'time_delta', 'time_object',
    'pylist', 'pyset', 'pytuple', 'pydict', 'pyiterable',
    'pyobject', 'pystruct',
    'enum',  # requires an enum class arg
    # Faker internals / configuration helpers (not masking providers).
    'add_provider', 'add_arguments', 'optional', 'unique', 'random',
    'get_arguments', 'get_providers', 'factories', 'factory',
    'seed', 'seed_instance', 'seed_locale', 'cache', 'parse', 'format',
    'pystr_format', 'set_arguments', 'set_formatter', 'generator_attrs',
    'locales', 'weights', 'generator_method', 'items',
}


def _coerce_to_str(value: Any) -> Any:
    """Best-effort: pandas cells take strings cleanly; everything else
    (Decimal, datetime, date) gets str()\'d. Returning None for None
    preserves the engine\'s existing null-handling expectations."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        # Hex string is safer than trying to UTF-8 decode arbitrary bytes.
        return value.hex()
    return str(value)


def _make_reflected_provider(method: Callable) -> Callable:
    """Wrap a bound Faker method so the engine can call it with arbitrary
    keyword args from a YAML ``faker_kwargs:`` block. Invalid kwargs are
    silently dropped rather than raised so a stale YAML doesn\'t kill a
    job — the masker emits a warning at the call site."""
    import inspect as _inspect
    try:
        sig = _inspect.signature(method)
        # Includes **kwargs methods (faker is sometimes flexible).
        has_var_keyword = any(
            p.kind == _inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )
        param_names = {
            p.name for p in sig.parameters.values()
            if p.kind in (
                _inspect.Parameter.KEYWORD_ONLY,
                _inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
        } - {'self'}
    except (TypeError, ValueError):
        has_var_keyword = True
        param_names = set()

    def call(**kwargs: Any) -> Any:
        if not kwargs:
            return _coerce_to_str(method())
        if has_var_keyword:
            accepted = kwargs
        else:
            # Drop unknown args. Anything the method doesn\'t accept can\'t
            # land in a TypeError here.
            accepted = {k: v for k, v in kwargs.items() if k in param_names}
        return _coerce_to_str(method(**accepted))

    return call


def get_faker_providers(faker_instance: Faker) -> Dict[str, Callable]:
    """Return a dict of every safe Faker provider on ``faker_instance``.

    Built by reflection: each public method that isn\'t in
    ``_FAKER_DENYLIST`` becomes an entry. Each value is a callable
    that accepts ``**kwargs`` so per-provider arguments (e.g.
    ``representation=\'alpha-3\'`` on ``country_code``) flow through from
    YAML\'s ``faker_kwargs:`` block. Returns are str-coerced so a
    Decimal/datetime/date method works inline.

    The engine\'s masking transforms call ``providers[faker_type](**kw)``.
    For legacy call sites that pass no kwargs, the entry behaves exactly
    like the old curated lambda.

    Two intentional overrides on top of reflection:
      * ``address`` joins multiline output with ``, `` — masking a CSV
        cell with an embedded newline breaks downstream consumers.
      * ``postcode`` aliases ``zipcode`` so older YAMLs (emitted by
        pre-2026-05-12 forecast/transform_metadata.py and the legacy
        default.yaml disguise mapping) don\'t generate "Unknown
        faker_type" warnings.

    Custom providers registered via ``register_faker_provider`` are
    added last and override reflection on name collision.
    """
    fake = faker_instance
    providers: Dict[str, Callable] = {}

    for name in dir(fake):
        if name.startswith('_'):
            continue
        if name in _FAKER_DENYLIST:
            continue
        attr = getattr(fake, name, None)
        if not callable(attr):
            continue
        providers[name] = _make_reflected_provider(attr)

    # ── overrides ────────────────────────────────────────────
    # ``address`` joins multiline output to a single line so CSV /
    # parquet cells stay one-row. The reflected version would leave the
    # newline in place, which breaks downstream tools that aren\'t
    # quoted-CSV-aware.
    def _addr(**kwargs: Any) -> str:
        out = fake.address(**kwargs) if kwargs else fake.address()
        return out.replace('\n', ', ') if isinstance(out, str) else _coerce_to_str(out)

    providers['address'] = _addr

    # ``postcode`` -> ``zipcode`` alias. Faker\'s en_US locale doesn\'t
    # expose ``postcode`` directly (some other locales do), so legacy
    # YAML used to trip the unknown-faker-type warning. Aliasing keeps
    # old pipelines warning-free; new ones should emit ``zipcode``.
    if 'zipcode' in providers and 'postcode' not in providers:
        providers['postcode'] = providers['zipcode']

    # Custom providers wrap the seeded ``fake`` instance so user-supplied
    # functions can call any Faker method (``fake.first_name()``,
    # ``fake.bban()``, etc.) and inherit the per-value seed for free.
    # Override built-ins on name collision — last registration wins.
    for name, fn in _CUSTOM_FAKER_PROVIDERS.items():
        providers[name] = lambda fn=fn, fake=fake: fn(fake)

    return providers


def make_faker(locale=None) -> Faker:
    """Construct a `Faker` instance with optional locale override. Locale
    can be a single string (`\'en_GB\'`) or a list of strings — Faker mixes
    them in the order given. `None` or empty returns the default `en_US`
    locale, preserving the pre-locale behavior. Invalid locales fall back
    to `en_US` with a warning so a single bad pipeline rule doesn\'t
    poison the run.

    Caller is responsible for seeding the returned instance via
    `seed_instance(...)` for deterministic output."""
    if not locale:
        return Faker()
    try:
        return Faker(locale)
    except (AttributeError, ValueError, TypeError):
        # Faker raises AttributeError for unknown locales like \'xx_YY\'.
        # Swallow + fall back so the pipeline still runs end-to-end.
        return Faker()


def convert_quoting_mode(quoting_mode: str) -> int:
    """
    Convert a quoting mode string to the corresponding CSV module constant
    
    Args:
        quoting_mode: String representation of quoting mode
        
    Returns:
        Integer value matching csv module constants
    """
    quoting_map = {
        'minimal': 0,  # csv.QUOTE_MINIMAL
        'all': 1,      # csv.QUOTE_ALL
        'nonnumeric': 2,  # csv.QUOTE_NONNUMERIC
        'none': 3      # csv.QUOTE_NONE
    }
    return quoting_map.get(quoting_mode.lower(), 0)


def create_directory_for_file(file_path: str) -> None:
    """
    Create the directory for a file path if it doesn\'t exist
    
    Args:
        file_path: Path to a file
    """
    import os
    from pathlib import Path
    
    directory = os.path.dirname(file_path)
    if directory:
        Path(directory).mkdir(parents=True, exist_ok=True)


def is_path_exists(path: str) -> bool:
    """
    Check if a path exists (file or directory)
    
    Args:
        path: Path to check
        
    Returns:
        True if path exists, False otherwise
    """
    import os
    return os.path.exists(path)


def get_filename_without_extension(file_path: str) -> str:
    """
    Get the filename without extension from a path
    
    Args:
        file_path: Path to a file
        
    Returns:
        Filename without extension
    """
    import os
    base_name = os.path.basename(file_path)
    return os.path.splitext(base_name)[0]


def convert_file_size(size_bytes: int) -> str:
    """
    Convert file size in bytes to a human-readable string
    
    Args:
        size_bytes: File size in bytes
        
    Returns:
        Human-readable file size string
    """
    # Define unit prefixes
    units = ['B', 'KB', 'MB', 'GB', 'TB']
    
    # Special case for size=0
    if size_bytes == 0:
        return '0 B'
    
    # Determine the appropriate unit
    i = 0
    while size_bytes >= 1024 and i < len(units) - 1:
        size_bytes /= 1024
        i += 1
    
    # Format with appropriate precision
    if i == 0:  # Bytes
        return f"{size_bytes:.0f} {units[i]}"
    else:
        return f"{size_bytes:.2f} {units[i]}"


def get_file_size(file_path: str) -> Optional[int]:
    """
    Get the size of a file in bytes
    
    Args:
        file_path: Path to the file
        
    Returns:
        File size in bytes or None if file doesn\'t exist
    """
    import os
    if os.path.exists(file_path) and os.path.isfile(file_path):
        return os.path.getsize(file_path)
    return None


def format_elapsed_time(seconds: float) -> str:
    """
    Format elapsed time in seconds to a human-readable string
    
    Args:
        seconds: Time in seconds
        
    Returns:
        Formatted time string
    """
    if seconds < 60:
        return f"{seconds:.1f} seconds"
    elif seconds < 3600:
        minutes = seconds / 60
        return f"{minutes:.1f} minutes"
    else:
        hours = seconds / 3600
        return f"{hours:.1f} hours"
