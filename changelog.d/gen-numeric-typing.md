### Changed (generated numeric columns are typed, GEN-NUMERIC-TYPING, 2026-11-10)

Generated numeric columns are now typed int64/float64 instead of string: an unformatted `sequence`
and a faker provider that returns an int or float (pyint, pyfloat and similar). `derived`
arithmetic and `grouped_series` ordering now work on numbers. Cell text is unchanged for non-null
values; Parquet output has numeric columns. Formatted sequences (prefix, suffix, pad) and other
faker providers stay string. A column stays string when its values cannot be proven to fit int64
(a sequence whose last value overflows, an unbounded or oversized faker int provider), when a
custom provider overrides its name, or when any reader of it needs text: formula references,
aggregate input, group labels, window anchors, statistical conditioning, a `derived` expression
that uses it with `+`, `==` or `in` against strings, or a relationship whose paired column is not
generated with the same type.

An engine release containing this change must ship together with the CLI switch from
`table.to_pandas()` to `generated_table_to_pandas`, or nullable integer columns print as `1.0`.

#### Added: `generated_table_to_pandas`

`decoy_engine.generated_table_to_pandas(table)` converts a generated Arrow table to pandas with
nullable integer dtypes, so a nullable numeric column writes `1` and not `1.0`.
