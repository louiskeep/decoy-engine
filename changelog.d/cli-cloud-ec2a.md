### Changed (S3 put path and SDK floors, CLI-CLOUD EC2a, 2026-11-12)

S3 client construction and body reads now go through the same classified-error path as every other S3 call. An invalid endpoint, half-set credentials, an unknown profile, a read timeout, a dropped connection or a truncated body leaves as a `PermanentError` or `TransientError` with fixed text, a `kind` and no `__cause__` or `__context__`; before, the raw botocore or `ValueError` escaped. `S3FileSource.check()` and `S3FileSink.check()` return `ok=False` on a construction error instead of raising.

`S3FileSink` writes through one `s3_put` in `connectors/_clients.py` (same 5 MiB parts, same abort on failure). The `cloud` extra needs `boto3>=1.43.36` (the first release checked for conditional-write parameters) and the dev extra `moto[s3]>=5.2.2`.
