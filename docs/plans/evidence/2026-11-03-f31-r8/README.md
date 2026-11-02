# F31 revision 8: getattr rule and the 18 new exc-copy rows

Rules consulted: 00-universal, development-loop, risk-and-exceptions, testing (same set as the plan).

Heads: decoy-platform `origin/main` `0638a97d` (unchanged since revision 7), decoy-engine
`origin/main` `9eec6123` (only #366, a subset fix, since `492f35ac`). Stdlib `ast`, container
Python 3.11.2, NOT the `uv.lock` interpreter; nothing imported from the targets. No test suite run.

| File | What |
|---|---|
| `excbound_r8.py` | the revision 7 allowlist probe plus `getattr(exc, name)`: a text read unless `name` is a string literal in the allowlist |
| `census-r8-0638a97d.txt` | pass 1 56 rows, pass 2 72 rows: 128 total (revision 7: 110) |
| `selftest_r8.py` | seeded `getattr` fixtures |
| `selftest_r8-r7-probe.txt` | 2 MISS, exit 1 (red on the revision 7 probe) |
| `selftest_r8-r8-probe.txt` | all ok, exit 0 |

Determinism (Dennis L5): the census was run with `PYTHONHASHSEED=1` and `=987`; the two outputs
are byte-identical (`cmp`). The revision 7 probe is also seed-independent and reproduces
`../2026-11-02-f31-r7-fresh/census-allow-0638a97d.txt` byte for byte.

## The 18 new rows, classified at `0638a97d`

| Site (api/...) | Read | Decision |
|---|---|---|
| jobs/pinned_download.py:70 | `getattr(exc, "response")` AWS error code, compared to a closed set | none |
| pipelines/preflight.py:167 | `getattr(exc, "key")`, a variable key (owner name) | none |
| cloud/_pg_probe_child.py:28 | `getattr(exc, "sqlstate")`, a SQLSTATE code | none |
| cloud/backend_errors.py:40 (two chains) | `getattr(exc, "response")` HTTP status int | none |
| cloud/conditional.py:419 (two chains) | same HTTP status int | none |
| jobs/cloud_range_reader.py:107 (six chains) | `getattr(exc, "response")` HTTP status inside `is_transient`, returns a bool | none |
| jobs/cloud_range_reader.py:297 (two chains) | AWS error code, compared to a closed set | none |
| jobs/target_locks.py:90 (two chains) | `getattr(exc, "orig")` driver class check, returns a bool | none |
| scheduler/queries.py:109 | `getattr(getattr(exc, "orig"), "sqlstate")`, compared to a closed set | none |

None needs V1 work. After P4d (6 rows) and P4e (6 rows: `preview_http.py:52` x2,
`preview_slot.py:121`, `retrieval_http.py:102`, `:103`, `retrieval_slot.py:133`), 116 rows remain.
