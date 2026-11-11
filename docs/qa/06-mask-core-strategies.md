# 06. Core mask strategies

Status: plan

## What it covers

The general-purpose column strategies: `hash`, `fpe`, `faker`, `categorical`,
`shuffle`, `redact`, `truncate`, `tokenize` and `passthrough`, plus the pool options on
`faker` (`deterministic`, `cardinality_mode`, `pool_size`, `allow_collisions`).

## Expected behaviour

- **hash**: `derive(key, namespace, value).hex()`, optionally truncated to N hex
  characters. Same value gives the same token within a namespace. Nulls stay null.
  Integer columns hash on the exact integer.
- **fpe**: NIST SP 800-38G FF1 over AES-256. Output keeps the input's shape and
  separators. The domain must be at least 1,000,000 (`radix ** length`); a smaller one
  fails the job with `fpe_unencryptable_domain`. A value with no in-charset characters
  fails with `fpe_unencryptable_value`. Nothing is ever emitted unmasked. Check digits
  can be recomputed (`luhn`, `npi`, `vin`, `isbn13`, `ean13`, `gtin`).
- **faker**: draws from a pool (10,000 values by default). With `deterministic: true`
  and a namespace, the same source value gives the same fake value. Without it, the pick
  follows the seed and row position, so it is repeatable but does not preserve joins.
- **categorical**: remaps onto a fixed list, optionally weighted or taken from the
  profile. Needs a namespace in both modes.
- **shuffle**: permutes the non-null values. The set of values and the null positions
  are unchanged.
- **redact**: every non-null value becomes a fixed string (default `REDACTED`).
- **truncate**: keeps the first or last N characters, optionally padding the dropped
  span with `mask_char`.
- **tokenize**: emits `tok_` plus 32 hex characters and records the pair through a
  caller-supplied vault writer. With no vault it fails closed. String columns only.
- **passthrough**: the column is untouched.

## Checks

| Id | Run this | What should happen |
|---|---|---|
| MASK-01 | `hash` on an SSN column containing nulls and repeated values. | Repeats give the same 64-hex token. Nulls stay null. No token equals its source. |
| MASK-02 | `hash` with `truncate: 12`. | Every token is 12 hex characters and is the prefix of the untruncated token. |
| MASK-03 | `hash` on an integer column with nulls, full-frame and chunked. | Same tokens on both routes. Nulls stay null. `1` and `1.0` do not appear as different inputs. |
| MASK-04 | `fpe` with `charset: digits` on `123-45-6789` style values. | Output is 9 digits with dashes in the same places. Same input gives the same output. Different inputs give different outputs. |
| MASK-05 | `fpe` on a column of 4-digit values. | Job fails with `runtime.fpe_unencryptable_domain`. No output is published. |
| MASK-06 | `fpe` with `charset: digits` on a column containing `N/A`. | Job fails with `runtime.fpe_unencryptable_value`. The value is not passed through. |
| MASK-07 | `fpe` with `checksum: luhn` on valid card numbers; `checksum: npi` on NPIs. | Every output passes the scheme's validator. NPI outputs start with 1 or 2. |
| MASK-08 | `faker` with `provider: person_first_name`, `deterministic: true`, a namespace, on a column with repeats. | Equal source values get equal fake names. Rerun gives the same output. |
| MASK-09 | Same column without `deterministic`, on a source where one value repeats on 1,000 rows. | The repeated value does not map to one fixed name: its 1,000 rows get more than one distinct name. Equal names on different source values can occur by chance. Rerun with the same seed is identical. |
| MASK-10 | Deterministic `faker` on a column with 50,000 distinct values and the default `pool_size`. | **Exploratory.** Record how many distinct outputs come back. Slot collisions are expected (see the open point on collisions). |
| MASK-11 | `faker` with `cardinality_mode: unique` on more rows than `pool_size`; then `unique` with `deterministic: true`. | First fails with `runtime.uniqueness_impossible`. Second is refused as an incompatible combination. |
| MASK-12 | `categorical` with `categories` and `weights` on a source of 100,000 distinct values. | Each output share is within 4 standard errors of its weight, √(w(1−w)/n) with n the distinct source values (about ±0.6 points for a weight of 0.7). The draw is keyed per source value, so repeated values share a category. Nulls preserved. |
| MASK-13 | `shuffle` on a column with nulls. | Sorted non-null output equals sorted non-null input. Null positions are unchanged. Two runs are byte-identical. |
| MASK-14 | `redact` with default and with `redact_with: "XXX"`; `truncate` with `length: 3`, then `keep: tail`, then `mask_char: "*"`. | Outputs are as configured. With `mask_char`, output length equals input length. Nulls preserved. |
| MASK-15 | `tokenize` with no vault writer; then on an integer column. | Both refused with a registered code before any row is masked. |
| MASK-16 | `passthrough` on columns of every Arrow type in the test corpus. | Output column equals the source column, type included. |

## Open points

- **Collisions in deterministic faker.** `docs/strategies.md` says deterministic pool
  strategies preserve cardinality by default (distinct in, distinct out).
  `PoolSampler._deterministic` picks `derive_index(value) % pool_size`, which lets two
  values share a slot. MASK-10 measures what actually happens. If collisions occur by
  default, either the doc or the default is wrong, and `allow_collisions` is not really
  an opt-in.
- `fpe` with a charset of fewer than two characters "degrades to passthrough" per the
  docs. That is unmasked output from a masking strategy; confirm it is refused or at
  least warned about.
- `tokenize` and `top_code` are in the capability matrix but not in
  `docs/strategies.md`, which still says there are seventeen mask strategies. The matrix
  lists 24.
