# 05. Keys, determinism and namespaces

Status: plan

## What it covers

The four things that decide whether two runs agree: the job seed
(`global_settings.seed`), the mask secret (`global_settings.mask_secret_ref` or
`run_pipeline(key_provider=...)`), the master key for generation (`--master-key` with
`--key-label`), and the per-column `namespace`. Everything keyed goes through
`derive(key, namespace, source)` (HKDF-SHA256 then HMAC-SHA256).

## Expected behaviour

- Same key, namespace and source value give the same output, across processes, days and
  machines, while `SEED_PROTOCOL_VERSION` is unchanged.
- Same namespace on two columns means the same value masks the same way in both.
  Different namespaces are independent.
- A keyed strategy needs a namespace, and needs an admitted secret of at least 32 bytes.
  A keyed plan with no secret, a `SeedKeyProvider`, or a short key is refused with
  `keyed_strategy_requires_secret` in every release phase.
- The secret is admitted before any source is read.
- The seed alone drives generation and the unkeyed, position-based draws
  (non-deterministic `faker` and `categorical`).
- The master key affects generation only. It has no effect on masking.
- The secret never appears in a plan, a log, an error or evidence.

Always keyed, whatever `deterministic` says: `fpe`, `hash`, `date_shift`, `shuffle`,
`bucket_perturb`, `text_mask`, `code_set`, `grouped_series`, `windowed_date`,
`group_key`, `joint_mask`, `tokenize`. A column with `vault: true` is keyed too. A
strategy the classifier does not recognise is treated as keyed.

## Checks

| Id | Run this | What should happen |
|---|---|---|
| KEY-01 | Mask a table with `hash` twice with the same secret, in two processes. | Byte-identical output. |
| KEY-02 | Same job with a different secret. | Every hashed value differs. |
| KEY-03 | Same job, same secret, different `seed`. | Hashed and FPE values are unchanged. Only seed-driven columns move. |
| KEY-04 | Two tables, the same value in columns sharing a namespace; then the same value under two namespaces. | First pair matches. Second pair differs. |
| KEY-05 | `hash` column with no secret configured at all. | Refused with `keyed_strategy_requires_secret` before any source is read. |
| KEY-06 | `mask_secret_ref: env:NAME` with the variable unset; then set to a 16-byte value. | `missing_mask_secret`; then `weak_mask_secret`. |
| KEY-07 | `mask_secret_ref: ftp:something`; then a malformed `env:` reference containing a recognisable string. | `secret_provider_unknown`; then `bad_secret_ref`. The reference text is not repeated in the error. |
| KEY-08 | Pass `SeedKeyProvider` as `key_provider` for a keyed job. | Refused with `keyed_strategy_requires_secret`. |
| KEY-09 | Config with only unkeyed strategies (`redact`, `bucketize`, non-deterministic `faker`) and no secret. | Runs. No secret is demanded. |
| KEY-10 | Non-deterministic `faker`, same seed and input, two processes; then a different seed. | Identical; then different. |
| KEY-11 | Generate with `--master-key` and `--key-label` on two machines; then change the label. | Byte-identical across machines; different under the new label. |
| KEY-12 | Mask run with `--master-key` set and unset. | Masked output is the same both ways. |
| KEY-13 | Change the `key_version` on an `env:` reference (`?key_version=v2`). | Keyed output changes everywhere. Changing it back restores the original output. |

## Open points

- `docs/determinism.md` still says that with no provider configured `mask_key` falls
  back to `job_seed`. `docs/security/key-derivation.md` and
  `docs/security/secret-providers.md` say that fallback was removed and a keyed plan
  with no secret is refused in every phase. KEY-05 assumes the second. Fix whichever doc
  is stale.
- `examples/runnable_demo.py` uses FPE and the token vault. Confirm it supplies a secret;
  if it relies on the removed fallback it no longer runs.
- `docs/determinism.md` says deterministic `shuffle` needs a namespace and the default
  does not, while the keyed-surface list treats `shuffle` as always keyed. Confirm what
  a default `shuffle` column demands (roadmap row 24d found two runs differing).
- Unicode: text is NFC-normalised before hashing, and the Rust and Python paths can ship
  different Unicode versions. A value with a very new codepoint can hash differently on
  the two paths. The companion result is canonical. See ROUTE-06.
