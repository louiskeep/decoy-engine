### Fixed (FPE fallback reason codes, QA-FPE, 2026-11-11)

The `fpe` strategy reported two failures under codes the reason registry did not hold:
`fpe_unencryptable_value` (a value that cannot be encrypted without leaking cleartext or losing the
round trip) and `fpe_checksum_unsupported` (an unsupported checksum scheme, or a value whose length
the scheme refuses). Consumers saw them as `decoy.unregistered_reason`. Both are now registered as
`runtime` reasons (`since` 1.24.0) and the handler emits the registry symbols. The reason sentry also
checks the fallback of a code table's `.get(key, default)`, so a literal default fails the build.
