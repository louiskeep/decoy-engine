# All-null chunk breaks string-output coarsening strategies (chunked route)

Status: open, low priority. Discovered 2026-07-17 during the HC-3(b) `top_code`
Dennis re-gate.

## Symptom

On the chunked masking route (`run_mask_pipeline_chunked`), if an entire chunk
of a column masked by a **string-output coarsening strategy** (`bucketize`,
`top_code`) contains only nulls, `pa.concat_tables` raises:

```
ArrowInvalid: Schema at index N was different: age: string vs age: null
```

The all-null chunk's output is an all-null pandas object column, which
`pa.Table.from_pandas` infers as Arrow `null` type; that will not concat with
the `string`-typed output of the chunks that held real values.

## Scope / severity

- **Pre-existing**, not introduced by `top_code`: `bucketize` reproduces it
  identically (verified). Any strategy whose output column is a Python `str`
  object column is affected; value-keyed strategies that stay numeric/fixed-type
  are not.
- **Narrow**: it requires a whole chunk to be 100% null. Chunks are typically
  large, so this needs either a tiny chunk size or a long all-null run aligned
  to a chunk boundary. A column with any real value in every chunk is fine (the
  HC-3(b) `TestChunkSafety` parity test covers boundaries >=2).

## Fix direction (when picked up)

Pin the output Arrow schema for a coarsening/string-output column from the plan
(the strategy's declared output type is `string`) so an all-null chunk emits a
`string`-typed all-null column instead of a `null`-typed one, rather than
letting `pa.Table.from_pandas` infer per-chunk. Do it once in the chunked
emission path so every string-output strategy benefits, not per strategy.
