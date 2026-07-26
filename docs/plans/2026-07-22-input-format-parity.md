# Input-Format Parity (fixed-width / delimited / Parquet x local / S3 / GCS): Implementation Guide

- **Date:** 2026-07-22
- **Status:** DRAFT, not started. Slice 0 (contract lock) is a Cam decision; see §1 and §9.
- **Owner:** Cam (decoy backend lane)
- **Author:** Codex (gpt-5.x, `/codex` consult, 2026-07-22) against decoy-engine + decoy-platform @ main
- **Goal (Cam, 2026-07-22):** "fixed-width, delimited/csv, parquet all acceptable file inputs for both local + cloud upload."
- **Scope:** decoy-engine `config/_sources.py`, `config/_fixed_width.py`, `profile/_readers.py`, `profile/_cloud_readers.py`, `profile/_fixed_width_reader.py`; decoy-platform `api/files/`, `api/jobs/v2_cloud_staging.py`, `api/jobs/v2_config.py`, `api/jobs/streams.py`, `api/jobs/admission.py`, `web/src/pipelines/hifi/`
- **Related:** `docs/plans/2026-07-10-oom-avoidance-routing-redesign.md` (routing + admission), `docs/plans/2026-07-09-consultant-f1-f2-bounded-profiling.md` (bounded ProfileSource contract), `decoy-platform/docs/ROADMAP.md`

---

## Why this exists

Today the intake matrix has holes. Local files take csv/parquet/fixed_width; S3 and
GCS take csv/parquet only. "Delimited" is comma-only everywhere: every reader calls
`pd.read_csv` with no `sep`, and no source model has a delimiter field. The platform's
file preview sniffs `,` `\t` `|` `;` for rendering but never persists what it sniffed,
so a TSV renders as a correct table in the File Manager and then executes as a single
column. This guide closes all nine format x source cells.

## Findings that correct the prior analysis

Three things the pre-guide investigation got wrong or missed. They change the work, so
they are stated here rather than buried:

1. **`resolve_v2_fixed_width_layouts()` already iterates every source by `format`**, not
   only local files (`decoy-platform/api/jobs/v2_config.py:188`). The function is fine;
   its docstring is stale and it lacks S3/GCS tests.
2. **Cloud Parquet already reaches the out-of-core route.** Cloud sources are staged to a
   local descriptor before route selection (`decoy-platform/api/jobs/v2_orchestrator.py:167`,
   `:303`), so eligible S3/GCS Parquet becomes a local-Parquet descriptor and passes the
   gate at `decoy-platform/api/jobs/v2_out_of_core.py:187`. The scale gap is format-based
   (CSV and fixed-width fall back to the O(cardinality) sequential route), not source-based.
3. **Latent bug: fixed-width offsets are documented as bytes but sliced as characters.**
   The layout contract says byte ranges (`decoy-engine/src/decoy_engine/config/_fixed_width.py:11`);
   the reader opens decoded UTF-8 text and slices characters
   (`decoy-engine/src/decoy_engine/profile/_fixed_width_reader.py:137`, `:152`). ASCII data
   is unaffected, non-ASCII silently reads the wrong bytes. This must be resolved before
   any cloud fixed-width reader is written, because ranged reads are byte-addressed. See §6
   for the compatibility call.

---

## 1. Scope decision: intake parity first, route parity second

Treat the product goal as **intake parity**:

> Local file, S3, and GCS sources may each use CSV/delimited, Parquet, or fixed-width; all nine combinations validate, profile, preview, and execute with identical format semantics.

Do not silently expand this into **route parity**, where all nine combinations also reach the cardinality-independent DuckDB/Parquet out-of-core FK route.

Premise correction: cloud Parquet is staged before route selection (`decoy-platform/api/jobs/v2_orchestrator.py:167`), and the out-of-core gate receives the staged config (`decoy-platform/api/jobs/v2_orchestrator.py:303`). Eligible S3/GCS Parquet sources therefore already become local-Parquet descriptors and can pass `decoy-platform/api/jobs/v2_out_of_core.py:187`. The remaining scale gap is format-based: CSV and fixed-width fall back to the O(cardinality) sequential relationship route.

Route parity additionally requires:

- Bounded CSV/fixed-width parsing into canonical temporary Parquet.
- Exact pandas/Arrow dtype and null-token parity.
- Temporary-disk admission and cleanup.
- FK-key type preservation.
- Byte-parity tests against the existing sequential route.
- Performance calibration on reference hardware.

Recommended split:

1. **Milestone A:** intake parity for all nine cells; correct fallback routes are acceptable.
2. **Milestone B:** bounded text-to-Parquet canonicalization feeding the existing out-of-core route.

`PipelineConfig` subsetting remains Parquet-only at `decoy-engine/src/decoy_engine/config/_pipeline.py:276`; include CSV/fixed-width subsetting only in Milestone B.

## 2. Config schema change

Keep `format: "csv"` as the spelling for all delimited text. Do not add a competing `"delimited"` literal.

Add a nested model:

```python
class DelimitedOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sep: str = Field(default=",", min_length=1, max_length=1)
    quotechar: str | None = Field(default='"', min_length=1, max_length=1)
    escapechar: str | None = Field(default=None, min_length=1, max_length=1)
    doublequote: bool = True
    encoding: str = "utf-8"
    header: bool = True
    column_names: list[str] | None = None
```

Validation rules:

- `sep` is one non-NUL, non-newline character.
- Reject regex and multi-character separators. Pandas’ regex path and Arrow do not guarantee quoted-field parity.
- Validate `encoding` through `codecs.lookup`.
- `sep`, `quotechar`, and `escapechar` must be distinct when present.
- `header=False` requires unique, non-empty `column_names`.
- `header=True` forbids `column_names`.
- `quotechar=None` maps to `quoting=csv.QUOTE_NONE`.

Include header handling now. The platform already exposes headerless parsing and explicit names in `decoy-platform/api/storm/schemas.py:101`; omitting them would retain scan/run divergence.

Refactor `decoy-engine/src/decoy_engine/config/_sources.py:38` around a private base:

```python
InputFormat = Literal["csv", "parquet", "fixed_width"]

class _FormattedSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: InputFormat
    format_options: DelimitedOptions | None = None
    layout: FixedWidthLayout | None = None

    @model_validator(mode="after")
    def _validate_format_payload(self):
        if self.format == "csv":
            if self.layout is not None:
                raise ValueError("layout is only valid for fixed_width")
        elif self.format == "fixed_width":
            if self.layout is None:
                raise ValueError("fixed_width requires layout")
            if self.format_options is not None:
                raise ValueError("delimited format_options are only valid for csv")
        else:
            if self.layout is not None or self.format_options is not None:
                raise ValueError("parquet accepts neither layout nor format_options")
        return self
```

Have `FileSource`, `S3Source`, and `GCSSource` inherit this base and add only locator/credential fields. All three then accept the same format literal set without duplicating validation.

Preserve top-level `layout`. Moving it under `format_options` would unnecessarily rewrite existing fixed-width configs. A nested `format_options` model is still preferable for the growing delimited dialect because it prevents top-level field sprawl and gives its own `extra="forbid"` boundary.

Missing `format_options` must remain distinct from an explicit model in normalized output; readers resolve `None` to `DelimitedOptions()` without mutating validation input.

## 3. Reader work by format and source

| Source | CSV/delimited | Parquet | Fixed-width |
|---|---|---|---|
| Local | Extend `CsvFileSource` with options | No semantic change | Refactor existing reader around bytes/stream core |
| S3 | Thread options through `S3CsvSource` | No semantic change | Add `S3FixedWidthSource` |
| GCS | Thread options through `GcsCsvSource` | No semantic change | Add `GcsFixedWidthSource` |

### Delimited readers

Thread the full dialect through every intake read:

- Local schema inference: `decoy-engine/src/decoy_engine/profile/_readers.py:222`
- Local bounded sample: `decoy-engine/src/decoy_engine/profile/_readers.py:226`
- Local full read: `decoy-engine/src/decoy_engine/profile/_readers.py:229`
- Cloud sample/schema helper: `decoy-engine/src/decoy_engine/profile/_cloud_readers.py:132`
- S3 full read: `decoy-engine/src/decoy_engine/profile/_cloud_readers.py:282`
- GCS full read: `decoy-engine/src/decoy_engine/profile/_cloud_readers.py:386`
- Platform full-frame execution: `decoy-platform/api/jobs/v2_cloud_staging.py:282`
- Platform streaming header/sample/parser: `decoy-platform/api/jobs/streams.py:102`, `:134`, `:161`

Map options consistently:

```python
kwargs = {
    "sep": options.sep,
    "encoding": options.encoding,
    "header": 0 if options.header else None,
    "names": None if options.header else options.column_names,
    "quotechar": options.quotechar,
    "escapechar": options.escapechar,
    "doublequote": options.doublequote,
}
```

When `quotechar is None`, omit `quotechar` and pass `quoting=csv.QUOTE_NONE`.

For Arrow streaming, set `ReadOptions.encoding`, `ParseOptions.delimiter`, `quote_char`, `escape_char`, and `double_quote`. If an option cannot be represented identically in Arrow, reject the streaming route during `classify_streaming_eligibility()` and use full-frame execution. Never admit a route that later changes parsing semantics.

Quoted fields containing the delimiter need no custom parser; pandas/Arrow handle them once options are threaded correctly.

Quoted embedded newlines are a separate trap. The cloud helper currently truncates at a physical newline (`decoy-engine/src/decoy_engine/profile/_cloud_readers.py:138`), which may cut inside a quoted logical record. Replace the fixed trimming behavior with geometrically growing ranged windows parsed by pandas until:

- `sample_rows` logical records are available;
- EOF is reached; or
- a documented maximum profile-window budget is exceeded.

At the budget, raise a typed bounded-sample error. Do not implement another CSV state machine.

### CSV row estimator

Non-comma delimiters do not require changing the estimator. It measures physical byte widths, not parsed fields (`decoy-engine/src/decoy_engine/profile/_readers.py:263`).

Required adjustments:

- With `header=False`, set `header_bytes=0` and include the first physical line in the sample.
- Retain `exact=False`.
- Document that quoted embedded newlines reduce accuracy because physical lines are not logical records.

### Fixed-width core

Resolve this trap before implementing cloud readers: the layout contract says offsets are bytes (`decoy-engine/src/decoy_engine/config/_fixed_width.py:11`), but the reader opens decoded UTF-8 text and slices characters (`decoy-engine/src/decoy_engine/profile/_fixed_width_reader.py:137`, `:152`).

Recommended pre-GA correction:

- Read binary records.
- Apply `[start:start+width]` to bytes.
- Decode each sliced field after slicing.
- Fail if a boundary splits an encoded character.
- Preserve PII-safe errors with no raw value or chained cast exception.

Do not widen `read_fixed_width()` to `str | bytes | BinaryIO`. Add:

```python
def read_fixed_width_bytes(
    data: bytes,
    layout: FixedWidthLayout | dict[str, Any],
    *,
    source_label: str,
    max_records: int | None = None,
) -> pd.DataFrame:
    ...
```

Implement `read_fixed_width(path, ...)` and `read_fixed_width_bytes(...)` through a private binary-stream parser. Export the supported entry points through `decoy_engine.profile`; stop adding new platform imports from `_fixed_width_reader`.

### Cloud fixed-width `ProfileSource`

Add S3/GCS classes with the existing `ProfileSource` methods.

Reuse the cloud `(size, fetch(start, end_inclusive))` shape, but not `_RangeFileReader`. `_RangeFileReader` is for Parquet random access; fixed-width needs one prefix range.

Behavior:

- `schema()`: fetch and parse one record.
- `sample_frame(n)`: fetch a bounded prefix sufficient for `n` records.
- `to_frame()`: full object download only on the explicit eager path.
- `row_count()`: use Content-Length plus physical record size.

The “Content-Length alone, no body transfer” premise is not achievable with the current layout. `record_width` is only the minimum column extent; trailing bytes are permitted, and LF versus CRLF is unspecified. Current local counting reads the first line at `decoy-engine/src/decoy_engine/profile/_readers.py:175`.

For Milestone A, use one small prefix range to learn physical record bytes, then compute the count. For a later metadata-only optimization, add an explicit physical record-length and terminator contract to `FixedWidthLayout`.

Also reconcile blank lines: the parser skips them at `decoy-engine/src/decoy_engine/profile/_fixed_width_reader.py:142`, while byte-division counting assumes uniform records. Either reject blank records or mark the count inexact.

## 4. Platform work

### Upload acceptance

Both upload paths already accept arbitrary bytes and preserve extensions:

- 500 MB simple path: `decoy-platform/api/files/router.py:412`
- 10 GB chunked path: `decoy-platform/api/files/chunked_router.py:61`

No format whitelist change is required. Both finalization paths must, however, persist the same parsing metadata described in section 5.

### Fixed-width layout binding

Premise correction: `resolve_v2_fixed_width_layouts()` already checks every descriptor by `format`, without filtering source type (`decoy-platform/api/jobs/v2_config.py:188`). Keep the logic, update its local-file-only docstring, and add S3/GCS tests.

Use the engine-compatible `FixedWidthLayout` model/table at `decoy-platform/api/models.py:1085`. Do not use `FwDefinition` from `decoy-platform/api/models.py:1021`; that is a legacy preview-only, 1-based schema.

### Cloud staging

Critical trap: `stage_cloud_sources()` reconstructs descriptors with only `type`, `path`, and `format` (`decoy-platform/api/jobs/v2_cloud_staging.py:370`). It therefore drops fixed-width `layout` and would drop every future dialect option.

Rewrite using an explicit shared-field allowlist:

```python
staged_descriptor = {
    "type": "file",
    "path": str(staged),
    "format": descriptor["format"],
}
for field in ("format_options", "layout"):
    if field in descriptor:
        staged_descriptor[field] = copy.deepcopy(descriptor[field])
```

Do not spread the entire cloud descriptor: `bucket`, `key`, `object`, and credentials would violate `FileSource.extra="forbid"`.

Add a stage-contract test asserting:

- Original cloud config is unchanged.
- Staged descriptor preserves parsing fields exactly.
- Cloud-only locator and credential fields are absent.
- The staged descriptor validates as `FileSource`.

### Execution and streaming

`_read_sources_as_arrow()` already stages direct cloud calls and dispatches all three formats at `decoy-platform/api/jobs/v2_cloud_staging.py:264`; make its CSV branch consume `format_options`.

Until fixed-width streaming exists, add a `fixed_width_source_not_streamable` rejection to `classify_streaming_eligibility()`. The current classifier can admit a fixed-width single-table job even though `iter_source_batches()` rejects it at `decoy-platform/api/jobs/streams.py:185`.

`streams.py` is already near the repo’s 600-line orchestration limit. Extract CSV/Parquet source readers into a cohesive module before adding dialect handling.

### Admission multiplier

Add `FIXED_WIDTH_MEMORY_MULTIPLIER`. Do not invent its final value.

Use `6.0` provisionally because the existing fixed-width reader materializes a Python `list[dict]`, a pandas DataFrame, and then an Arrow table. This is conservative relative to Parquet but not proven sufficient.

Calibrate with `decoy-platform/tests/perf/test_execution_metrics_admission_budgets.py` across:

- Narrow and wide layouts.
- String-heavy and numeric-heavy columns.
- 1 MB, 10 MB, and 100 MB inputs.
- LF and CRLF.
- Local and staged cloud execution.
- Reference deployment hardware.

Choose the observed worst peak-RSS/input-byte ratio plus a stated safety margin. Record the measurement and hardware in `admission.py`, following the existing CSV provenance at `decoy-platform/api/jobs/admission.py:39`.

Update `_dominant_multiplier()` at `decoy-platform/api/jobs/admission.py:255` to select the maximum multiplier among present formats.

## 5. Preview/run mismatch

Replace `_detect_delimiter()` at `decoy-platform/api/files/router.py:33`; it chooses the first candidate character present in the first line and can select a comma inside a quoted header.

Extract detection/preview logic from the already oversized router into `api/files/dialect.py` and `api/files/preview.py`.

Detection flow:

1. Read a bounded multi-record sample.
2. Use `csv.Sniffer` for candidate delimiter and quoting.
3. Parse the sample with that candidate.
4. Require consistent column counts across complete records.
5. Return canonical `DelimitedOptions` plus `detected | ambiguous`.

Persist on `UploadedFile`:

```text
delimited_options_json       TEXT NULL
delimited_options_confirmed  BOOLEAN NOT NULL DEFAULT false
```

Flow:

- Upload/finalize performs bounded detection for likely text inputs.
- High-confidence detection is stored with `confirmed=false`.
- Preview uses the persisted options and returns them to the UI.
- A PATCH endpoint validates operator overrides and sets `confirmed=true`.
- New Job and graph source emitters copy the canonical options into `sources[*].format_options`.
- Cloud source cards expose the same options directly in node config.

When detection is wrong, the explicit operator override wins. When ambiguous, do not silently emit a comma config for a new pipeline; show the raw preview and require delimiter confirmation.

Do not mutate parsing metadata from a GET preview request. Do not retroactively change an existing saved pipeline when uploaded-file metadata changes: its source descriptor is the execution contract.

Fix frontend emission:

- `decoy-platform/web/src/pipelines/hifi/yaml/graphToV2.ts:85`: widen all source descriptor formats to include `fixed_width`; add `format_options` and `layout`/platform-only `layout_id`.
- `decoy-platform/web/src/pipelines/hifi/yaml/graphToV2.ts:313`: honor the node’s explicit format and parsing config instead of calling `fileFormat(path)`.
- `SourceFileCard.tsx` already captures delimiter/header controls, but its fields are currently discarded by the emitter.
- Add source-only fixed-width and delimited controls to `CloudS3Card.tsx` and `CloudGcsCard.tsx`; do not widen cloud target formats as part of this input-only goal.
- `StudioNewJob.tsx:253` sends the selected delimiter to STORM, but `StudioNewJob.tsx:286` omits it from the generated pipeline. Include the same canonical options there.

## 6. Backward compatibility

Defaults must preserve current behavior:

```yaml
format: csv
# format_options omitted
```

is equivalent to:

```yaml
format: csv
format_options:
  sep: ","
  quotechar: '"'
  escapechar: null
  doublequote: true
  encoding: utf-8
  header: true
```

Rules:

- Keep `format: csv`.
- Existing configs with no options must not consume detected upload metadata automatically.
- Parquet behavior is unchanged.
- Existing fixed-width top-level `layout` remains valid.
- Options for an unrelated format fail validation instead of being ignored.
- Cloud staging preservation changes no old output because old descriptors lack these fields.

The byte-offset correction can alter existing non-ASCII fixed-width output. ASCII remains unchanged. Make this an explicit PO decision and pin before/after tests. Recommended action is to correct it now, before GA, because the current implementation contradicts its documented contract.

`RELEASE_PHASE` is `"pre-ga"` at `decoy-engine/src/decoy_engine/release.py:45`. This permits a hard correction without deprecation shims. It does not justify silent output drift, weakening fail-closed validation, exposing raw values in errors, or changing persisted evidence/vault formats.

## 7. Test plan

Follow the repo convention: land each assertion test first and observe it fail before implementation.

### Fixtures

Create one logical table in:

- Comma CSV.
- TSV.
- Pipe.
- Semicolon.
- Custom single-character delimiter such as `^`.
- Parquet.
- Fixed-width with gaps and mixed string/int/float fields.

Delimited cases must include:

- Delimiter inside a quoted field.
- Doubled quotes.
- Explicit escape character.
- Empty and null-like values.
- Quoted embedded newline.
- Headerless input with explicit names.

Fixed-width cases must include:

- Left/right padding.
- LF and CRLF.
- Final newline present and absent.
- Blank record policy.
- Trailing unused bytes.
- Non-ASCII value crossing byte/character offset differences.
- Short record and cast failures.

### CI assertions

- Parameterized schema test accepts all nine source/format cells.
- All three fixed-width source types require `layout`.
- CSV/Parquet reject `layout`; Parquet/fixed-width reject delimited options.
- TSV and comma equivalents produce identical:
  - schemas;
  - samples;
  - Arrow input tables;
  - masked logical output;
  - row counts and exactness flags.
- Local, S3, and GCS fixed-width readers over identical raw bytes produce equal DataFrames and byte-identical Arrow IPC serialization.
- Bounded cloud fixed-width profile never issues a full-object GET.
- Cloud staging preserves `layout` and `format_options`.
- Schema, sample, full-frame, and streaming CSV readers apply identical options.
- Preview-returned options equal persisted options, emitted config, and execution options.
- Ambiguous detection blocks new pipeline emission until confirmed.
- Existing optionless comma configs retain their golden output.
- Staged S3/GCS Parquet remains eligible for out-of-core routing.
- Fixed-width streaming classification falls back before execution.
- Errors and `capture_locals=True` tracebacks contain no offending source value.

Use moto and the existing fake GCS client in normal CI; do not require real cloud credentials.

### Property tests

- Generate valid one-character delimiters and field values containing separators/quotes; serialize with `csv.writer` and round-trip through local/S3/GCS readers.
- Generate valid non-overlapping fixed-width layouts and records; assert path and bytes entry points agree.
- Generate overlaps, duplicate names, short rows, invalid codecs, and split multibyte boundaries; assert fail-closed behavior.

### Nightly

- Real S3/GCS smoke.
- 100 MB admission calibration.
- Large quoted-record window growth.
- Text-to-Parquet route-parity benchmarks when Milestone B begins.
- Temporary-disk exhaustion and cleanup tests.

## 8. Shippable sequencing

| Slice | Size | Judgment | Deliverable |
|---|---:|---|---|
| 0. Contract lock | S | High | ADR: intake versus route parity, one-character separators, header handling, fixed-width byte semantics |
| 1. Delimited engine vertical | M | Medium | Assertion tests, `DelimitedOptions`, shared source base, local/S3/GCS profiling and full reads |
| 2. Delimited platform vertical | M | Medium | Stage preservation, full-frame reader, streaming reader or safe fallback, nine-cell config tests remain green |
| 3. Cloud fixed-width | M–L | High | Binary parser core, S3/GCS `ProfileSource`, platform staging/execution, range tests |
| 4. Preview and authoring parity | L | High | DB migration, detector/override API, preview, New Job, graph emitters, cloud source cards |
| 5. Admission | S code, M evidence | Medium | Named fixed-width multiplier, nightly calibration, documented value |
| 6. Docs and sentries | S | Mechanical | Config reference, capability matrix, known-limit removal, schema/read-path sentries |
| 7. Route parity | XL | High | Bounded CSV/fixed-width-to-Parquet canonicalizer feeding the existing out-of-core route |

For slices 1–3, do not activate a new schema combination before its reachable profile and execution paths are correct. Each slice lands green.

Match load-bearing docstring standards:

- `_sources.py`: Pydantic discriminated-union and format-option contract.
- `_readers.py`: pandas `read_csv` dialect mapping and estimator limitations.
- `_cloud_readers.py`: S3/GCS ranged-read pattern and bounded-window policy.
- `_fixed_width_reader.py`: byte-offset invariant and decoding boundary.
- Platform streaming module: Arrow `open_csv`/`ParseOptions` parity and fallback conditions.

Comments should explain invariants and tradeoffs, not restate branches.

## 9. Risks and open questions

- **Fixed-width encoding:** UTF-8 newline-delimited input is sufficient for Milestone A. Latin-1 is feasible after byte slicing. CP1047/EBCDIC requires a separate decision: codec availability, EBCDIC record separators, and block-record files are not solved by adding `encoding`.
- **Byte versus character offsets:** must be resolved before cloud fixed-width. Prefer bytes because that matches the documented contract, mainframe layouts, and ranged reads.
- **Physical record model:** define LF/CRLF, final terminator, trailing bytes, and blank-line behavior before claiming exact metadata-only row counts.
- **Quoted newlines:** fixed physical-window trimming is unsafe; bounded incremental parsing is required.
- **Quoted delimiters:** must use the configured quote/escape behavior in every reader, including Arrow streaming.
- **Regex/multi-character delimiters:** keep out of the V2 source contract unless pandas/Arrow parity is proven. STORM scans using them should materialize to Parquet before pipeline promotion.
- **Delimiter detection:** `csv.Sniffer` is still heuristic. Persist the result, surface confidence, and provide explicit confirmation/override.
- **Pandas versus Arrow:** pin NA tokens, header handling, quote behavior, encoding, and column naming. A library upgrade may otherwise change streaming/full-frame parity.
- **Fixed-width memory:** the current list-of-dicts parser may exceed the CSV multiplier. Calibration decides the number.
- **Temporary disk:** route parity may simultaneously hold a staged raw cloud object and canonical Parquet. Add disk admission before enabling it.
- **Legacy layouts:** `FwDefinition` and engine `FixedWidthLayout` have incompatible indexing and shapes. Do not silently translate one in the job path.
- **Cloud range cost:** many tiny range calls are worse than one bounded prefix. Fixed-width sampling should issue one prefix request whenever possible.
- **Oversized modules:** do not add more parsing logic to the 1,272-line `api/files/router.py`; extract it. Split source readers from `streams.py` before crossing the 600-line orchestration cap.