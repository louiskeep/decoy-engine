### Added (GEN-TYPES E2: `output_type` on generate columns, 2026-11-11)

A generate column can say how its values are printed with `output_type: int`, `decimal(n)` (n from 0
to 18), `date` or `bool`. The option compiles to a declared-casting descriptor, so a column emits
`int64`, `decimal128(38, n)`, `date32` or `bool` on the resident, streamed and pinned routes alike.
`decimal(n)` rounds half-even on the decimal value of the generated number; `int` refuses a fraction
instead of rounding it (use `decimal(0)`). Which types accept which output type is the table in
`generate_option_catalog()`. Same-table readers (`derived`, `formula`) keep reading the generator's
own values, and adding `output_type` to a column does not change the values it draws.

`declared_casting` gains the `boolean/text/v1` format (generation only; declared source columns gain
no boolean type), `round_to_scale`, `generated_value_spec` and `arrow_type_for_spec`. A value that
cannot be cast fails the run with `runtime.output_type_cast_failed`, naming the table, column and
target type but never the value. `config.generate_output_type_unsupported` refuses an output type
the column type cannot carry, and a fixed-width target whose layout carries a column typed
`decimal(n)`, `date` or `bool` (its writer prints text and numbers only). New reasons:
`config.generate_output_type_unsupported`, `runtime.output_type_cast_failed` and
`runtime.declared_boolean_invalid` (all `since` 1.23.0).
