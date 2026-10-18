Status: record

# Arrow-native FK resolve and string write-back for the mixed route (2026-10-18)

Plans: `docs/plans/2026-10-18-arrow-native-fk-resolve.md` and
`docs/plans/2026-10-18-arrow-fk-writeback.md`. Code: `execution/_mixed_fk_bridge.py`.

## What changed

1. Resolve. `_resolve_arrow` resolves child keys against the generate parent with pyarrow
   compute (`index_in`, `take`, `if_else`) instead of per-row Python tuples and a parent dict.
   Integer keys of one signedness and string keys are admitted; other shapes delegate to
   `_resolve_python`. Orphan handling (FAIL raises, REMAP rejected, WARN counts) runs before
   any write-back.
2. Write-back. String keys no longer go through `to_pylist()` and the pandas round trip. The
   resolved column is `pa.nulls(n)` when empty or all null, else cast to `string`
   (large_string input included). This matches the oracle's pandas-object round trip.
   Integer keys keep `_write_back_fk_column`, because its value-dependent int widening is the
   fragile case and integers are not the memory bottleneck.

## Evidence

- Differential suite (`tests/unit/execution/test_arrow_fk_resolve_differential.py`) compares
  values, dtype, warnings, errors, and canonical Arrow IPC bytes against the Python resolve.
- F2 mixed parity and sweep stay byte-identical with identical warning signatures.
- F4 10M tier (2M parent, 8M child, 1% orphans, external `wait4` peak RSS, 3 runs):

| Variant | Peak RSS |
|---|---|
| Baseline (Python tuple resolve) | 2009 MB |
| Arrow resolve, `to_pylist` write-back | 1906 MB |
| Arrow resolve, Arrow string write-back | 1419 MB (1418-1420) |

The empirical target of 1000 MB was not met. The carrier phase high-water mark is about
1015 MB; the rest of the peak comes from the chunked mask lane, not the FK write-back. The
100M projection is therefore lower than before but still needs the GCP run to confirm.
