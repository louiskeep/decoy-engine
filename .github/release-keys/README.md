# Release public key (native companion wheels)

`decoy-release-key.pub.pem` is the engine's copy of the Decoy release public key:
the Ed25519 key whose private half (`DECOY_RELEASE_KEY_PEM`, an environment secret
of the `native-release` GitHub Environment) signs every companion wheel and the
`SHA256SUMS` manifest in `.github/workflows/native-companion-release.yml`.

It is a PIN, not a convenience: the release job verifies every `.sig` against THIS
file (`scripts/release/native_release_checks.sh verify-signatures`), and first
asserts the secret's public half equals it (`assert-key-matches`), so a release can
never be signed with a key the installers will not trust. A key derived from the
secret inside the job would prove nothing.

Until launch it holds the `__DECOY_RELEASE_PUBLIC_KEY__` placeholder, exactly like
`src/decoy/distribution/release_key.pub.pem` in the CLI repo; both copies are filled
with the same public key at launch, in the same release step. The workflow refuses
to publish while the placeholder is present.
