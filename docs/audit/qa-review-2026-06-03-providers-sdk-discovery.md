# QA Review: providers_v2, sdk, data_discovery, expressions, validation, validation_result

**Date:** 2026-06-03
**Scope:** `src/decoy_engine/providers_v2/` (all files), `sdk.py`, `data_discovery.py`,
`expressions.py`, `errors.py`, `validation/_config.py`, `validation_result.py`
**Prior QA coverage excluded:** `_strategies/`, `instrumentation/`, `internal/`,
`validation/post/`, `walks/`, `disguises/`, `quality/`, `generation/`, `plan/`,
`execution/`, `config/`, `storm/`, `transforms/`, `connectors/`, `relationships/`,
`generators/`, `profile/`, `context.py`, `determinism/`

---

## 1. Summary

Nine DecoyNative identifier adapters (SSN, EIN, NPI, NDC, MRN, PAN, ICD-10, IBAN, CUSIP) share a
silent logic fault: when `spec.deterministic=True` and `source_value is None`, every adapter falls
through to unseeded `np.random.default_rng()` and returns a non-deterministic value with no error.
Callers that hold `spec.deterministic=True` believe output is seed-stable; they are wrong. This is
the most dangerous finding in this review. Separately, the CUSIP Luhn docstring directly contradicts
the (correct) implementation, guaranteeing a future developer will "fix" the code toward the wrong
behavior. The `data_discovery` view-name pathway lacks input sanitisation and is a SQL-injection
surface if caller-controlled names reach it.

---

## 2. Findings

### F1 — CRITICAL | Correctness
**All nine DecoyNative adapters silently produce non-deterministic output when `spec.deterministic=True` and `source_value is None`**

Files: `_ssn.py`, `_pan.py`, `_iban.py`, `_npi.py`, `_icd10.py`, `_cusip.py`,
`_ein.py`, `_ndc.py`, `_mrn.py`

Every adapter follows the pattern:
```python
def generate(self, provider, *, spec, source_value=None):
    if spec.deterministic and source_value is not None:
        return derive_value(...)          # deterministic path
    rng = np.random.default_rng()         # unseeded — non-deterministic
    return generate_random(rng=rng, ...)
```

When `spec.deterministic=True` and `source_value=None` (a caller that forgot to pass the source
value, or a column where the source is null), the condition `spec.deterministic and
source_value is not None` is `False`, and the adapter silently falls to `default_rng()` — an
unseeded, OS-entropy RNG. The caller's audit log shows `deterministic=True`; the output is random.
This breaks the engine's core invariant across all nine identifiers.

**Impact:** Any job with null source values in a deterministic column silently shifts from a stable
mapping to an unseeded random one. Across runs the same null source produces different masked
values, breaking FK consistency and reproducibility. The failure is silent — no exception, no log
entry.

**Fix:** Raise explicitly on the impossible combination:
```python
def generate(self, provider, *, spec, source_value=None):
    if provider != "synthetic_ssn":
        raise ProviderError(...)
    if spec.deterministic:
        if source_value is None:
            raise ProviderError(
                code="missing_source_value",
                message=(
                    "SsnAdapter: deterministic mode requires a non-None source_value. "
                    "If the source column contains nulls, configure a null_action "
                    "policy (skip_row / constant / use_literal) on the column spec."
                ),
            )
        canonical = (
            source_value if isinstance(source_value, bytes)
            else _canonicalize_source(source_value)
        )
        if spec.seed is None:
            raise ProviderError(code="missing_seed", ...)
        if spec.namespace is None:
            raise ProviderError(code="missing_namespace", ...)
        return derive_value(seed=spec.seed, namespace=spec.namespace,
                            source=canonical, domain=SsnDomain())
    rng = np.random.default_rng()
    return generate_random(rng=rng, locale=spec.locale or "en_US")
```

Apply the same restructuring to all nine adapters. The `if spec.deterministic:` block must be
mutually exclusive with the random path; there must be no fall-through.

**Verify:** Property-based test with Hypothesis: for every adapter, for any `source_value is None`
plus `spec.deterministic=True`, assert `generate(...)` raises `ProviderError(code='missing_source_value')`.

---

### F2 — HIGH | Correctness
**CUSIP `_luhn_check_digit` docstring directly contradicts the (correct) implementation**

File: `providers_v2/identifiers/_cusip.py`

The function docstring states:
> "equivalent to doubling positions 0, 2, 4, 6 (zero-indexed)"

The actual code:
```python
if i % 2 == 1:  # 2nd, 4th, 6th, 8th positions (0-indexed: 1, 3, 5, 7)
    v *= 2
```

The code doubles 0-indexed positions 1, 3, 5, 7 — which is the correct CGS behavior (double
1-indexed even positions 2, 4, 6, 8). The docstring claiming "positions 0, 2, 4, 6" is wrong.
The module-level docstring compounds this by calling the doubled positions "odd-position
characters" when they are at even positions in CGS 1-indexed notation.

**Impact:** A developer reading the docstring will conclude the code is inverted and "fix" it to
`i % 2 == 0`, which would produce incorrect check digits that pass the CUSIP alphabet test but
fail real CUSIP validators. The wrong docstring is a ticking bug.

**Fix:**
```python
def _luhn_check_digit(body8: str) -> int:
    """Compute the CUSIP modified-Luhn check digit for an 8-char body.

    Per CGS spec: characters at 1-indexed even positions (2, 4, 6, 8) —
    that is 0-indexed positions 1, 3, 5, 7 — have their values doubled.
    """
    total = 0
    for i, ch in enumerate(body8):
        v = _char_value(ch)
        if i % 2 == 1:  # 0-indexed 1,3,5,7 = 1-indexed even positions 2,4,6,8
            v *= 2
        v = (v // 10) + (v % 10)
        total += v
    return (10 - (total % 10)) % 10
```

Also fix the module docstring: "Every **even-position** character (1-based, positions 2, 4, 6, 8)
is doubled."

**Verify:** `assert CusipValidator.is_valid(CusipAdapter().generate('synthetic_cusip', spec=...))`.
Compare generated check digits against the CGS test vector `"037833100"` (Apple Inc. CUSIP).

---

### F3 — HIGH | Correctness | Security
**`data_discovery.py`: view name not validated before `create_view(name)`**

File: `data_discovery.py`, `run_discovery_sql`, ~L140

```python
con.read_parquet(path).create_view(name, replace=True)
```

DuckDB's `create_view` internally generates `CREATE [OR REPLACE] VIEW "<name>" AS ...`. If `name`
contains a double-quote character, the generated SQL becomes malformed and could escape the quoting
context. Example: `name = 'foo"--'` produces `CREATE VIEW "foo"--" AS ...`, where the view is named
`foo` and `--" AS ...` becomes a comment, potentially creating a view with an unexpected name and
suppressing the `AS ...` body.

Depending on how `tables` is populated upstream (platform-managed table catalog vs. any
caller-supplied dict), this is an injection surface of varying severity. The code currently has no
guard.

**Impact:** At minimum, a double-quote in a table name would cause an opaque DuckDB error. At worst
it allows SQL injection through the view-name channel in environments where `tables` keys are
caller-influenced.

**Fix:** Validate view names at the entry point. Alphanumeric + underscore only:

```python
import re as _re
_SAFE_TABLE_NAME_RE = _re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')

def run_discovery_sql(sql, tables, *, row_limit=10_000):
    _validate_select_only(sql)
    con = duckdb.connect(":memory:")
    try:
        for name, path in tables.items():
            if not _SAFE_TABLE_NAME_RE.match(name):
                raise DiscoverySqlError(
                    f"Invalid table name {name!r}. Only [A-Za-z_][A-Za-z0-9_]* allowed."
                )
            con.read_parquet(path).create_view(name, replace=True)
        ...
```

**Verify:** `run_discovery_sql("SELECT 1", {"bad\"name": "/tmp/x.parquet"})` must raise
`DiscoverySqlError` before touching DuckDB.

---

### F4 — MEDIUM | Correctness | Data
**`_iban.py`: generator produces all-digit BBANs for countries with alphanumeric BBANs**

File: `providers_v2/identifiers/_iban.py`

`_generate_iban_from_bytes` and `generate_random` both construct the BBAN as a string of decimal
digits only:

```python
bban_int = int.from_bytes(b[1:], "big") % (10 ** bban_length)
bban = f"{bban_int:0{bban_length}d}"   # always digits
```

However, several countries in `_COUNTRIES_ORDERED` / `_IBAN_LENGTH_BY_COUNTRY` have
BBAN structures that include alpha characters per the SWIFT IBAN registry:

| Country | Alpha chars in BBAN |
|---------|----------------------|
| GB      | 4-char bank code (e.g., NWBK, LOYD) |
| GI      | 4-char bank code |
| QA, LC  | 4-char bank code |
| MT      | 4-char bank code |
| VA, VG  | 4-char bank code |

A generated GB IBAN will have the correct check digits and correct total length, but a
purely-numeric 4-char "bank code" that doesn't match any real sort-code prefix. Downstream EHR or
payment systems that validate BBAN structure (not just mod-97) will reject these. If
`_iban_valid` in `storm.detectors` validates BBAN structure beyond mod-97, the `IbanValidator`
will also reject them.

**Impact:** Generated IBANs for alphanumeric-BBAN countries fail real-world BBAN structure
checks. This is silent — the adapter returns a string, passes the `IbanValidator.is_valid` call
if the validator only checks mod-97, but gets rejected by production banking validators.

**Fix (minimum):** Either (a) restrict `_COUNTRIES_ORDERED` to countries with all-digit BBANs
(removing GB, GI, QA, LC, MT, VA, VG) or (b) implement per-country BBAN structure generators.
Option (a) is the safe near-term fix:

```python
# Countries whose BBANs are all-digit (SWIFT IBAN registry, 2026-06-01).
# Excluded: GB, GI, QA, LC, MT, VA, VG (contain alpha bank codes).
_ALL_DIGIT_BBAN_COUNTRIES: frozenset[str] = frozenset({
    "AD", "AE", "AL", "AT", "AZ", "BA", "BE", "BG", "BH", "BR", ...
})
_COUNTRIES_ORDERED = tuple(sorted(_ALL_DIGIT_BBAN_COUNTRIES & _IBAN_LENGTH_BY_COUNTRY.keys()))
```

Add a comment in `_IBAN_LENGTH_BY_COUNTRY` marking the excluded countries as "alpha-BBAN, not
yet supported."

**Verify:** For every country in `_COUNTRIES_ORDERED`, generate 1000 IBANs and assert
`IbanValidator.is_valid(iban)` for each. Add a regression test confirming GB is not in
`_COUNTRIES_ORDERED`.

---

### F5 — MEDIUM | Correctness | UX
**`validation/_config.py`: `_select_validator` error message is self-contradictory**

File: `validation/_config.py`, `_select_validator` ~L45

When the config does not have `version: 1`, the function raises:
```
"v1 mask + v1 generate config shapes are no longer validated by validate_config (S9 removal).
Use a `version: 1` PipelineConfig (see decoy_engine.PipelineConfig.model_validate) for v2
mask + v2 generate configs."
```

The message says "v1 shapes are no longer validated" in the first sentence, then says "use
version: 1" in the second — as though `version: 1` is the deprecated v1 shape. In reality
`version: 1` **is** the current V2 `PipelineConfig`. The error misleads users who are migrating
from the old v1 YAML shape (no version field, `masking_rules:` or `tables:` at the root).

**Impact:** A user trying to migrate from an old YAML gets an error that implies their target
format is also obsolete. Support burden; likely to cause repeated tickets.

**Fix:**
```python
raise PipelineValidationError(
    "Unrecognised config shape. The legacy `masking_rules:` (mask) and "
    "`tables: dict` (generate) V1 formats are no longer supported. "
    "Convert to a V2 PipelineConfig (top-level `version: 1`, `nodes:` list, "
    "`edges:` list) and pass it to `PipelineConfig.model_validate(data)` "
    "directly. See the migration guide in docs/guides/v2-migration.md."
)
```

---

### F6 — MEDIUM | Security
**`expressions.py`: `safe_eval` with `re` module allows unrestricted attribute traversal**

File: `expressions.py`, `MASK_GLOBALS`

```python
MASK_GLOBALS: dict[str, Any] = {
    "__builtins__": {},
    "re": _re,
    ...
}
```

`re` is included as a complete module object. CPython's standard sandbox-escape path — accessing
`__class__.__mro__[-1].__subclasses__()` to reach `object` and enumerate subclasses — is blocked
by `__builtins__: {}`, but attribute traversal through module objects can still reach sensitive
internals:

```python
# In a formula string:
re.compile.__globals__["os"].system("...")
```

Whether this is exploitable depends entirely on the trust model of formula authors. If formula
strings are operator-written config (trusted), risk is acceptable. If any user-submitted
formula string reaches `safe_eval`, this is a high-severity sandbox escape.

**Impact:** Operator-configured formulas: acceptable. User-submitted formulas (UI, API): critical.

**Fix (if user-submitted formulas are possible):** Pass a module proxy that only exposes safe
`re` functions:

```python
class _SafeRe:
    match = staticmethod(_re.match)
    search = staticmethod(_re.search)
    sub = staticmethod(_re.sub)
    fullmatch = staticmethod(_re.fullmatch)
    split = staticmethod(_re.split)
    findall = staticmethod(_re.findall)
    compile = staticmethod(_re.compile)  # omit to block pattern objects
```

If formulas are always operator-configured, document this explicitly in `MASK_GLOBALS`:
```python
# Trust model: formulas are operator-configured config, not user-submitted.
# Full re module is intentionally included; adding user-submitted formula
# support requires replacing this with _SafeRe.
"re": _re,
```

**Verify:** `safe_eval('re.compile.__globals__["os"]', MASK_GLOBALS, {})` — confirm whether this
evaluates or raises `KeyError`.

---

### F7 — MEDIUM | Correctness
**`_pan.py`: `PanDomain.from_bytes` comment references wrong byte slice**

File: `providers_v2/identifiers/_pan.py`, `PanDomain.from_bytes`

```python
# Body9 from bytes[1:9]; mod by 10^9 to fit.
rest9 = int.from_bytes(b[0:9], "big") % 1_000_000_000
```

The comment says `b[1:9]` (8 bytes) but the code uses `b[0:9]` (9 bytes). The code is correct
for the 9-digit body — 9 bytes = 72 bits, max value ~4.7e21, well above 10^9, so the modulo
gives uniform coverage. The comment is simply wrong.

**Impact:** Low functional impact today. Future developer editing the comment-based "spec" would
naturalise to `b[1:9]`, shifting the byte slice and breaking determinism across the version
boundary.

**Fix:** Correct the comment to `b[0:9]`.

---

### F8 — LOW | Design
**`sdk.py`: `FileSource.head` default implementation is O(N) in list size**

File: `sdk.py`, `FileSource.head`

```python
def head(self, path: str) -> FileMeta:
    for item in self.list():
        if item.path == path:
            return item
    raise PermanentError(...)
```

The docstring acknowledges this. However, the `head` signature does not carry a warning that
S3/GCS connectors should override for correctness reasons (not just performance) — S3's
`list_objects` doesn't return `content_type`; a `head` that relies on `list()` will silently
return `content_type=None` for S3 objects even when the connector could resolve it via
`head_object`. The performance concern is documented; the content-type accuracy concern is not.

**Fix:** Extend the docstring:
```
Note: for S3/GCS connectors, `list()` does not return content_type; override
`head()` with a native HEAD call (`head_object`, `Blob.reload`) to populate it.
```

---

### F9 — LOW | Correctness
**`_ssn.py` blocklist: `_INVALID_AREAS` uses `frozenset({0, 666, *range(900, 1000)})`**

File: `providers_v2/identifiers/_ssn.py`

`range(900, 1000)` covers 900–999 (100 values). The SSA POMS rule excludes areas 900–999, which
is correct. But `frozenset({0, ...})` adds 0 explicitly — the SSA POMS rule says area "000" is
invalid, not specifically area 0 treated as a numeric. In `_is_blocklisted`, `area =
int(ssn_digits[:3])` converts "000" to `0`. The set contains `0`, so the check is correct.
However, the comment and the literal `0` (vs. the more explicit `range(0, 1)`) make the code
slightly opaque — "area 0" doesn't map obviously to "area 000" without parsing the logic.

**Fix (nit):** Change `0` to `range(0, 1)` for consistency with the range pattern, or add a
comment: `0,  # Area 000 per POMS`.

---

### F10 — LOW | Reliability
**`providers_v2/_registry.py`: `get_default_registry` is not thread-safe for first build**

File: `providers_v2/_registry.py`, `get_default_registry`

```python
_DEFAULT_REGISTRY: ProviderRegistry | None = None

def get_default_registry() -> ProviderRegistry:
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        ...
        _DEFAULT_REGISTRY = ProviderRegistry(bindings)
    return _DEFAULT_REGISTRY
```

Two threads hitting this simultaneously both see `None`, build the registry independently, and
one overwrites the other. In CPython, dict assignment is GIL-atomic, so the result is always a
valid `ProviderRegistry`. However, both threads pay the construction cost (FakerAdapter +
DecoyNative imports + Mimesis check), and the two instances are different objects — any test
relying on singleton identity (`get_default_registry() is get_default_registry()`) can fail
under concurrent startup.

**Fix:** Use a threading lock or `functools.lru_cache(maxsize=None)` on a no-arg inner function:
```python
from functools import lru_cache

@lru_cache(maxsize=None)
def get_default_registry() -> ProviderRegistry:
    ...
```

Note: `_reset_default_registry_for_tests()` would need updating if you switch to `lru_cache`
(call `get_default_registry.cache_clear()`).

---

### F11 — NIT | Design
**`_cusip.py` module docstring uses "odd-position" for what are 1-indexed even positions**

File: `providers_v2/identifiers/_cusip.py`, module docstring

```
- Every odd-position character (1-based, so positions 2, 4, 6, 8) is doubled
```

Positions 2, 4, 6, 8 in 1-based indexing are by definition even, not odd. The CGS spec calls
them "even positions." This is the seed from which the F2 docstring contradiction grew.

**Fix:** Change to "even-position character (1-based: positions 2, 4, 6, 8)".

---

### F12 — NIT | Design
**`validation_result.py`: `ValidationError` docstring references stale `decoy_engine.graph.validators.*`**

File: `validation_result.py`, `ValidationError` class docstring

```
Lower-level validation failure raised by individual modular validators
(``decoy_engine.graph.validators.*``).
```

`decoy_engine.graph` was removed under the V2 clean break. The validators now live under
`decoy_engine.validation.*` (and platform-side in `api/validations/`). The stale reference is
in the `errors.py` module's `ValidationError` docstring, not `validation_result.py` itself.

**Fix:** Update to `(decoy_engine.validation.*)`.

---

## 3. Performance Notes

**Bottleneck classification for the reviewed surface:**

| Module | Bottleneck type | Notes |
|--------|-----------------|-------|
| `data_discovery.py` | I/O | DuckDB Parquet scan; `row_limit` cap is correct. Predicate pushdown works. |
| `providers_v2/_faker_adapter.py` | CPU + GIL | `generate_batch` is a Python list comprehension with per-call Faker invocations. No vectorization opportunity; Faker is not vectorizable. The per-call overhead is ~5–20 µs; at 100k rows that's 0.5–2 s per column. Profile with `cProfile` or `py-spy` before claiming it's a bottleneck. |
| `providers_v2/identifiers/*.py` | CPU | `derive_value` → HKDF-SHA256 per row. Each row costs ~2–10 µs (HMAC-SHA256). At 1M rows per masked column that's 2–10 s. Batch-mode HKDF is the optimization target but is a design-level change out of scope of this slice. |
| `_faker_adapter.py` `_faker_instances` | Memory | One `Faker` instance per locale. Typically 1 locale → negligible. 100 locales in a multi-locale job → ~30 MB. Not a concern in practice. |
| `data_discovery.py` `_coerce` | CPU | Per-row Python type dispatch with `isinstance` + `hasattr`. For a 10k-row query result this is fast; for 100k+ rows consider moving coercion to a vectorised op (Polars cast + `to_list()` is 10–100x faster). Add a `row_limit` note to the platform API: the current 10k cap is the right production default. |

**What to measure first if throughput complaints arrive:**
```bash
# Throughput for one masked SSN column at 100k rows:
python -m cProfile -s cumulative \
    -c "from decoy_engine.providers_v2.identifiers._ssn import SsnAdapter, ProviderSpec; ..."
# Or use scalene for memory + CPU breakdown:
scalene --cpu --memory tests/perf_fixtures/ssn_column.py
```

---

## 4. Suggested Tests

| Test case | What it guards |
|-----------|----------------|
| `test_all_adapters_raise_on_deterministic_with_null_source` — Hypothesis: for each adapter, `generate(provider, spec=Spec(deterministic=True), source_value=None)` raises `ProviderError(code='missing_source_value')` | F1 regression |
| `test_cusip_check_digit_against_known_vector` — `CusipValidator.is_valid("037833100")` (Apple AAPL, a known valid CUSIP) and `generate_cusip_from_body("03783310") == "037833100"` | F2 regression (verify code not docstring) |
| `test_view_name_injection_rejected` — `run_discovery_sql("SELECT 1", {'bad"name': str(tmp_path)})` must raise `DiscoverySqlError` | F3 regression |
| `test_iban_alpha_bban_countries_excluded` — assert "GB" not in `_COUNTRIES_ORDERED` after fix | F4 |
| `test_iban_generated_passes_own_validator` — for every country in `_COUNTRIES_ORDERED`, 500 generated IBANs all pass `IbanValidator.is_valid` | F4 coverage |
| `test_validate_config_error_message_no_version_field` — config with `masking_rules:` root but no `version:` field; assert the raised message contains "V2 PipelineConfig" and "nodes:" | F5 UX |
| `test_safe_eval_re_module_attribute_access` — confirm `safe_eval('re.compile.__globals__', MASK_GLOBALS, {})` behavior is understood (document result; if using `_SafeRe` proxy, confirm `__globals__` is not accessible) | F6 |
| `test_pan_domain_determinism` — same seed + source → same PAN across 3 independent `derive_value` calls | determinism regression for PAN |
| `test_registry_singleton_identity` — two calls to `get_default_registry()` return the same object | F10 |
| `test_ssn_blocklist_area_zero` — `SsnValidator.is_valid("000-42-1234")` is False | F9 |
| `test_npi_regex_matches_generated` — all `generate_random`-produced NPIs match `_NPI_REGEX` | NPI format coverage |

---

## 5. What's Good

- **`data_discovery.py` SQL safety design is thorough.** The three-layer filter (leading-keyword
  whitelist + banned-keyword regex + `\bFROM\s+['"]` path-FROM block) was clearly thought through
  with the DuckDB-specific escape vectors in mind. The QA-2 annotations trace each block to its
  source audit finding. The `_coerce` helper handles all DuckDB scalar types cleanly.

- **`validation_result.py` / `errors.py` exception hierarchy** is clean and well-factored.
  `PipelineValidationError` carrying stable `code` strings alongside human-readable messages is
  the right design for a UI-driving validation surface. `raw_message` on `ValidationError`
  avoiding the formatted prefix for programmatic consumers is a thoughtful detail.

- **All nine identifier adapters follow a consistent shape.** The `from_bytes` / `generate_random`
  / `Adapter.generate` / `generate_batch` / `capability_matrix` structure is uniform across
  SSN, PAN, NPI, NPI, NDC, MRN, ICD-10, IBAN, CUSIP. Adding a new identifier is a clear
  copy-fill exercise.

- **`FakerAdapter.generate_batch` seeding** (`seed_instance` per-locale, not the module-global
  `Faker.seed`) is the correct pattern. Using `int.from_bytes(spec.seed, "big")` to derive a
  numeric seed from the engine's determinism bytes is idiomatic.

- **`ProviderRegistry.override`** returning a new instance rather than mutating preserves
  immutability. Tests getting a clean registry via direct `ProviderRegistry({...})` construction
  (instead of `_reset_default_registry_for_tests`) is the right direction.

- **`sdk.py` capability-flag constants** (`CAP_STREAMING`, `CAP_RESUMABLE`, etc.) as named string
  constants rather than inlined literals prevents typo-based silent mismatches in third-party
  connectors.
