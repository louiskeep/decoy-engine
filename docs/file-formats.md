# File formats: Avro and ORC

Status: current (built ahead on the V1+ track; not merged).

Besides CSV, Parquet and fixed-width, a local `file` source or target can be `format: avro` or
`format: orc`. S3 and GCS sources and targets take only CSV and Parquet. The readers and writers
live in `decoy_engine.io`; the CLI wiring comes later (V1P-4.2 in the CLI repo).

```yaml
sources:
  events: {type: file, format: avro, path: in/events.avro}
  orders: {type: file, format: orc,  path: in/orders.orc}
targets:
  events: {type: file, format: avro, path: out/events.avro}
  orders: {type: file, format: orc,  path: out/orders.orc}
```

## API

| Function | What it does |
|---|---|
| `iter_avro_chunks(path, budget, *, table)` / `iter_orc_chunks(...)` | Owned `pa.Table` chunks within a `ChunkBudget` (bounded memory) |
| `read_avro(path, *, table)` / `read_orc(...)` | The whole file as one table |
| `write_avro(data, path, *, table, schema=None)` / `write_orc(...)` | Write one table, or an iterable of chunks cast to `schema` (required for an iterable: pass the source schema); publishes atomically |

Every file is opened through the file-access policy. `profile_source` reads both formats (row
counts come from the ORC file tail and the Avro block headers: exact, but claims the file makes).

## Reading

**Avro.** The engine frames the container itself (header, blocks, sync markers, codecs) and hands
Polars one group of whole, already decompressed blocks at a time. Codecs: `null`, `deflate`,
`snappy`. On the chunked route a block larger than the chunk budget is refused
(`avro_block_too_large`: raise the budget or read without chunking), and nested fields are
refused; the whole-file read and profiling take both (blocks up to 256 MiB).

| Avro | Arrow |
|---|---|
| `boolean`, `int`, `long`, `float`, `double`, `bytes`, `string` | `bool`, `int32`, `int64`, `float32`, `float64`, `binary`, `string` |
| `date` | `date32` |
| `timestamp-millis` / `-micros` | `timestamp[ms|us, tz=UTC]` |
| `local-timestamp-millis` / `-micros` | `timestamp[ms|us]` |
| `decimal` (bytes or fixed, precision 1 to 38) | `decimal128(p, s)` |
| `uuid` (on string) | `string` |
| `time-millis` / `time-micros` | `time64[ns]` |
| `enum` | `string` |
| `fixed` | `binary` |
| `record`, `array` | `struct`, `list` (whole-file read only) |
| `[null, T]` | nullable `T` |

Refused (`avro_schema_unsupported`): `map`; unions with more than one non-null branch or a single
branch; `duration`; `timestamp-nanos`, `local-timestamp-nanos`; a decimal outside 1 to 38 or
larger than its fixed size holds; `uuid` on fixed; a known logical type on the wrong base type;
any named-type reference (Polars does not resolve them); records with no fields; fixed of size 0;
codecs other than the three above. An unknown logical type is ignored, as the spec says.

**ORC.** Read through Arrow's ORC reader, below the stripe. A file with a timestamp column is
refused (`orc_type_unsupported`): pyarrow reads every ORC timestamp as `timestamp[ns]` and wraps a
value outside 1677-09-21 to 2262-04-11 into a wrong date without an error, and no installed
library exposes the column statistics that could prove a file in range.

## Writing

| Arrow | Avro | ORC |
|---|---|---|
| `bool`, `int32`, `int64`, `float32`, `float64`, `string`, `binary` (and `large_*`) | as is | as is |
| `int8`, `int16`, `uint8`, `uint16` | `int` | `int8`/`int16` as is; unsigned widened |
| `uint32`; `uint64` (every value must fit int64) | `long` | `int64` |
| dictionary | decoded | decoded |
| `date32` | `int` + `date` | `date` |
| `timestamp[ms|us]` | `local-timestamp-millis|micros` | `timestamp`, values 1677-09-21..2262-04-11 only |
| `timestamp[ms|us, tz=UTC]` | `timestamp-millis|micros` | same range rule |
| `timestamp[ns]` | `micros`, when no value has sub-microsecond digits | as is |
| other time zones | refused | same range rule; zone becomes UTC |
| `decimal128(p, s)` | `bytes` + `decimal(p, s)` | `decimal(p, s)` |
| `time`, `duration`, `null`, `decimal256` | refused | refused |
| nested | refused | written when every leaf is written as is |

A refused column is `target_column_type_unsupported` (column and type named, never a value); a
chunk that does not cast to `schema` is `target_schema_mismatch`. Nothing is published on a
refusal and an existing target is left as it was.

The Avro header is the engine's: record `topLevelRecord`, codec `null`, field names
`[A-Za-z_][A-Za-z0-9_]*` and unique. Blocks hold about 256 KiB each, so a modest chunk budget
reads the file back. The sync marker is derived from the schema and the first block: the same
data gives the same bytes. ORC output bytes are deterministic too.

Avro to Avro keeps every admitted type except: `enum` becomes `string`, `fixed` becomes `bytes`,
fixed-decimal becomes bytes-decimal, `uuid` loses its annotation, and `time-*` and nested fields
are refused on write.

## Routes

| Route | Avro / ORC |
|---|---|
| Chunked (`run_mask_chunked`, `run_mask_pipeline_chunked`) | Supported, fed by `iter_*_chunks` |
| Full frame, sequential | Supported with a resident table from `read_*` |
| Unified slice | Not admitted; the legacy route runs instead (same output) |
| Out-of-core FK | Parquet only, as for CSV and fixed-width |
| Subset | Parquet only (refused at config validation) |
| Declared casting, binary contracts | Refused with the existing codes |

## Limits

See [What Decoy does not prove](what-we-cannot-prove.md#avro-and-orc-files).
