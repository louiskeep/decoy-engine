# File-Format Contract for Local and Cloud Sources

Status: proposal  
Date: 2026-10-01

## Purpose

Decoy must treat a file description as a durable contract.

The user describes a file once:

- On the platform, upload or select the file, run a bounded STORM scan, confirm or override the detected format, then save the normalized description.
- In the CLI, run `decoy init`, confirm or override the bounded detection result, then write the normalized description into pipeline YAML.

Every later preview, profile, mask, generation, streaming, and cloud-staging path must use that stored description. Execution must not sniff the file again. A run must not perform an automatic full-file preflight scan.

File parsing produces Arrow. Rust is the default execution path at every supported size. pandas remains available only when a CLI user opts out of Rust or when a requested operator has no Rust or named Arrow implementation. File format must not select the masking backend.

Passthrough columns must retain their Arrow arrays and types exactly. The native passthrough kernel already returns the input array unchanged, and the native schema builder treats passthrough output as type-preserving (`decoy-engine/src/decoy_engine/execution/native/_kernels_scalar.py:49-51`, `decoy-engine/src/decoy_engine/execution/native/_requirements.py:302-324`). The wider execution program must remove conversions that route unchanged values through pandas.

This proposal supersedes the unstarted July draft and updates the September research record for the public fixed-width reader, the CLI format-dispatch work, the Rust route expansion, and the platform’s current STORM surface.

## Contract principles

1. `format` is explicit. A suffix is an authoring hint, not an execution contract.
2. Parsing settings are explicit after authoring. A run does not infer missing settings.
3. Detection is bounded. It reads a byte and record budget, reports confidence, and requires confirmation or override.
4. The confirmed result is stored in normalized config.
5. Local, S3, and GCS sources use the same format block.
6. Cloud staging preserves the entire format block.
7. Full-frame and chunked readers implement the same parsing semantics.
8. Input parsing and target formatting are separate contracts.
9. Unsupported combinations fail validation before execution.
10. Runtime validation is incremental. Decoy does not scan the whole file before starting a run.
11. Readers return Arrow with stable types. Sampled type inference never determines execution types.
12. Format parsing remains an Arrow boundary concern. It does not belong in masking kernels.

## Current reality

### Capability matrix

| Format and source | Config | Profile | Full-frame execution | Chunked execution |
|---|---|---|---|---|
| Local CSV | Yes | pandas defaults | Platform and CLI read all columns as strings | Platform Arrow reader and CLI pandas chunks |
| S3/GCS CSV | Yes | Bounded prefix range | Object is staged, then read as local CSV | Object is staged, then local chunk reader |
| Local Parquet | Yes | Footer and bounded batches | Typed Arrow | Row-group batches |
| S3/GCS Parquet | Yes | Random range reads | Object is normally staged, then read locally | Object is staged, then row-group batches |
| Local fixed-width | Yes, with layout | Bounded record read | Engine fixed-width reader | Rejected |
| S3/GCS fixed-width | No | No | Staging code could carry the bytes, but config and metadata preservation are incomplete | Rejected |

Local `FileSource` accepts `csv`, `parquet`, and `fixed_width`. It requires `layout` only for fixed-width (`decoy-engine/src/decoy_engine/config/_sources.py:38-68`). S3 and GCS accept only CSV and Parquet and have no layout or dialect field (`decoy-engine/src/decoy_engine/config/_sources.py:71-125`). All three models forbid unknown fields, so dialect keys are rejected rather than ignored (`decoy-engine/src/decoy_engine/config/_sources.py:46,80,112`).

Local reader dispatch uses the declared format, not the suffix (`decoy-engine/src/decoy_engine/profile/_readers.py:330-349`). The CLI format-dispatch branch follows the same rule for whole-file reads and preserves CSV as strings, Parquet types, and fixed-width declared types (`decoy/src/decoy/cli/_sources.py:1-7`, `:31-54`). Its chunked path supports CSV and Parquet and rejects fixed-width with instructions to run without `--chunked` (`decoy/src/decoy/cli/_sources.py:57-91`).

The public fixed-width-reader slice exports `read_fixed_width` from the package root (`decoy-engine/src/decoy_engine/__init__.py:164,422`). The platform still imports the private module (`decoy-platform/api/jobs/v2_cloud_staging.py:371-375`). Platform and CLI callers should use the public export.

The unified Rust lane now admits local descriptors declared as Parquet, CSV, or fixed-width (`decoy-engine/src/decoy_engine/execution/_unified_slice_admission.py:177,268-282`). This does not mean all sizes and operators are already Rust-backed. It establishes that the source format itself is no longer a reason to select pandas.

### Platform authoring is ahead of the engine contract

STORM accepts a literal or multicharacter delimiter, header presence, quote stripping, a regex-delimiter flag, explicit headerless column names, and fixed-width columns (`decoy-platform/api/storm/schemas.py:101-119`). It stores these choices in the scan parser block (`decoy-platform/api/storm/helpers.py:74-99`). Regex separators select pandas’ Python engine. Disabling quote stripping maps to `csv.QUOTE_NONE`. The streaming scan tries UTF-8, then Latin-1 (`decoy-platform/api/storm/router.py:714-742`).

Those settings are not expressible in the engine’s `FileSource`. A STORM scan can therefore interpret a file differently from the job that later executes it.

The File Manager has a separate preview path. Its delimiter detector selects the first of comma, tab, pipe, or semicolon present in the first line and defaults to comma (`decoy-platform/api/files/router.py:46-50`). It assumes a header unless a separate header layout supplies names (`decoy-platform/api/files/router.py:187-200`, `:245-250`). The preview result does not constitute a complete execution dialect.

The CLI guide also documents `csv_options`, `definition_path`, and `fixed_width_options`, but those fields are rejected by the current engine schema (`decoy-platform/docs/guides/cli-yaml-workflows.md:166-211`). The guide correctly states the intended product boundary: platform V1 manages reusable file layouts through Files and STORM rather than requiring normal users to hand-author them (`decoy-platform/docs/guides/cli-yaml-workflows.md:211`).

### Cloud staging loses parsing metadata

Platform execution stages cloud objects to local files before reading them. This is a useful transport boundary, but the rewritten descriptor currently retains only `type`, `path`, and `format` (`decoy-platform/api/jobs/v2_cloud_staging.py:392-427`, `:494-509`). It drops a fixed-width layout and would drop a future CSV dialect block.

Staging must replace only the locator. It must preserve the normalized format contract without modification.

## Describe once

### Platform flow

1. Upload or select a local or cloud object.
2. Read a bounded sample.
3. Propose a format and complete parsing contract.
4. Return each inferred field with a confidence level and evidence summary.
5. Show a parsed preview using that exact proposal.
6. Require confirmation or an override for ambiguous fields.
7. Store the normalized contract with the file or reusable layout.
8. Copy the contract into each generated source descriptor.
9. Use that descriptor unchanged for preview, profile, execution, and reruns.

STORM’s existing parser block is the starting point, but its result must become the same schema accepted by the engine.

### CLI flow

`decoy init <file>` should perform the same bounded detection and write the normalized contract into YAML. Today it dispatches by `.csv`, `.tsv`, `.parquet`, or `.pq` and reads at most 10,000 CSV rows, but it does not produce a durable dialect description and does not support fixed-width authoring (`decoy/src/decoy/cli/init.py:122-139`, `:362-374`).

Interactive use should ask the user to confirm low-confidence fields. Non-interactive use should require explicit flags or fail if the result is ambiguous. It must not silently accept a low-confidence guess.

### Bounded detection

Detection may read a bounded prefix and a bounded number of complete logical records. It must not read the full file merely to improve confidence.

For delimited text:

1. Check for a recognized BOM.
2. Validate candidate encodings over the bounded sample.
3. Test a bounded set of literal delimiters and quote rules with a standard parser.
4. Reject candidates that produce inconsistent field counts across complete logical records.
5. Score remaining candidates by record consistency, nontrivial column count, quote balance, and uniqueness of the winning candidate.
6. Estimate whether the first logical record is a header.
7. Present detected null-like values and types as observations, not automatic execution settings.
8. Persist the confirmed values.

Confidence should be per field:

- `high`: one candidate is structurally consistent across the sample.
- `medium`: several candidates work, but one has stronger evidence.
- `low`: evidence is insufficient or conflicting.
- `explicit`: the user supplied or confirmed the value.

A high-confidence result may be preselected, but the platform and CLI must allow confirmation or override. Execution receives only the normalized values, not an instruction to sniff again.

## Delimited files

`format: csv` remains the stable literal for all supported delimited text. Do not add a competing `delimited` format value.

### Required declarations

A runnable delimited source must declare:

- Delimiter.
- Quote character, or quoting disabled.
- Escape character, or none.
- Whether doubled quotes escape a quote.
- Header mode.
- Column names when no header is present.
- Encoding.
- BOM policy.
- Record terminator.
- Null-token policy.
- Column type policy.
- Bad-record policy.
- Whether leading and trailing whitespace is significant.

Regex and multicharacter separators should not be part of the portable execution contract. They do not have consistent quoted-field semantics across pandas, Arrow, and Rust-capable readers.

### Safe inference

The following fields can be proposed from a bounded sample:

- UTF BOM.
- UTF-8 validity.
- Literal delimiter.
- Common quote character.
- LF versus CRLF.
- Probable header presence.
- Observed column count.
- Candidate column names.

The result must be stored.

The following fields cannot be inferred safely enough to become execution policy without confirmation:

- Null tokens.
- Escape semantics in files that contain no escape examples.
- Whether an empty field means null or empty string.
- Column types.
- Date formats.
- Whitespace trimming.
- Whether a rare malformed row should be rejected or skipped.
- A legacy single-byte encoding with no distinguishing characters. Latin-1 accepts every byte sequence and therefore cannot be identified merely because decoding succeeds.

### Current assumptions and risks

| Setting | Current assumption | Risk |
|---|---|---|
| Delimiter | Comma in engine and execution readers. File preview guesses from the first line. | TSV, pipe, semicolon, and custom files may become one column or a wrong shape. A comma inside a quoted header can defeat preview detection. |
| Quoting | Library default double quote. | Alternate quote characters or quote-free files can parse differently across preview, profile, full-frame, and streaming paths. |
| Escape | Library default, generally no escape character with doubled quotes enabled. | Backslash-escaped vendor files can split or corrupt fields. |
| Header | First record. | Headerless files lose the first data row and receive data-derived names. |
| Encoding | pandas or UTF-8 defaults. STORM alone retries Latin-1. | A file can scan successfully in STORM and fail in execution. |
| Line terminator | Parser default. Cloud profiling trims at physical LF. | Embedded quoted newlines can be mistaken for record boundaries. |
| Null tokens | Live pandas defaults in full-frame paths and a pinned pandas-derived list in platform Arrow streaming. | Library upgrades or path differences can change strings such as `NA`, `NULL`, or `None` into nulls. |
| Column types | Engine profiling samples with pandas inference. Platform and CLI execution force CSV columns to strings. | The profile schema can differ from the Arrow table that is actually masked. |
| BOM | No explicit field. | UTF-8 BOM, UTF-16, and other BOM-bearing files can produce different first-column names or decoding behavior. |
| Whitespace | Library default. | Identifiers can be changed by path-specific trimming. |
| Bad records | Library default. | One route can reject a row while another skips or reshapes it. |

Engine CSV profiling uses pandas defaults for schema, bounded samples, and full reads (`decoy-engine/src/decoy_engine/profile/_readers.py:216-250`). Platform full-frame execution forces `dtype=str`, while Parquet remains typed and fixed-width uses declared types (`decoy-platform/api/jobs/v2_cloud_staging.py:315-322`, `:355-376`). Platform streaming forces Arrow string columns and pins the pandas null-token set, but otherwise uses Arrow CSV defaults (`decoy-platform/api/jobs/streams.py:65-95`, `:101-151`).

The normalized contract must remove this divergence. CSV execution should default to Arrow strings, with explicit per-column types available when the user needs them. Profiling must use the same Arrow schema as execution.

### Streaming

A delimited source is streamable when its stored dialect can be represented exactly by the selected streaming parser.

The first implementation should support:

- One-byte literal delimiter.
- Optional one-byte quote and escape characters.
- Explicit doubled-quote behavior.
- Explicit header mode and names.
- Supported encoding.
- Explicit null-token set.
- All-string or explicit Arrow column types.
- LF or CRLF records.
- Quoted embedded newlines when supported by the selected parser.

If a stored dialect cannot be represented exactly, validation must select an explicit fallback or reject the route. It must not run with altered semantics.

The current platform batch reader supports CSV and Parquet only (`decoy-platform/api/jobs/streams.py:160-191`). CLI chunking also supports only those two formats (`decoy/src/decoy/cli/_sources.py:57-91`).

### Cloud reads

Delimited discovery and profiling should use a growing prefix range:

1. Fetch a bounded initial range.
2. Decode only complete characters.
3. Parse complete logical records.
4. Grow geometrically if the range ends inside a quoted record.
5. Stop at the record target, EOF, or byte budget.
6. Return an explicit bounded-sample error at the budget.

The current cloud profiler fetches a fixed 1 MiB prefix and truncates at a physical newline before calling pandas (`decoy-engine/src/decoy_engine/profile/_cloud_readers.py:34-40`, `:116-140`, `:260-284`, `:359-391`). That approach is not safe for quoted embedded newlines.

Execution may initially keep stage-once behavior. A later direct cloud streaming reader can use sequential ranges, but it must retain parser state across ranges and must use the same stored dialect.

## Fixed-width files

### Current contract

`FixedWidthColumn` currently contains:

- `name`
- Zero-based `start`
- Positive `width`
- `type: str | int | float`
- One-character `pad`
- `align: left | right`

(`decoy-engine/src/decoy_engine/config/_fixed_width.py:62-82`)

Columns must be unique, ascending, and non-overlapping. Gaps are allowed. `record_width` is only the maximum column end, not a declared physical record length (`decoy-engine/src/decoy_engine/config/_fixed_width.py:85-122`).

The model says offsets are byte ranges, but the reader decodes UTF-8 and slices Python characters (`decoy-engine/src/decoy_engine/config/_fixed_width.py:9-18`, `decoy-engine/src/decoy_engine/profile/_fixed_width_reader.py:137-152`). The hardened public-reader slice reads physical lines in binary for bounded error handling, but still decodes UTF-8 before slicing. Roadmap A10 must resolve this mismatch before cloud fixed-width range reads are authoritative.

### Required declarations

A complete fixed-width layout must declare:

- Offset unit.
- File or field encoding.
- Physical record model.
- Record length where applicable.
- Terminator where applicable.
- Header and trailer record handling.
- Column ranges.
- Column logical types.
- Field representation.
- Padding and alignment.
- Null representation.
- Numeric sign and scale rules.
- Date formats.
- Overflow and invalid-value behavior.
- Whether trailing bytes are permitted.

### Byte versus character offsets

Three choices exist:

- Character offsets after decoding.
- Byte offsets before decoding.
- An explicit unit per layout.

Byte offsets are the recommended canonical contract. They match mainframe layouts, fixed-length cloud ranges, packed decimal, and the existing documentation. A byte range is sliced first, then decoded according to the field representation.

Existing layouts need an explicit migration. Current behavior is character slicing even though the model says bytes. Decoy must not silently reinterpret an old layout. Normalize each stored layout with `offset_unit: character` or `offset_unit: byte`, show a preview, then require confirmation before conversion.

### Encodings

The first supported set should include:

- UTF-8 for newline-delimited character layouts.
- ISO-8859-1 or `latin-1`.
- A curated set of EBCDIC codecs, beginning with the code pages required by real inputs.
- Raw binary fields for packed decimal.

EBCDIC and packed decimal require byte offsets. Do not decode the whole record and then slice it. Text fields decode after byte slicing. Binary numeric fields are interpreted directly from bytes.

Encoding inference should be limited to a recognized BOM and valid UTF-8. Latin-1 and EBCDIC require explicit selection or user confirmation.

### Physical record models

Support an explicit union:

- `delimited`: each record ends with `lf` or `crlf`.
- `fixed_length`: every record occupies exactly `length` bytes and has no terminator.
- `fixed_length_with_terminator`: fixed payload bytes followed by a declared terminator.

Do not derive exact row counts by dividing file size by the first line unless the physical record contract proves uniformity.

Today the profiler labels fixed-width row count exact by dividing file size by the first physical line length (`decoy-engine/src/decoy_engine/profile/_readers.py:183-203`). The parser skips blank lines (`decoy-engine/src/decoy_engine/profile/_fixed_width_reader.py:141-143`), the layout permits trailing bytes, and the record terminator is not declared. Those facts make the exactness claim unsafe.

### Header and trailer records

The first contract should support fixed counts:

- `header_records`
- `trailer_records`

A header or trailer can be skipped or parsed by a separately named layout. Record-type discriminators should wait for a concrete customer format because they introduce conditional layouts and ordering rules.

A cloud reader cannot discard trailer records correctly from a forward-only stream unless the count is known and it retains a bounded tail buffer.

### Nulls

Null representation must be per column or inherited from a layout default. It may be:

- One or more exact decoded strings.
- One or more exact byte patterns.
- All padding.
- An explicit zoned or packed-decimal sentinel.

All-padding numeric fields currently raise rather than becoming zero, except genuine zero-padded numeric text such as `0000` (`decoy-engine/src/decoy_engine/config/_fixed_width.py:46-52`). Preserve fail-closed behavior unless the layout explicitly declares an all-padding null.

### Numeric representations

A fixed-width column needs separate logical and physical descriptions.

Logical types should include at least:

- `string`
- `integer`
- `decimal`
- `date`
- `timestamp`
- `boolean`
- `binary`

Physical numeric representations should include:

- Plain text.
- Leading sign.
- Trailing sign.
- Zoned decimal.
- Overpunch.
- Packed decimal.

Decimal fields must declare precision where required and an implied scale. For example, bytes representing `0012345` with `scale: 2` produce `123.45`. Float should not be the canonical type for money.

Unsupported sign, encoding, or representation combinations must fail layout validation.

### Dates

A date or timestamp column must declare its format, such as `%Y%m%d` or `%Y%j`. Detection may propose a format from a sample, but execution must use the stored format. Invalid dates must follow the declared error policy.

### Platform model mismatch

The platform has two fixed-width models:

- Legacy `FwDefinition` stores one-based `{name,start,length}` and has no type, padding, or alignment (`decoy-platform/api/models.py:1905-1925`).
- Engine-compatible `FixedWidthLayout` stores zero-based `{name,start,width,type,pad,align}` (`decoy-platform/api/models.py:1969-1995`).

File preview still consumes the legacy shape and renders strings (`decoy-platform/api/files/router.py:272-343`). Job execution uses the engine-compatible layout.

Platform authoring should converge on the engine-compatible layout. A legacy definition may be converted once at the UI or API boundary. The job reader must receive only the canonical model.

### Streaming

A fixed-width reader should expose a batch iterator from the same parser core used by `read_fixed_width`.

For newline-delimited records, batches are formed from complete physical lines. For fixed-length records, the reader consumes exact multiples of the declared physical record size and carries any partial record into the next byte range. Header and trailer rules apply before Arrow batches are emitted.

The iterator must build Arrow arrays directly. It must not accumulate `list[dict]` records and then construct pandas frames.

### Cloud range reads

Cloud behavior follows the physical record model:

- Newline-delimited records use growing sequential ranges and retain an incomplete trailing line.
- Fixed-length records calculate exact aligned byte ranges.
- Fixed-length records with terminators calculate ranges from payload length plus terminator length.
- Trailer records use a final bounded range or a bounded tail buffer.
- Schema sampling reads only the records required by the sample budget.
- Full execution streams ranges or uses the existing stage-once path according to route admission.

Cloud source descriptors must accept fixed-width and preserve the entire layout through staging.

## Parquet

Parquet owns its physical schema. Decoy should read that schema from metadata rather than infer it from sampled values.

Local profiling already reads schema and exact row count from the footer and samples through batches (`decoy-engine/src/decoy_engine/profile/_readers.py:154-180`). Cloud profiling exposes a seekable range adapter so Arrow can fetch footer and row-group data without downloading the full object (`decoy-engine/src/decoy_engine/profile/_cloud_readers.py:43-113`, `:218-249`, `:316-350`). Streaming uses `ParquetFile.iter_batches` (`decoy-platform/api/jobs/streams.py:154-157`).

A Parquet source still needs:

- Locator and credentials.
- `format: parquet`.
- Schema policy: `exact`, `compatible`, or `accept`.
- Optional expected schema fingerprint.
- Column projection, if supported.
- Timestamp and timezone preservation policy.
- Behavior for schema changes between authoring and execution.
- Decryption configuration if encrypted Parquet is added later.

Recommended default:

- Read the file schema from the footer.
- Store an expected schema or fingerprint when the user confirms the source.
- Validate the current footer at run time.
- Preserve Arrow types and metadata.
- Fail before publishing output if the schema violates the stored policy.

This is a footer read, not a full-file preflight scan.

For a dataset composed of multiple Parquet objects, Decoy must define whether schema compatibility is checked per object, per partition, or against dataset metadata. The current single-object source contract does not answer that question.

## Output and target formatting

Input and output contracts are independent. Decoy must not copy a source dialect or fixed-width layout to a target unless the user explicitly requests that behavior.

Current target models accept only CSV and Parquet for local, S3, and GCS destinations (`decoy-engine/src/decoy_engine/config/_targets.py:28-92`). There is no fixed-width target.

Local platform output currently chooses Parquet by `.parquet` suffix and writes every other suffix as default Arrow CSV, regardless of the declared target format (`decoy-platform/api/jobs/v2_cloud_materialize.py:65-101`). Cloud spooling dispatches on the declared `csv | parquet` value (`decoy-platform/api/jobs/v2_cloud_materialize.py:104-133`). Local and cloud output must both dispatch on the validated target contract.

A delimited target must declare:

- Delimiter.
- Quote and escape policy.
- Header emission.
- Encoding and BOM policy.
- Record terminator.
- Null representation.
- Float, decimal, date, and timestamp formatting.
- Final newline policy.

A fixed-width target must declare:

- The complete output layout.
- Record model and encoding.
- Padding and alignment.
- Null representation.
- Decimal, sign, overpunch, packed-decimal, and date formatting.
- Overflow policy.

The recommended overflow policy is `error`. Silent truncation can corrupt identifiers and destroy referential integrity.

A Parquet target should declare:

- Compression codec and level where applicable.
- Row-group sizing.
- Data-page and file-format version if exposed.
- Timestamp coercion policy.
- Whether Arrow schema metadata is preserved.

All writers must be deterministic under the existing parity contract.

## Proposed config contract

### Delimited CSV source

```yaml
sources:
  claims:
    type: s3
    format: csv
    bucket: customer-ingest
    key: claims/2026-10-01.dat
    credentials_ref: claims-read

    dialect:
      delimiter: "|"
      quote_char: '"'
      escape_char: "\\"
      double_quote: true

      header:
        mode: present

      encoding: utf-8
      bom: allow
      record_terminator: crlf
      trim_whitespace: false

      null_values:
        - ""
        - "NULL"

      types:
        mode: all_string
        columns:
          service_date:
            type: date32
            format: "%Y%m%d"
          paid_amount:
            type: decimal128
            precision: 14
            scale: 2

      bad_record: error

    discovery:
      method: bounded_sniff
      sample_bytes: 262144
      sample_records: 200
      confidence: high
      confirmed: true
```

`discovery` is provenance. Runtime readers use `dialect`, not `discovery`.

For a headerless file:

```yaml
header:
  mode: absent
  column_names:
    - claim_id
    - member_id
    - service_date
    - paid_amount
```

A local source uses the same block and changes only the locator:

```yaml
type: file
format: csv
path: data/claims.dat
dialect: ...
```

### Extended fixed-width source

```yaml
sources:
  members:
    type: file
    format: fixed_width
    path: data/members.dat

    layout:
      version: 2
      offset_unit: byte
      encoding: cp037

      record:
        mode: fixed_length
        length: 120
        header_records: 1
        trailer_records: 1
        trailing_bytes: reject

      defaults:
        pad: " "
        align: left
        null_values:
          - raw_hex: "4040404040404040"

      columns:
        - name: member_id
          start: 0
          width: 12
          logical_type: string
          representation: text
          pad: " "
          align: left

        - name: birth_date
          start: 12
          width: 8
          logical_type: date
          representation: text
          format: "%Y%m%d"
          null_values:
            - text: "00000000"

        - name: status
          start: 20
          width: 1
          logical_type: string
          representation: text

        - name: balance
          start: 21
          width: 6
          logical_type: decimal
          representation: packed_decimal
          precision: 10
          scale: 2
          sign: packed

        - name: contribution
          start: 27
          width: 9
          logical_type: decimal
          representation: text
          precision: 9
          scale: 2
          implied_decimal: true
          sign: trailing
          pad: "0"
          align: right

        - name: legacy_code
          start: 36
          width: 5
          logical_type: integer
          representation: zoned_decimal
          sign: overpunch
```

A newline-delimited Latin-1 layout would use:

```yaml
offset_unit: byte
encoding: latin-1
record:
  mode: delimited
  terminator: lf
  header_records: 0
  trailer_records: 0
```

The normalized model should reject packed decimal with character offsets, reject a fixed-length record whose declared columns exceed the record length, and reject an encoding or representation combination the reader cannot implement exactly.

## Compatibility and failure behavior

The schema change is additive at the model level but changes the meaning of omitted settings. Compatibility must therefore be explicit.

For existing CSV descriptors without `dialect`:

- Validation may normalize them to a versioned legacy dialect matching today’s behavior.
- New authoring must always emit the full block.
- A later schema version may require the block.
- Normalization must be visible in `model_dump()` and evidence records.

For existing fixed-width layouts:

- Do not assume that documented byte semantics describe actual stored intent.
- Mark legacy layouts as `offset_unit: character` during import unless they are reviewed.
- Preview migrated layouts against bounded source records.
- Require confirmation before converting to byte offsets.

Parsing errors must identify the source, logical record number, and column without logging raw field content. Target publication remains transactional. A mid-stream parse failure aborts the staged output.

No route may silently:

- Change delimiters.
- Enable or disable quoting.
- Add null tokens.
- Infer new types.
- Change encoding.
- reinterpret offsets.
- skip malformed records.
- choose output format from the filename suffix.

## Phased delivery

Sizes include implementation, contract tests, documentation, and the repository review gate.

| Slice | Size | Deliverable |
|---|---:|---|
| 0. Owner decisions and contract lock | S, 2 to 4 days | Approve field names, defaults, offset unit, typing, null, regex, record, and output decisions. |
| 1. Canonical config models | M, 1 to 2 weeks | Add the dialect model, extended fixed-width model, source and target validation, normalized legacy defaults, and schema-version handling. |
| 2. Shared reader contract | M, 1 to 2 weeks | One local reader boundary for platform and CLI, Arrow-first results, matching profile and execution types, public fixed-width API, and staging metadata preservation. |
| 3. Describe-once authoring | M-L, 2 to 3 weeks | Bounded STORM and `decoy init` detection, confidence, preview, confirm or override, and persisted normalized config. |
| 4. Delimited parity | M, 1 to 2 weeks | Identical local, cloud-staged, full-frame, and chunked semantics. Add logical-record-aware cloud prefix sampling. |
| 5. Fixed-width A10 and layout convergence | L, 3 to 5 weeks | Byte slicing, encoding support, physical record union, canonical platform editor, legacy definition migration, direct Arrow batches, and exact row-count rules. |
| 6. Advanced fixed-width fields | M-L, 2 to 3 weeks | Null patterns, implied decimals, sign modes, overpunch, packed decimal, and formatted dates. |
| 7. Cloud fixed-width and bounded streaming | M-L, 2 to 3 weeks | S3/GCS config parity, aligned ranges, newline carry buffers, trailer handling, and route eligibility. |
| 8. Output formatting | M for delimited, M-L for fixed-width | Declared target dialect, target Parquet options, fixed-width writer, deterministic formatting, and atomic publication. |
| 9. Cross-path certification | M, 1 to 2 weeks | Local and cloud contract fixtures, full-frame and chunked parity, Rust route evidence, passthrough Arrow identity, documentation, and capability matrix. |

Slices 1 through 4 should land before advanced fixed-width representations. They close the current scan, profile, and execution divergence for common CSV files.

## Decisions required from Cam

### 1. CSV block name

Options:

- `format_options`
- `csv_options`
- `dialect`

Recommendation: `dialect`. It identifies the actual contract while retaining `format: csv` as the stable format literal.

### 2. Delimiter scope

Options:

- One character.
- Multicharacter literal.
- Regex.

Recommendation: permit one non-NUL, non-newline byte in the portable execution contract. STORM may retain regex for discovery, but promotion should require conversion to a literal dialect or explicit materialization to Parquet.

### 3. Detection authority

Options:

- Sniff on every run.
- Trust the filename extension.
- Run bounded detection during authoring and store the result.

Recommendation: bounded authoring detection with confidence and confirm or override. Execution never sniffs.

### 4. CSV type policy

Options:

- Infer types on every run.
- Force every column to string.
- Default to string and permit explicit stored column types.

Recommendation: default to Arrow string for identifier stability, with explicit per-column Arrow types. Never let a fresh sample choose execution types.

### 5. Null-token policy

Options:

- Use each library’s defaults.
- Use a global versioned Decoy set.
- Require an explicit set for every source.

Recommendation: normalize a versioned compatibility set into old configs and write an explicit set into every newly authored config. Permit `[]` when no token should become null.

### 6. BOM policy

Options:

- Leave it to the parser.
- Strip recognized BOMs automatically.
- Store an explicit `require | allow | forbid` policy.

Recommendation: store the policy. Detection may identify the BOM, but the confirmed result controls execution.

### 7. Fixed-width offsets

Options:

- Characters.
- Bytes.
- Selectable per layout.

Recommendation: bytes for the canonical v2 layout. Import existing layouts with an explicit legacy character unit until reviewed.

### 8. Physical fixed-width records

Options:

- Newline-delimited only.
- Fixed-length only.
- An explicit record-model union.

Recommendation: an explicit union covering LF, CRLF, fixed length, and fixed length with terminator.

### 9. Encoding scope

Options:

- UTF-8 only.
- Arbitrary Python codec names.
- A curated codec set.

Recommendation: a curated set. Start with UTF-8 and Latin-1. Add required EBCDIC code pages with byte-level fixtures and fixed-length record support.

### 10. Advanced fixed-width representations

Options:

- Defer all advanced representations.
- Accept a generic transform expression.
- Add structured representation fields.

Recommendation: structured fields for text, zoned decimal, overpunch, packed decimal, implied scale, sign placement, null patterns, and date format. Reject combinations not covered by contract tests.

### 11. Header and trailer support

Options:

- Ignore them.
- Fixed leading and trailing counts.
- Conditional record-type layouts.

Recommendation: fixed counts first. Add discriminator-driven layouts only for a concrete customer format.

### 12. Platform layout migration

Options:

- Keep `FwDefinition` and `FixedWidthLayout` indefinitely.
- Translate between them in every reader.
- Make the engine-compatible layout canonical.

Recommendation: make the engine-compatible model canonical. Allow a one-time conversion at the UI or API boundary. Never translate in the execution reader.

### 13. Cloud execution

Options:

- Always stage the full object.
- Always read direct ranges.
- Use a hybrid.

Recommendation: hybrid. Use ranges for Parquet metadata and bounded authoring. Use calculated ranges for fixed-length records. Keep stage-once execution until direct streaming provides identical semantics and stronger operational evidence.

### 14. Delimited output fidelity

Options:

- Support only Arrow writer defaults.
- Mirror every input dialect option.
- Define a documented target subset.

Recommendation: define an explicit target subset and reject unsupported combinations. Do not silently approximate an output dialect.

### 15. Fixed-width output timing

Options:

- Ship with fixed-width input.
- Ship after input and streaming are stable.
- Omit from the current program.

Recommendation: ship it later. Cam must first choose overflow, null, sign, decimal, date, encoding, and terminator policies.

### 16. Full-file preflight

Options:

- Scan the full source before every run.
- Use a bounded sample, then validate incrementally during execution.

Recommendation: no automatic full-file preflight. Use the stored contract, perform bounded authoring checks, validate records as they are consumed, and abort transactional output on the first contract violation.
