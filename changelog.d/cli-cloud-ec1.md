Breaking: S3 and GCS connector errors carry fixed text and a `kind`, S3 no longer retries inside the SDK, and `S3Config.region` has no default.

### Changed (one S3 client and classified connector errors, CLI-CLOUD EC1, 2026-11-12)

`ConnectorError` takes a keyword `kind`, one of `access_denied`, `not_found`, `precondition_failed`, `conflict`, `wrong_region`, `pointer_invalid`, `transient`, `credentials`, `rejected` and `unexpected` (`decoy_engine.errors.ConnectorErrorKind`, default `unexpected`). Only `transient` is a `TransientError`; every other kind is a `PermanentError`. S3 and GCS errors no longer carry SDK text, and `__cause__` and `__context__` are empty. GCS maps 412 and 409 to `precondition_failed` and `conflict`.

An S3 code outside the documented tables and below HTTP 500 is now a `PermanentError` that the engine does not retry. `PreconditionFailed`, `ConditionalRequestConflict` and `PermanentRedirect` were retried before. The profiler's S3 failures are `TransientError` or `PermanentError` instead of `RuntimeError`.

The S3 client is built in one place, with one request per call and no SDK retry (`total_max_attempts: 1`; the old `max_attempts: 1` allowed one SDK retry). `S3Config.region` defaults to `None`, so botocore resolves the region from the environment or profile instead of using `us-east-1`.
