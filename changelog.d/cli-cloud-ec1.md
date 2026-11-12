Breaking: S3 and GCS connector errors carry fixed text and a `kind`, and `S3Config.region` has no default.

### Changed (one S3 client and classified connector errors, CLI-CLOUD EC1, 2026-11-12)

`ConnectorError` takes a keyword `kind`, one of `access_denied`, `not_found`, `precondition_failed`, `conflict`, `wrong_region`, `pointer_invalid`, `transient`, `credentials`, `rejected` and `unexpected` (`decoy_engine.errors.ConnectorErrorKind`, default `unexpected`). Only `transient` is a `TransientError`; every other kind is a `PermanentError`. S3 and GCS errors no longer carry SDK text, and `__cause__` and `__context__` are empty. GCS maps 412 and 409 to `precondition_failed` and `conflict`.

An S3 code outside the documented tables and below HTTP 500 is now a `PermanentError` that the engine does not retry. The throttling and timeout codes botocore's standard retry mode retries (`Throttling`, `RequestLimitExceeded`, `PriorRequestNotComplete` and the rest) and HTTP 429 and 509 stay `TransientError`. `PreconditionFailed`, `ConditionalRequestConflict` and `PermanentRedirect` were retried before. The profiler's S3 failures are `TransientError` or `PermanentError` instead of `RuntimeError`.

The S3 client is built in one place, with an explicit `total_max_attempts: 2` (the same two requests the old `max_attempts: 1` made). A `single_request` client uses `total_max_attempts: 1` for conditional writes. `S3Config.region` defaults to `None`, so botocore resolves the region from the environment or profile instead of using `us-east-1`.
