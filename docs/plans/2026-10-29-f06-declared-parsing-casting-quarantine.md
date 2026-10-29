Status: plan

Summary: Add one engine-owned, versioned declared-casting contract; parse before masking and format after masking through one route-independent boundary; make null substitutions countable; and admit row quarantine only through a protected, retention-qualified sink.

Baseline: `plan/f06-declared-parsing` at `fdd08a894d5dd4440a1adea0ee62bf0b670808bb`, equal to `origin/main` when this plan was authored (`F29: machine-readable errors & progress (#336)`).

# F06: declared parsing, casting, and quarantine

This is the R2 FRAME + PLAN for the Gemini -> Opus-4.8 Dennis -> owner plan gate. It is not implementation approval and does not claim that F06 or its consumer surfaces have shipped. The supplied MASTER contract is authoritative. No `ROADMAP.md` exists in this engine checkout, and no local G7 source document was found; those labels are therefore requirements supplied with the task, not invented repository prerequisites.

Risk: **R2**. The change alters persisted configuration and plan schemas, value types seen by masking strategies, route admission, error disposition, evidence counts, and handling of raw quarantined rows. It must preserve old configs byte-for-byte where their new fields are unset and must fail before reading data when a requested capability cannot be honored.

Rules consulted: `/home/cam/dev-rules/00-universal.md`, `risk-and-exceptions.md`, `development-loop.md`, `autonomous-operation.md`, `workspace-and-vcs.md`, `reuse-first.md`, `exploration.md`, `scope-discipline.md`, `verification.md`, `feature-dev.md`, `architecture.md`, `testing.md`, `documentation.md`, `api-and-compatibility.md`, `data-and-migrations.md`, `observability-and-resilience.md`, `security.md`, `config-secrets-and-supply-chain.md`, and `code-review.md`; repository `README.md`, `CODEMAP.md`, `CONTRIBUTING.md`, `CLAUDE.md`, and `SECURITY.md`.

Grounding: all repository anchors below were opened at the pinned baseline. Proposed files and symbols are labeled **new**. qmd recall was run before source inspection; it surfaced the fixed-width plans and code map but no F06/G7 record. A narrow Graphify query over `src/decoy_engine/profile` confirmed the fixed-width reader/writer choke points. External standards and library documentation were surveyed before the format decisions. No application tests were run for this plan-only change.

## Established solutions surveyed first

1. Python's `decimal` module represents base-10 values exactly, exposes signals/traps, and documents `quantize` with an `Inexact` trap as fixed-place validation. F06 uses `Decimal` constructed from validated text and traps precision/scale loss; it does not use binary float. [Python `decimal`](https://docs.python.org/3/library/decimal.html).
2. Python's `datetime` provides `date.fromisoformat`, `strptime`, and `strftime`, but its documentation notes that supported `strptime` codes vary by platform and some directives are locale-dependent. F06 uses the standard library under the restricted numeric grammar below, not pandas/dateutil inference. [Python `datetime`](https://docs.python.org/3/library/datetime.html#strftime-and-strptime-behavior).
3. Unicode TR35 documents that decimal/grouping symbols and currency presentation vary by locale and that symbols are not sufficient currency identity. V1 makes every separator/token/code explicit and defers CLDR locale profiles. [Unicode TR35 Numbers](https://unicode.org/reports/tr35/tr35-numbers.html).
4. PyArrow's CSV `ConvertOptions` demonstrates explicit column types, null token lists, decimal points, and timestamp parsers. F06 adopts explicitness but keeps parsing in the engine boundary because not every route is Arrow CSV and route parity cannot depend on reader-specific conversion. [PyArrow `ConvertOptions`](https://arrow.apache.org/docs/python/generated/pyarrow.csv.ConvertOptions.html).
5. IBM documents the ASCII overpunch characters, zoned-decimal zone nibbles, and packed-decimal digit/sign nibbles. The strict V1 maps selected below follow those conventions and deliberately canonicalize output. [IBM overpunch](https://www.ibm.com/docs/en/epfz/5.3.0?topic=characters-overpunch), [IBM zoned decimal](https://www.ibm.com/docs/en/iis/11.5.0?topic=listing-zoned), [IBM packed decimal](https://www.ibm.com/docs/en/ste/11.0.2?topic=functions-pack).
6. Exact null-token lists are established in PyArrow and pandas readers, but relying on reader defaults would create route-dependent behavior. F06 owns exact sentinel matching and counts after framing.
7. Decoy's vault already uses bounded, length-prefixed Fernet tokens rather than a full plaintext temporary artifact (`src/decoy_engine/vault.py:250-257`, `:276-305`), and its `KeyProvider` keeps raw secrets out of plans/logs (`src/decoy_engine/keyprovider.py:20-25`, `:103-121`). The local quarantine profile reuses that established cryptographic pattern with a distinct HKDF purpose; it does not invent a cipher or reuse the vault file format.

The repository already depends on pandas, PyArrow, Pydantic, pinned Faker, and `cryptography` (`pyproject.toml:55-80`, `:146-157`), with Hypothesis in test dependencies (`:170`). F06 adds no parsing dependency. It uses `decimal`, `datetime`, `codecs` only where a declared EBCDIC decoding profile needs it, existing PyArrow/Pandas carriers, Pydantic discriminated schemas, and existing F29 reasons. The COBOL nibble codecs are small engine-owned standard mappings because Python has no standard overpunch/COMP-3 parser; they are table-driven from the cited convention and exhaustively tested, not a general copybook parser. `locale`, float conversion, pandas numeric/date inference, and an unpinned new package are prohibited in this boundary.

## FRAME

### Problem

Users need string- or byte-encoded values converted to declared logical values before masking, then rendered to a declared destination representation. Today that behavior is not a column contract shared by routes:

- The fixed-width reader is the only input path with declared casting. Its layout supports only `str`, `int`, and `float` (`src/decoy_engine/config/_fixed_width.py:62-82`). It slices declared fields and calls `_cast_value` (`src/decoy_engine/profile/_fixed_width_reader.py:222-264`), refusing short rows and bad casts rather than coercing them (`:248-262`).
- Text-source framing is not yet lexeme-preserving enough for F06. Local CSV profiling uses `pandas.read_csv` with inference (`src/decoy_engine/profile/_readers_csv.py:54-81`), cloud CSV readers do the same (`src/decoy_engine/profile/_cloud_readers.py:136-140`, `:284`, `:391`), and the bounded Arrow reader deliberately maps pandas' default NA tokens to null (`src/decoy_engine/io/bounded_reader.py:36`, `:275-280`). Those behaviors can erase spelling or turn a sentinel into an indistinguishable source null before F06 can count it.
- The fixed-width writer already demonstrates the required output posture: it refuses width overflow at `src/decoy_engine/profile/_fixed_width_writer.py:128-138` and reuses the reader decode as a round-trip oracle at `:149-160`. That pattern must be generalized; the legacy `float` type is not the representation for new decimal/currency contracts.
- `ColumnConfig` currently has no logical parse/output contract. Its `dtype` at `src/decoy_engine/config/_tables.py:109-124` is a coarse declaration used by a chunked FK gate, not parsing semantics.
- Compilation is a deterministic, frozen contract (`src/decoy_engine/plan/_types.py:1-13`), but `Plan` has no casting payload (`:282-311`) and `PLAN_VERSION` is 2 (`src/decoy_engine/plan/_compile.py:102-106`).
- Routing is not governed by one universal compiler. `build_run_context` profiles and then compiles before route selection (`src/decoy_engine/execution/_pipeline_context.py:278-290`); chunked routing consults `check_chunked_compatibility` (`src/decoy_engine/execution/_planner.py:316-369`), while out-of-core has its own authoritative compatibility result (`src/decoy_engine/execution/out_of_core/_compat.py:130-177`). The physical compiler is not a universal route gate. F06 therefore needs one capability decision invoked by every live admission surface, not another disconnected allowlist.
- Per-row failure handling has fail-loud and row quarantine, not the complete MASTER triad. `finalize_validators_and_quarantine` raises for uncovered failures and quarantines covered ones (`src/decoy_engine/execution/_pipeline_finalize.py:404-463`); `RowErrorsFailedError` explicitly describes only fail or quarantine (`src/decoy_engine/errors.py:395-405`). A generic F06 field-null policy does **not** exist at this baseline.
- Existing quarantine is row-level JSONL to a configured path. It writes original row values plus free-text `_quarantine_reason` (`src/decoy_engine/quarantine.py:16-37`, `:171-192`) and refuses a missing path (`:247-267`). Its config has `enabled`, `output_path`, and triggers (`src/decoy_engine/config/_validators.py:78-143`), but no explicit access policy, finite retention, or protected-store capability. It is useful machinery, but it is not yet MASTER's protected quarantine.
- Counts already have an appropriate evidence carrier. `ExecutionResult.quality_metrics` and `row_errors` are additive fields (`src/decoy_engine/execution/_adapter.py:42-71`); finalization emits per-table/column/trigger counts without cell values (`src/decoy_engine/execution/_pipeline_finalize.py:394-436`); `RowErrorSummary` enforces `total_observed == covered + uncovered` and bounded samples (`src/decoy_engine/execution/_row_error_summary.py:55-87`). F06 must extend this carrier instead of creating a second report channel.
- F29 is present at this exact baseline. The registry versions every stable reason vocabulary (`src/decoy_engine/reasons/_registry.py:24-29`), declares each identity once (`:38-104`), and exports a deterministic manifest (`:148-180`). `ReasonRecord` excludes exceptions and free text and permits only closed, bounded context (`src/decoy_engine/reasons/_record.py:1-16`, `:114-169`). The sentry rejects ad-hoc reason codes (`tests/sentry/test_reason_registry.py:1-20`, `:158-163`) and the hostile boundary tests prove raw values/paths do not escape (`tests/security/test_reason_boundary.py:44-114`). F06 issues must use this boundary.

The root defect is duplicated or inferred interpretation. If parsing lives in readers, strategy handlers, pandas conversions, and route-specific kernels, the same config can mean different values on different routes. If output formatting is a target-only concern, width and precision become late failures after masking. If a parse failure is represented only as free text, null and quarantine cannot be counted safely and consistently.

### Guarantee

For every configured F06 column admitted for a route, the engine applies the exact versioned lexical/byte parser before the masking strategy, applies the declared error disposition exactly once, and applies the exact versioned formatter after masking. The following are testable invariants:

1. A logical value and its `Decimal` precision/scale or date domain are identical on every admitted route and batch partition.
2. No parser or formatter silently truncates, rounds, changes scale, overflows a declared width/precision, infers a locale, or guesses a format. Unknown or incomplete behavior is rejected before source data is read.
3. Sentinel-to-null is an explicit pre-parse lexical mapping. Source nulls and sentinel substitutions are distinguishable in evidence; every substitution increments a bounded table/column/reason count.
4. `fail`, `null`, and `quarantine` are exhaustive dispositions for parse/format failures. `fail` aborts without publishing; `null` replaces only the field and keeps the row; `quarantine` removes the entire row from normal output and writes it only through a sink that proves protected access and finite retention.
5. Every externally visible issue is an F29 registry identity plus closed `ReasonContext`. Values, parser exception text, paths, connection strings, and raw rows never enter logs, progress, ordinary evidence manifests, or ordinary exception messages. Declared sentinel literals necessarily remain in the privileged config/Plan contract so the rule is reproducible, but never enter diagnostic/evidence projections.
6. Formatting succeeds only if the emitted representation parses back to the exact logical value under the same contract. A declared fixed width includes the sign, currency token, separators, and padding. An unprovable or variable output width blocks the route/config rather than truncating.
7. Validation of configuration, plans, and route capability never mutates user data. Runtime transformation occurs only at the casting boundary and returns new arrays/tables plus typed issue/count records.

### Definition of done

F06 engine core is done when:

- additive config JSON schema, frozen Plan payload, deterministic serialization, and capability manifest describe the V1 catalog below;
- all supported execution routes call the same cast/format kernel and pass the parity acceptance suite, while unsupported combinations produce registered pre-read declines;
- the RED tests below pass without weakening assertions, replacing real boundaries with mocks, or silently skipping routes;
- fixed-width `042` reaches an integer strategy as `42` and is rendered back as exactly three characters; decimal/date/currency/COBOL vectors round-trip without loss;
- sentinel and parse-null substitutions appear in `quality_metrics` with exact counts, separate causes, and no source value;
- protected row quarantine has mandatory access/retention declarations, real local filesystem enforcement for the reference profile, opaque safe references, purge/read tests with an injected clock, and no permissive legacy-path fallback;
- F29 catalog fixtures, reason adapters, hostile-output tests, route sentries, compatibility fixtures, documentation, and release notes are updated;
- old configs with no F06 fields retain identical validated config dumps/hashes, route decisions, outputs, and fixed-width behavior; the intentional Plan schema-version change is fixture-pinned and documented rather than disguised as byte identity;
- engine completion is reported separately from CLI explanations and Studio controls. The engine exports the schema/capability/reason contract those consumers need; it does not claim their work complete.

### Boundaries and non-goals

V1 includes exact strings, strict integers, exact decimal/fixed-point, strict dates, explicitly configured currency, ASCII signed overpunch, EBCDIC zoned decimal, COMP-3 packed decimal, exact sentinel tokens, three error policies, exact output formats, and protected row quarantine.

V1 does **not** include ambient locale parsing; Unicode/CLDR currency inference; natural-language dates; month/day names; automatic format detection; time, datetime, or timezone semantics; exponent notation; arbitrary grouping patterns; rounding modes; permissive COBOL sign nibbles; copybook import; multi-line records; automatic encoding detection; field-level quarantine; whole-row nulling; quarantined-value display in ordinary evidence; an authentication service; a retention daemon; or support for every physical route merely because the logical format exists. These are explicit later catalog/profile additions.

Existing `date_shift.provider_config.date_format` remains a strategy parameter until a separately gated migration. F06's column parser becomes authoritative for F06 columns; the compiler rejects conflicting duplicate declarations instead of choosing one. Existing fixed-width `type: float` remains for compatibility, but new `logical_type: decimal|currency` never converts through float.

The engine is an in-process library with caller privileges (`AGENTS.md` scope). OS owner-only modes protect the local reference store from other OS principals under the declared deployment policy, not from arbitrary code running as the same principal. Platform authorization is supplied through the protected-sink protocol. The engine must not manufacture an `authorized=True` boolean and call that access control.

### Phase-1 pillar alignment

| Pillar | F06 construction |
|---|---|
| One sanitizing boundary | **New** `declared_casting` runtime owns sentinel match, parse, failure disposition, and format. Readers frame bytes/fields; strategies receive typed values; writers publish already formatted values. No route-specific parser is authoritative. |
| Validation never mutates | Pydantic/compile/route checks produce frozen contract and rejection records only. The boundary returns new Arrow arrays/tables and typed effects. |
| Bounded | Work is per batch and per column; issue samples use existing bounds; counts are aggregated integers. No whole-column inference or unbounded bad-value collection is allowed. Quarantine writes stream through a bounded sink rather than accumulating all rows. |
| Machine-readable/versioned | Contract schema, format IDs, plan payload, capability manifest, count schema, quarantine envelope, and all reason identities are versioned. |
| Fail closed | Unknown ID/directive, missing precision/scale/width, ambiguous token/separator, unsupported physical type, unqualified quarantine sink, and unsupported route all reject before data read. Runtime values outside the declared domain follow exactly the selected policy. |
| Engine owns cross-surface contract | Engine owns types, catalog metadata, JSON schema, reason codes, capability explanations, and evidence. CLI/Studio render and author them; they do not reimplement semantics. |

### FRAME decisions

| Decision | V1 answer | Consequence |
|---|---|---|
| Contract shape | Per mask column: `logical_type`, `parse_format`, optional `sentinel_to_null`, `error_policy`, and optional `output_format`. The fields form one discriminated `DeclaredColumnContractV1`, not independent dictionaries. | Invalid combinations fail at config validation/compile. Unset F06 fields are omitted to preserve legacy dumps/hashes. |
| String | `string/exact/v1`: framed text is retained exactly after the physical format's structural unescaping and, for fixed-width only, declared pad removal. No trim, Unicode normalization, encoding guess, or case fold. Optional output `string/exact/v1` may declare total width/pad/alignment for a fixed-width target. | Provides an explicit sentinel-capable passthrough type without allowing reader inference. Width overflow is an error. |
| Integer | `integer/ascii/v1`: ASCII digits with one optional leading `+` or `-`; no whitespace, exponent, grouping, or Unicode digits. V1 has explicit/defaulted `signed: true` and `bit_width: 64`, with allowed widths 8/16/32/64; those defaults are part of `/v1`, not host inference. Output is `integer/ascii/v1` or `integer/zero-pad/v1`, the latter requiring total width. | `042` parses to `42`; zero-pad width 3 emits `042`. Sign consumes width. Values outside the declared integer range fail rather than wrap or widen by route. |
| Decimal | `decimal/plain/v1` requires positive `precision` and non-negative `scale <= precision`; `.` is the radix, no grouping/exponent. `decimal/grouped/v1` additionally requires distinct one-character ASCII radix/group separators and Western 3-digit grouping. Output uses the same numeric rules plus explicit width/pad/alignment when fixed width is required. | Parse from string to `Decimal`; quantize only as an equality validator with `Inexact`/`Rounded`/`InvalidOperation` traps. Never round or pass through float. |
| Date | `date/iso8601/v1` is exactly `YYYY-MM-DD`. `date/strptime/v1` requires an explicit, fully consuming format drawn from `%Y %m %d` plus literal separators; V1 requires all three fields, fixed-width directives, and forbids `%b`, `%B`, `%c`, `%x`, `%X`, `%Z`, platform extensions, and separator-free ambiguity. Outputs are `date/iso8601/v1` or `date/strftime/v1` under the same restricted grammar. | Uses `datetime.date`, a lexical shape check, and explicit `strptime`/`strftime`. No host locale and no format inference. Output width is statically derivable. |
| Currency | `currency/explicit/v1` requires an ISO 4217-style three-uppercase-ASCII-letter semantic `currency_code`, an exact non-empty literal token bounded to 32 UTF-8 bytes, token position (`prefix` or `suffix`), one fixed sign style (`leading_minus` or `accounting_parentheses`), precision/scale, explicit radix, and optional explicit Western grouping separator. Whitespace, if required, is literal contract text. Output repeats those choices and requires width for fixed-width targets. | A symbol never implies a currency. `$` paired with `USD` is valid; bare `$` with no code is not. Amount is an exact `Decimal`; currency code remains plan metadata. No locale module or Babel dependency. |
| ASCII overpunch | `cobol/overpunch-ascii-trailing/v1`, text input/output, trailing sign only. Positive final characters are `{ABCDEFGHI` for digits 0-9; negative are `}JKLMNOPQR`. Precision/scale are mandatory. | Strict known mapping; ordinary final digit is rejected rather than guessed as unsigned. Signed zero is preserved. |
| EBCDIC zoned decimal | `cobol/zoned-ebcdic-trailing/v1`, binary input/output. Every digit nibble must be 0-9. With `signed: true`, V1 accepts IBM positive zones A/C/E and negative B/D; with `signed: false`, it accepts F only. Output canonicalizes signed positive to C, negative to D, unsigned to F. Precision/scale and exact byte length are mandatory. | This is a byte format; a UTF-8 text reader cannot claim it. Route capability includes binary-field support. |
| COMP-3 | `cobol/comp-3/v1`, binary input/output. Two digits per byte except the final low sign nibble; only C positive, D negative, and F unsigned (only when `signed: false`) are admitted. Unused leading nibble for even precision must be zero. Precision/scale determine exact width as `ceil((precision + 1) / 2)` bytes. Output is canonical C/D/F. | No permissive nibble guessing and no copybook inference. Only routes carrying exact bytes may admit it. |
| Sentinel | `sentinel/exact/v1`: a non-empty, ordered-unique list of at most 256 exact lexical text tokens or exact byte tokens, each at most 1,024 encoded bytes, matching the physical input kind. Match after transport framing and declared fixed-width pad removal, before parsing. No trim, case fold, normalization, glob, regex, or locale behavior. Empty is a sentinel only if explicitly listed. | Sentinel match wins over parse. Existing physical null remains null and does not increment substitution counts; each sentinel replacement does. Tokens never appear in safe output. |
| Width unit | Every fixed-width text output declares `width_unit: code_points | bytes`; binary COBOL is always bytes. The current fixed-width writer is a code-point implementation, so it admits only `code_points` until a byte-framed writer exists. Padding is exactly one unit in the selected domain. | A Unicode token such as `€` cannot be mistaken for one byte. A target whose unit differs rejects at route admission; no conversion silently changes offsets. |
| Failure scope | `fail` aborts; `null` is **field-level** and preserves the row; `quarantine` is **row-level** and removes the whole row. If several fields fail, all field issues count, but the row is written once with a bounded set of reason identities. | No field-level quarantine in V1. This reuses existing row-dedup semantics while making reason metadata structured. |
| Quarantine protection | F06 `quarantine` is admitted only with a `ProtectedQuarantineSink` capability carrying an access-policy ID, finite retention deadline, bounded write budget, opaque storage reference, streaming write/seal/abort, authorized open, and purge-expired operations. The local reference profile uses per-record/chunk Fernet encryption under a real domain-separated `KeyProvider` secret, private 0700 directories and 0600 files, no symlink following, atomic seal, and injected clock. | Existing `quarantine.output_path` is not silently promoted. It remains compatible for existing triggers, but cannot satisfy F06's protected policy. Missing sink or real encryption key rejects before source read. Platform supplies identity/auth and durable lifecycle adapters. |

## Proposed design

### 1. One versioned engine contract

Create **new** `src/decoy_engine/contracts/declared_casting.py` as the small public contract module and the private runtime package mapped below. The public module owns immutable/discriminated types:

- `DeclaredColumnContractV1`
- `LogicalType = string | integer | decimal | date | currency`
- parse/output format variants named by the exact `/v1` IDs above
- `ExactSentinelSetV1`
- `CastErrorPolicy = fail | null | quarantine`
- frozen plan equivalents and canonical JSON conversion
- `FormatCapability` metadata: physical kind, logical Arrow/Python type, fixed-width derivation, input/output support, and catalog version

Use this exact responsibility map; split further only to satisfy the repository's module-size gate, never to duplicate policy:

| Proposed file | Single responsibility |
|---|---|
| **new** `src/decoy_engine/contracts/declared_casting.py` | public versioned Pydantic/frozen contract types and canonical JSON forms; leaf with no execution imports |
| **new** `src/decoy_engine/config/_declared_casting.py` | additive config adapters and top-level protected-quarantine policy validation |
| **new** `src/decoy_engine/plan/_checks_declared_casting.py` | pure pre-profile contract compile, strategy/type compatibility, and Plan stamping input |
| **new** `src/decoy_engine/declared_casting/_catalog.py` | the only format descriptor registry and exported capability manifest |
| **new** `src/decoy_engine/declared_casting/_numeric.py` | strict integer, Decimal, and currency parse/format kernels |
| **new** `src/decoy_engine/declared_casting/_date.py` | restricted date grammar and parse/format kernels |
| **new** `src/decoy_engine/declared_casting/_cobol.py` | overpunch, zoned, and COMP-3 table-driven codecs |
| **new** `src/decoy_engine/declared_casting/_boundary.py` | typed pre-mask/post-mask batch boundary and disposition reducer |
| **new** `src/decoy_engine/declared_casting/_counts.py` | count algebra, completeness, and safe `quality_metrics` projection |
| **new** `src/decoy_engine/declared_casting/_eligibility.py` | central route/target/source capability decision |
| **new** `src/decoy_engine/declared_casting/_quarantine_protocol.py` | protected sink/session/receipt/handle protocols only |
| **new** `src/decoy_engine/declared_casting/_quarantine_local.py` | Fernet-encrypted POSIX reference adapter only |

Existing config/Plan/reader/route modules receive narrow calls into these owners. They do not grow local parser helpers.

Add optional F06 fields to `ColumnConfig` at `src/decoy_engine/config/_tables.py:55-127`, but have a model validator construct/validate the cohesive contract. Follow the existing omit-when-unset serializer pattern at `:154-174` so pre-F06 config dumps and hashes are unchanged. Do not put a free-form `dict[str, Any]` under `parse_format`.

Add a pure **new** `compile_declared_casting_contract(config)` preflight and call it in `build_run_context` before `profile_source`; this corrects the current profile-before-compile order at `src/decoy_engine/execution/_pipeline_context.py:278-290` for config-only F06 failures. Full `compile_plan` receives/recomputes the same deterministic result and stamps it only after equality is established, so there are not two validators. Add a frozen, sorted `declared_casting` payload to `Plan`, bump `PLAN_VERSION` from 2 to 3, and update both directions of `src/decoy_engine/plan/_serialize.py`. A version-2 plan can deserialize with no declared contracts for diagnostics/legacy execution; a plan claiming an unknown format or V3 casting payload never executes. Config and Plan round trips pin omission, ordering, and unknown-field rejection. The version bump is the one intentional Plan-YAML difference for legacy configs and is covered by an explicit migration fixture.

`dtype` remains a legacy route hint. When F06 is set, the compiler derives its logical family and rejects an explicit conflicting `dtype`; runtime code never chooses parsing behavior from `dtype`. Generation-only columns are outside F06 unless a later contract explicitly adds source parsing to generation.

The config surface is concrete. An age column is authored as:

```yaml
- name: age
  strategy: bucketize
  provider_config: {width: 10, format: lower}
  logical_type: integer
  parse_format: {id: integer/ascii/v1, signed: true, bit_width: 64}
  sentinel_to_null: {id: sentinel/exact/v1, values: [""]}
  error_policy: fail
  output_format:
    id: integer/zero-pad/v1
    width: 3
    width_unit: code_points
    pad: "0"
    align: right
```

A `TBD` date that must quarantine the row is authored without making `TBD` a sentinel:

```yaml
- name: start_date
  strategy: date_shift
  logical_type: date
  parse_format: {id: date/iso8601/v1}
  error_policy: quarantine
  output_format: {id: date/iso8601/v1, width: 10, width_unit: code_points}
```

Any column using `error_policy: quarantine` also requires one top-level block:

```yaml
protected_quarantine:
  schema_version: 1
  access_policy: tenant-job-owner
  retention_seconds: 604800
  max_rows: 100000
  max_bytes: 1073741824
  required_protection: encrypted
```

`access_policy` is an approved identifier, retention is 1 through 31,536,000 seconds, and both budgets are positive bounded integers. There is deliberately no raw output path or key in config. Add one typed public/runtime argument, `quarantine_runtime: ProtectedQuarantineRuntime | None`, instead of sink/policy/key callback argument sprawl. It carries the resolved sink adapter and authorization owner; the existing resolved real `KeyProvider` supplies domain-separated encryption. `PipelineRunContext` carries the resolved runtime. A local run returns a `QuarantineHandle` on a new `ExecutionResult.quarantine_handle` field declared `repr=False`; the handle contains the process-private read capability and safe receipt, is never serialized into evidence, and is absent when no rows were quarantined. `quality_metrics` contains only the safe receipt/count projection. Isolated/platform adapters must replace this process-local handle with their own authorized opaque handle or declare the capability unsupported before execution.

### 2. Format catalog and fixed semantics

Create one closed `FormatDescriptor` registry keyed by `(format_id, direction)`. A descriptor owns schema validation, physical kind, parser/formatter functions, statically known width or a typed `unknown_width` result, logical representation, and route requirements. It is separate from the masking operator registry because format capability is orthogonal to strategy capability.

Catalog registration is the only way a format becomes usable. Unknown IDs never fall through to an `if/else` default. Export a deterministic capability manifest for CLI/Studio. Retired IDs stay readable as tombstones according to the compatibility policy; they are never silently rebound to new semantics.

Decimal/currency parsing first validates ASCII numeric grammar and grouping, then constructs `Decimal` from the string. Every arithmetic/quantize operation runs inside an engine-created `decimal.localcontext` with pinned precision, exponent bounds, rounding setting used only for exactness checks, and traps; caller/thread global decimal context cannot alter results. Precision counts coefficient digits under the declared scale; signed zero is retained. Inputs requiring rounding or exceeding precision fail. Formatting constructs the canonical unpadded token, checks precision/scale and exact round-trip, then applies declared padding only if the final declared unit width fits.

Date parsing first enforces exact lexical width/directive grammar, then calls the standard library. It does not inspect process locale. ISO means exactly ten characters and `YYYY-MM-DD`, not every broader input accepted by a Python version's `fromisoformat`.

COBOL parsing receives `str` for ASCII overpunch and `bytes` for zoned/COMP-3. It validates every nibble/character before constructing the digit string and `Decimal`. Binary formats never decode through UTF-8 or transcode through pandas objects that lose byte identity.

### 3. Lexeme-preserving framing, then one casting boundary on every route

Readers still own physical framing—CSV quoting/escaping and delimiters, fixed-width slices/pad removal, Parquet typed/binary cells—but an F06 column must reach the cast boundary as an exact `string` or `binary` value. Add contract-aware conversion options to local/cloud/bounded CSV paths so declared columns disable type inference and default NA conversion. Preserve legacy reader behavior for columns/configs without F06. Refactor fixed-width F06 columns to slice/pad-remove without the legacy `int`/`float` cast, then feed the shared boundary; legacy layouts without F06 still use `_cast_value`. A caller-supplied `pa.Table` is admitted only when its physical type matches the declared input kind; the engine cannot reconstruct a lexeme a caller already converted.

Profiling does not execute a sample-only `null` or `quarantine` disposition: that would bias statistics and make counts depend on the sample. The profile records physical schema, and the compiler overlays the declared logical type. A value-derived profile metric for an F06 column may be produced only when the bounded sample parses completely through the same parser; otherwise that metric is explicitly unavailable and any strategy that requires it must fail/fallback honestly. Runtime applies the configured policy to every row, and only runtime contributes substitution/quarantine evidence.

Create **new** typed runtime carriers rather than a long argument list:

- `CastingContext`: frozen Plan contracts, table/schema, batch identity and absolute row offset, protected-quarantine capability, and bounded issue sink.
- `CastBatchResult`: new typed columns, per-cause counts, typed field issues, and row quarantine selection.
- `DeclaredCastIssue`: table/column/row position, registered `ReasonCode`, internal stage (`parse` or `format`), and disposition; no raw token, exception, or free text. At the F29 boundary the occurrence uses an existing registry `Phase` such as `MASK` or `FINALIZE`; F06 does not invent unregistered phase strings.
- `DeclaredCastCounts`: schema/version, by table/column/cause/disposition, totals, and completeness.

The pre-mask boundary runs after a reader has framed rows/fields and before any strategy, relationship mapping, or `when` evaluation that consumes that column. Sentinel matching is first. The post-mask boundary runs after strategy/relationship work and before output projection/publication. Strategies see exact `str`, `int`, `Decimal`, or `date`, never a numeric/date source spelling. When `output_format` is absent, a target may receive the native typed logical value only if its capability says that type is lossless; a text/fixed-width target must have an explicit output format. Output format is not applied to unconfigured passthrough columns.

Consolidate the existing operator capability facts with declared logical input/output signatures. `OperatorSpec` is already the shared native-operator descriptor (`src/decoy_engine/execution/_operator_registry.py:1-11`, `:59-90`), while `masked_output_type` is the current streaming output resolver (`src/decoy_engine/execution/out_of_core/_mask.py:245-267`); the correctness checks already reuse it but explicitly identify bucketize as untyped (`src/decoy_engine/plan/_checks_correctness.py:16-30`). F06 must extend or wrap these existing sources into one resolver rather than add another contradictory table. The compiler checks the selected strategy shape against the parser output and formatter input. For the MASTER example, F06 `bucketize` with `format: lower` and integral width returns an integer on the F06 path, so `integer/zero-pad/v1` receives `42`; legacy bucketize remains string-producing. `bucketize format: range` produces a string and is rejected with an integer output formatter. Dynamic or untyped strategy output cannot be sent to a typed formatter until its capability is proven. This prevents a second implicit parse after masking.

All relationship endpoints are cast before equality/mapping. The compiler requires compatible logical types and canonical equality domains; lexical formats may differ (`042` and `42` can join as integer 42), but signedness/scale/currency-code mismatches fail. Route adapters that join or stage keys before the shared boundary are ineligible until reordered and proven by the FK parity tests.

Both boundaries operate on Arrow arrays/batches and expose a pandas adapter using the same scalar kernels; they do not call pandas inference. A route can vectorize later only if the vectorized implementation passes the same golden/property suite and remains behind the same descriptor. Batch row offsets keep issue attribution stable across partition sizes.

The boundary processes every declared column in deterministic config order and collects all field issues for a row before disposition. `fail` records the first deterministic registered failure and aborts the batch/run; `null` writes a typed null only into that field; `quarantine` marks the row, continues enough columns to count its field failures, removes it from masking/main output, and streams the original framed row only to the protected sink. A row with both `null` and `quarantine` field dispositions is quarantined; all substitutions/failures still count.

### 4. Compiler and route eligibility choke point

Add **new** `evaluate_declared_casting(route, plan_contracts, source_facts, target_facts, capabilities) -> CastRouteDecision`. It returns registered, value-free rejection identities and capability requirements. It never reads source values.

The config-only preflight/compiler validates format existence, directive grammar, required precision/scale, static output width, sentinel physical type, `dtype` consistency, and records any protected-sink requirement. Route admission checks the actual runtime sink capability, physical input kind, Arrow logical type support, batch-local issue/count support, pre/post boundary availability, and protected-quarantine support. The hook must be called by:

- normal/full-frame route construction;
- `_planner` chunked/auto-chunk decisions;
- sequential admission and runner preflight;
- `check_out_of_core_compatibility`;
- mixed route admission where mask columns are present;
- unified/physical compilation when enabled;
- explicit forced-route entry points.

Automatic routing may fall back to another eligible route. An explicitly requested unsupported route returns a registered route refusal. A malformed/unknown contract or unknown output width is a compile error for every route and is detected before profiling/source read where config alone is sufficient. This distinguishes invalid semantics from a merely incapable route.

Do not copy format allowlists into these routes. The central descriptor and evaluator are authoritative; routes declare capabilities, not format names. A sentry inventories every production route owner and fails if a new route does not call the evaluator.

### 5. Counts and F29-safe failure boundary

Extend `quality_metrics` with `declared_casting`:

```yaml
schema_version: 1
completeness: complete
total_substitutions: 3
total_failures: 2
by_table:
  people:
    by_column:
      start_date:
        sentinel_to_null: 2
        parse_null: 1
        parse_quarantine: 1
        output_quarantine: 1
```

Keys are closed enums, names pass F29's safe name approval, counts are non-negative/bounded in dimension, and no sample contains a value. Source nulls are not substitutions. `total_substitutions` equals sentinel plus error-policy null substitutions; quarantined fields are failures but not substitutions. Completeness is `complete`, `partial_before_abort`, or `unavailable`, matching the existing row-error reporting posture.

Register these exact F29 identities; their summaries remain fixed, value-free sentences and their symbols are the registry's uppercase forms:

| Namespace | Wire code | Boundary |
|---|---|---|
| `plan` | `declared_cast_contract_invalid` | inconsistent logical/parse/output/sentinel shape |
| `plan` | `declared_cast_format_unknown` | unknown or retired format ID |
| `plan` | `declared_cast_directive_unsupported` | date/grouping/sign/width-unit directive outside V1 |
| `plan` | `declared_output_width_unknown` | output width cannot be proved before execution |
| `plan` | `declared_type_strategy_incompatible` | parser output, strategy input/output, or formatter input mismatch |
| `route` | `declared_cast_route_unsupported` | route lacks a required cast/format/physical-kind capability |
| `route` | `protected_quarantine_unavailable` | selected policy lacks a qualified sink/key/retention capability |
| `runtime` | `declared_integer_invalid` / `declared_integer_overflow` | integer grammar or declared range failure |
| `runtime` | `declared_decimal_invalid` / `declared_decimal_precision_loss` / `declared_decimal_overflow` | decimal grammar, scale, or precision failure |
| `runtime` | `declared_date_invalid` | lexical or calendar failure |
| `runtime` | `declared_currency_invalid` | token, sign, grouping, code, or amount failure |
| `runtime` | `declared_cobol_invalid` | overpunch/zoned/packed digit, sign, length, or pad-nibble failure |
| `runtime` | `declared_output_width_overflow` | canonical rendered value exceeds declared width |
| `runtime` | `protected_quarantine_write_failed` / `protected_quarantine_seal_failed` | artifact write or seal failure |
| `runtime` | `protected_quarantine_access_denied` / `protected_quarantine_expired` | open/purge denied or artifact past finite retention |

Bump the registry minor version and regenerate the deterministic compatibility fixture; never mutate an existing identity's meaning. No string literal reason codes are permitted outside the catalog declaration itself.

Replace F06 free-text row reasons with `ReasonCode`/`ReasonRecord`. The protected record contains registry and schema versions, safe table/column identity, row key/position metadata, and the full raw row payload because quarantine exists to isolate it; ordinary logs/evidence carry only opaque storage reference, counts, and registered codes. Existing legacy `_quarantine_reason` is not used for new F06 records. Any bridge into legacy `RowErrorRecord.reason` uses a fixed registry summary, never exception text or values, until that carrier is typed.

### 6. Protected row quarantine binding

Define **new** `ProtectedQuarantineSink` as a runtime-checkable protocol whose capability record is pinned before source read. Required operations are `begin(run, policy)`, bounded `write_row(envelope)`, `seal() -> QuarantineReceipt`, `abort()`, authorized `open(receipt, access_context)`, and `purge_expired(now, authority)`. The policy requires an access-policy identifier, finite `retention_seconds`, maximum bytes/rows, ownership/run identity, and storage protection level. Receipt carries only opaque approved refs, versions, counts, digest, created/expiry times, and protection capability—never a raw path or value.

The local reference implementation creates an engine-owned private arena with `O_NOFOLLOW`/exclusive creation where available, validates the parent and file types, uses 0700 directory and 0600 files, encrypts each bounded row/chunk as a length-prefixed Fernet token, fsyncs and atomically seals it, and refuses reads after expiry. Derive a quarantine-only Fernet key from a real `KeyProvider.mask_key()` with a new HKDF purpose/version; never use `SeedKeyProvider`, serialize the key, or write plaintext staging. Retention metadata and safe record framing remain outside ciphertext only when they contain no row values. `begin` returns a process-private `QuarantineReadCapability` separately from the safe receipt; it is generated with `secrets`, compared without logging, and never serialized into ordinary evidence. The API requires that capability, matching owner/run context, and a matching key version to open or purge. Tests exercise a real temp filesystem, wrong-capability/key denial, ciphertext tamper, absence of plaintext markers on disk, permission modes, symlink attacks, budget overflow, partial-write abort, digest verification, expiry, and purge with an injected clock. The OS owner with the key can still bypass this API, as the boundary section states. A platform adapter may declare an equivalent or stronger at-rest mechanism, but plaintext local quarantine is never an F06-qualified fallback.

The existing `compute_quarantine` row-dedup/filter logic can be extracted/reused, but not its all-rows-in-memory entry list or raw `output_path` writer. New F06 quarantine deduplicates `(table, absolute_row_position)` across batches, counts every field reason, streams each row once, and removes it from every row-aligned companion frame. A sink failure aborts the run and main publication; it never degrades to null or fail-without-quarantine.

F22, F25, and F26 are seams, not invented blockers:

- F22 exists here as a plan only (`docs/plans/2026-10-29-f22-atomic-idempotent-publication.md`). F06 exposes quarantine preparation/receipt so a future qualified publisher can bind required artifacts in one publication unit. Until then, do not claim atomic output+quarantine publication; fail-loud before normal output commit on quarantine failure.
- F25 is also a plan/in-flight design (`docs/plans/2026-10-29-f25-protected-scratch-quotas-orphan.md:19-29`); its `ResourceLedger` is explicitly new. F06 accepts a future managed arena capability but does not import a phantom class or claim filesystem encryption, quotas, or orphan recovery. F06's local artifact is application-level Fernet ciphertext; that narrower fact does not complete F25.
- No F26 implementation or plan was found in this checkout. F06 therefore owns mandatory finite expiry metadata and reference local purge/read enforcement, and exposes a lifecycle adapter. It does not add a background sweeper or claim platform-wide deletion schedules.
- F29 is merged and mandatory now. F31's shared safe-output policy is not present; `approve_ref` is explicitly its seam (`src/decoy_engine/reasons/_record.py:72-76`). F06 uses the current strict F29 boundary and is ready to adopt F31 later without a second redactor.

## RED-first acceptance suite

Slice 0 adds these tests before implementation. Each must fail on current main for the intended missing-contract/capability reason, then pass without weakening or mocking past the asserted boundary.

Put end-to-end cases A1-C6 in **new** `tests/acceptance/test_f06_declared_casting.py`, generated inverse/parity cases in **new** `tests/property/test_f06_declared_casting.py`, construction scanners/mutants in **new** `tests/sentry/test_f06_declared_casting.py`, and protected-store/leak cases in **new** `tests/security/test_f06_quarantine_boundary.py`. Focused format vectors live under **new** `tests/unit/declared_casting/`. Existing F29, fixed-width, route, and row-error test files are extended only where they already own the asserted regression boundary.

### A. Contract, cast, and round trip

1. **A1 fixed-width age:** a real fixed-width file with age `042`, `logical_type: integer`, and output `integer/zero-pad/v1 width: 3` reaches an instrumented real bucketize boundary as integer `42` and writes `042`. The test reads the written file back through the declared parser and compares typed values.
2. **A2 no truncation:** formatting `1000` to width 3 produces registered `declared_output_width_overflow`, publishes no target, and never emits `100`. Parameterize sign, token, grouping, and padding widths.
3. **A3 decimal precision:** `999.99` at precision 5/scale 2 succeeds; `1000.00` overflows; `1.234` refuses scale loss; `0.1` remains exact; a Python float input is refused for declared decimal rather than converted. Mutate the process/thread decimal context to hostile precision/rounding/traps and verify identical `Decimal` values and round-trip bytes.
4. **A4 dates are explicit:** ISO accepts only `2026-02-01`; `%m/%d/%Y` deterministically parses `01/02/2026`; the inverse declared format round-trips. Wrong shape, invalid calendar date, trailing content, and locale-dependent directives fail at config/parse as appropriate.
5. **A5 locale independence:** run the real parser/formatter while monkeypatching `locale.localeconv` and locale date names to hostile, contradictory values. Results remain byte-identical because the boundary never consults them. The construction sentry below also forbids importing `locale`; this adversarial environment test does not replace the real parser.
6. **A6 currency:** `$1,234.50` under declared USD/prefix/`.`/`,` and `1.234,50 EUR` under declared EUR/suffix/`,`/`.` produce exact amounts. `12,34.56`, a misplaced token, mismatched sign style, and `$` without a currency code fail with stable registered reasons.
7. **A7 COBOL vectors:** table-driven IBM vectors cover every ASCII positive/negative overpunch terminal; zoned positive/negative/unsigned bytes; COMP-3 odd/even precision and signed zero. Invalid digit/zone/sign/unused nibbles, wrong byte length, and precision overflow fail. Every accepted vector formats to canonical bytes and parses back exactly.
8. **A8 sentinel precedence:** date token `TBD` maps to null before date parse; empty maps only when declared; case variants do not match; physical null remains null. Evidence reports exact `sentinel_to_null` count and zero parse failures for matched tokens.

### B. Policies, route parity, and evidence

9. **B1 three policies:** the same bad date under `fail` aborts with no publish; under `null` leaves the row and substitutes only that field; under `quarantine` removes the entire row and stores it once. Counts distinguish parse-null from parse-quarantine.
10. **B2 multi-field row:** two failing fields on one row yield two field counts and one quarantined row with two registered reason identities. A second good row remains byte-identical. No field-level quarantine is observable.
11. **B3 route parity:** a shared golden corpus runs through every actually eligible full-frame, sequential, chunked, out-of-core, mixed/unified route harness. Values, Arrow schemas, formatted output, counts, dispositions, and reason identities match for batch sizes 1, 2, and larger than input. An FK case joins parent `042` to child `42` only after both become integer 42. A route lacking binary/error/quarantine/cast-before-join capability declines before reading rather than being forced through.
12. **B4 generated property parity:** Hypothesis generates valid and one-edit-invalid integer/decimal/date/currency/COBOL/sentinel representations. Parser/formatter inverse properties and admitted-route parity hold; invalid samples never become a different valid value.
13. **B5 unknown width/behavior:** missing zero-pad width, missing/mismatched width unit, an unbounded output directive, unknown format ID, or variable-width date directive fails the config-only F06 preflight before profiling or a poisoned `LazySource` can be read. A Unicode currency token is checked in both byte and code-point modes. A known contract on an incapable forced route yields the registered route rejection; automatic mode may select an eligible route.
14. **B6 count algebra:** across batch partitions, `total_substitutions` equals sentinel plus policy-null cells, failure totals equal disposition totals, quarantined row count is the distinct row set, and completeness changes to `partial_before_abort` on fail. Counts never contain sentinel/source values.
15. **B7 legacy compatibility:** configs without F06 fields have identical validated dumps/hashes, fixed-width reads/writes, route decisions, and outputs. Plan YAML changes only by the documented schema version and empty-payload omission pinned in the compatibility fixture. Existing `type: float` tests stay unchanged.

### C. Protected quarantine and safe output

16. **C1 capability required:** `error_policy: quarantine` without a qualified protected sink fails preflight before a poisoned source is opened. Legacy `quarantine.output_path` does not satisfy the capability.
17. **C2 real local protection:** on a real temp filesystem the arena/file modes are 0700/0600, symlink targets are rejected, disk bytes contain no hostile plaintext marker, wrong-key/tamper reads fail, partial files are not receipts, byte/row budgets fail closed, and sealed digest/counts verify. A seed-only/non-secret key provider is rejected before source read. Tests are conditional only on explicitly declared unsupported OS primitives; the POSIX CI profile must pass, not skip.
18. **C3 access/retention:** an unauthorized access context cannot open a receipt; authorized access before expiry can; at/after expiry reads fail with a registered reason; `purge_expired` removes only the exact owned sealed artifact and reports the result. Use an injected clock, not sleeps.
19. **C4 publication failure:** sink write/seal failure aborts normal output publication and leaves no readable partial quarantine. Cleanup failure is reported as a resource outcome without masking the original registered failure.
20. **C5 no leakage:** hostile values containing a unique marker are injected as invalid numbers/dates, currency values, COBOL bytes, parser exception text, filenames, and raw quarantined rows. Search captured logs, progress payloads, `ReasonRecord`, exceptions, `quality_metrics`, route explanations, and ordinary evidence manifests; the marker may occur only when an authorized test opens the protected quarantine artifact. A separately generated marker used as a configured sentinel is expected in the privileged config/Plan contract that must reproduce the rule, but is forbidden from all ordinary diagnostic/evidence channels.
21. **C6 F29 compatibility:** every new reason appears once in the versioned registry manifest, round-trips through the safe adapter, has correct phase/outcome/retry semantics, and triggers existing no-ad-hoc-code and hostile-boundary sentries.

## Defect-class choke points and SENTRY tests

| Defect class | Enforced choke point | Build-breaking SENTRY |
|---|---|---|
| Numeric truncation/precision loss | Catalog decimal parser/formatter and exact round-trip oracle | AST scan forbids `float`, `pd.to_numeric`, Arrow unsafe cast, unchecked `quantize`, slicing-to-width, or rounding in the casting package. Seeded mutants delete each width/precision/trap comparison and must be caught by A2/A3/property tests. |
| Locale-dependent parsing | Restricted date/currency grammar and descriptor registry | AST/import sentry forbids `locale`, dateutil/pandas inference, and locale directives in descriptors; hostile-locale A5 proves runtime independence. |
| Route semantic drift | `evaluate_declared_casting` plus shared kernel | Inventory sentry maps all production route owners to the central evaluator and all runtime adapters to the shared kernel. A seeded fake route without either fails. Capability manifest has one source, no copied format-name allowlists. |
| Unknown output behavior | Compile-time descriptor width derivation | Matrix sentry iterates every output descriptor/route/target and requires exact width or a registered pre-read rejection. Seeded `unknown -> allowed` mutant fails B5. |
| Silent issue drop/double disposition | One issue collector/disposition reducer | Algebra sentry requires every issue exactly once in fail/null/quarantine totals, substitution totals, and row dedup. Mutants that clear, duplicate, or reclassify one issue fail B6. |
| Output not inverse of input | Descriptor formatter invokes parser oracle | Property suite checks `parse(format(v)) == v`, including signed zero and boundaries. A spy/structural test proves each descriptor declares its inverse; none can opt out. |
| Raw-value/error leakage | F29 `ReasonRecord` and protected-sink boundary | Extend `tests/security/test_reason_boundary.py` and log interpolation scan with hostile F06 carriers; no free-text reason field is allowed in new issue types. |
| Unprotected/immortal quarantine | Capability preflight and protected store protocol | Protocol conformance tests require access policy, finite expiry, bounded budget, seal/abort, and purge. A fake sink missing any capability is rejected before source read. |
| Catalog drift | Closed `FormatDescriptor` registry | Snapshot schema/capability manifest; duplicate/unknown/removed IDs fail. Each registered descriptor must have vectors, inverse properties, width result, physical kind, and route golden coverage. |

## Implementation plan

Every slice uses the same loop: **self-check -> targeted verification -> Codex adversarial review -> remediation -> Opus-4.8 Dennis adversarial quality/security/QA review -> remediation**. No slice is called complete with BLOCKER/HIGH findings, unexplained skips, or weakened tests. Broader regression and Barry documentation occur before the exact-HEAD receipt.

### Slice 0 — engine contract and RED acceptance suite

Add the **new** contract package, discriminated config shapes, pure pre-profile F06 compiler, frozen Plan payload/codec design, format/capability manifest schema, count/quarantine protocol types, and F29 catalog entries/fixtures. Add A1-C6 and the sentry harnesses in RED form. Pin the intended config examples and explain output. Record why each test is RED on `fdd08a89` (normally extra-forbidden config or missing symbol), avoiding import-collection failure that hides later cases. Decide and fixture the Plan V3 compatibility behavior before implementation.

Self-check contract cohesion, unset-field omission, naming, and dependency status. Targeted verification runs config/schema/Plan/reason tests plus the RED suite and records the expected failures. Codex then Dennis review the contract and tests; remediate before writing parser logic.

### Slice 1 — strict format catalog and pure kernels

Implement integer, Decimal, restricted date, explicit currency, overpunch, zoned, COMP-3, sentinel matching, canonical formatting, and fixed-width derivation in single-responsibility modules behind the descriptors. No route or I/O changes. Reuse standard-library exact types and table-driven IBM maps. Return typed issues instead of raising from value-bearing frames; construct public exceptions/ReasonRecords only after raw locals are out of scope, following the fixed-width reader's safe pattern (`src/decoy_engine/profile/_fixed_width_reader.py:95-104`, `:205-211`).

Self-check grammar tables and edge arithmetic. Run A2-A8, property/mutation tests, catalog snapshot, static sentries, type/lint checks. Codex and Dennis adversarially inspect precision, signed zero, nibble validation, exception chains, and denial-of-service bounds; remediate.

### Slice 2 — compile stamping and universal route eligibility

Wire config -> frozen Plan V3 -> deterministic YAML. Implement the central evaluator and capability declarations, then call it from every live route/admission surface listed above. Reject invalid semantics before profiling/source read where possible, and incapable forced routes before execution. Export deterministic explanations using registered reasons. Keep automatic safe fallback behavior.

Self-check source-wide route inventory and absence of format allowlist copies. Run B5/B7, Plan round trips, routing suites, poisoned-source preflight tests, sentry seeded mutants, and full config validation. Codex and Dennis review especially the split current route architecture; remediate all bypasses.

### Slice 3 — pre-mask casting on every route

Make local/cloud/bounded CSV and fixed-width framing contract-aware so F06 lexemes and sentinel tokens survive inference/default-null handling. Implement `CastingContext`, batch/result/issue types, Arrow kernel and thin pandas adapter. Insert the same pre-mask boundary after framing and before strategy/relationship/`when` consumption on full-frame, sequential, chunked, OOC, mixed, and unified/physical routes. Preserve absolute row identity across chunks. Defer only routes that declare a real capability gap; never duplicate a parser to gain admission.

Self-check data immutability, bounded memory, null typing, row offsets, and strategy input types. Run A1, B1-B4 input halves, batch-size parity, route suites, memory/streaming tests, and regression suites for strategies and relationships. Codex and Dennis review route parity and source-read ordering; remediate.

### Slice 4 — post-mask output formatting and fixed-width integration

Insert the shared formatter before target encoding/publication. Adapt fixed-width layout integration so F06 contracts supersede only their declared columns while legacy `type` behavior remains. Require the exact parser oracle for every output descriptor. Ensure target projection, Arrow schema, and fixed-width width checks happen before publication. Binary COBOL output is admitted only through targets that preserve bytes.

Self-check inverse use and target capability facts. Run A1-A7 output halves, A2, B3-B5, fixed-width reader/writer regression and atomic-write failure tests. Codex and Dennis review truncation/overflow and target bypasses; remediate.

### Slice 5 — policies, counts, and F29 safe output

Implement the disposition reducer, field-null writes, deterministic fail exception/adaptation, count algebra, completeness, and evidence merge. Migrate F06 issues through registered F29 records only. Extend progress/log/security boundaries. Keep source null, sentinel substitution, parse-null, output-null, and quarantine failures distinct.

Self-check every issue is consumed once and no value-bearing exception escapes. Run B1/B2/B6, C5/C6, existing row-error summaries, F29 manifest/sentry/security tests, and seeded issue-drop mutants. Codex and Dennis review fail/partial semantics and leakage; remediate.

### Slice 6 — protected row quarantine

Implement the protocol and Fernet-encrypted POSIX local reference store, stream/dedup row envelopes, authorized read, finite expiry, purge, seal/abort, resource bounds, and opaque receipt. Bind it to the disposition reducer. Reuse the current row-filter semantics without its raw legacy writer. Preserve the old quarantine config for non-F06 callers; prohibit it as an F06 protected sink. Add optional adapters only when their actual contracts exist.

Self-check exact ownership targets, modes, no-follow behavior, budgets, abort, clock, and artifact schema. Run C1-C5 on a real temp filesystem, B1/B2, failure injection, cancellation, security, and publication-order tests. Codex then Dennis perform adversarial security/QA review; remediate. Any missing platform auth/lifecycle integration remains an explicit consumer gap, not a permissive engine fallback.

### Slice 7 — compatibility, evidence, documentation, and release gate

Run the complete suite and all route matrices on supported Python/Arrow/Pandas profiles. Verify old config/Plan/fixed-width fixtures, performance and bounded-memory thresholds, package build, public exports, JSON schema and reason/capability manifests. Produce an evidence record with exact commit, commands, pass/fail/skip counts, route/format matrix, mutation results, security tests, and remaining CLI/Studio work.

Barry updates shipped behavior only after implementation truth is known. Codex reviews the final diff first; Dennis then runs the repository quality gate (`/code-review`, `/security-review`, `/qa`) and cold-reads it. Resolve every BLOCKER/HIGH and rerun affected/broader checks. Barry synchronizes docs. Record the exact-HEAD receipt with `bash .claude/hooks/decoy-gate-pass.sh`; any new commit invalidates it.

## Verification and evidence plan

Minimum targeted commands are selected by the builder against actual test paths, but the evidence must include:

- config/JSON-schema and Plan V2/V3 serialization tests;
- catalog unit vectors and Hypothesis inverse/invalid-neighbor properties;
- fixed-width reader/writer and F06 end-to-end tests;
- full-frame, sequential, chunked, OOC, mixed, and unified/physical admission/execution matrices, including forced decline and automatic fallback;
- row-error/count/quarantine tests across batch partitions;
- real-filesystem protected-store tests and failure injection;
- F29 registry fixture, no-ad-hoc-code sentry, hostile reason/log boundary, and reason adapter tests;
- all new sentries with seeded mutants that prove each scanner/check can fail;
- lint, formatting, type checks, package build, and the full non-integration test suite required by `CONTRIBUTING.md`;
- relevant integration/slow suites with credentials/data-independent fixtures; every skip classified as environment-only, unsupported-by-contract, or defect. No unexplained skip is release evidence;
- performance measurements for per-cell kernels, batch memory, issue-heavy input, and streamed quarantine. A regression beyond the pre-agreed budget triggers optimization behind the same semantics, never a weaker parser.

The evidence matrix has rows for every format ID and columns for config validation, parse, sentinel, logical type, format, inverse, width, each route, each policy, count, reason code, safe output, and quarantine protection. Each cell links to a test result or an explicit unsupported registered rejection. This matrix is generated from the format/route registries so omissions fail the build.

## Documentation plan

Barry owns documentation synchronization for what actually ships:

- `README.md`: public config example, execution ordering, strictness, and engine/consumer completion boundary;
- `CODEMAP.md`: contract, casting, route evaluator, protected store, reason/count locations;
- config/JSON-schema reference: every V1 ID and field, exact grammars, examples and rejection cases;
- fixed-width docs: relationship between physical layout and F06 logical/output declarations, including `042`;
- security reference: raw-data boundary, F29 safe output, quarantine access/retention threat model, OS-mode limitation, expiry/purge operation;
- reason/capability manifest docs and CLI/Studio consumer handoff;
- compatibility/migration notes: legacy `dtype`, `type: float`, `date_shift.date_format`, old quarantine path, Plan V3;
- `CHANGELOG.md` and roadmap/build record only when code and cross-surface status justify them.

Examples must use synthetic values. Do not document unsupported locale inference, protected quarantine without a qualified sink, or route support beyond the generated matrix.

## Gate chain and remote-worker handoff

**Plan gate:** Codex authors this file -> Gemini reviews the exact file -> Opus-4.8 Dennis verifies anchors and adversarially reviews -> owner decides. Findings and resolutions are appended without falsifying original provenance. The remote claude.ai/code worker must receive this committed plan and the pinned baseline; it should not need local chat or private qmd context.

**Code gate order:** for each meaningful implementation artifact, self-check and targeted verification first, then **Codex review first**, remediation, then **Opus-4.8 Dennis** quality/security/QA review, remediation and rerun. Barry updates documentation after behavior stabilizes. Owner approves the completed evidence. This requested cross-model order precedes the repository's exact-HEAD pass receipt; Dennis remains the official quality-gate commander and does not write fixes.

Remote worker instructions:

1. Rebase/verify the implementation baseline and re-open every load-bearing anchor; if current code differs, stop and amend the plan rather than coding around drift.
2. Start with Slice 0 and preserve RED evidence. Do not implement later slices before the contract/gate findings are resolved.
3. Treat all **new** symbols/files here as designs, not evidence that a prerequisite exists. In particular, do not assume F22/F25/F26 platform implementations.
4. Use the standard library and existing pinned dependencies; any proposed new runtime dependency requires a separate reuse/supply-chain decision and plan amendment.
5. Do not broaden the catalog, add locale inference, or weaken protected quarantine to make a route pass. A truthful registered refusal is the correct result for an unsupported combination.
6. Preserve unrelated worktree changes, make small commits/slices, record exact commands/results, and never push or create a PR until the exact-HEAD gate passes.

The owner decision requested by this plan is the FRAME table as written: strict V1 formats, field-level null, row-level quarantine, and no F06 quarantine without a retention- and access-qualified protected sink.
