# FF1 test vector provenance

Round-2 BLOCKER-2 remediation (Codex FINAL gate, `feat/native-phase5-ff1`):
`ff1_wycheproof_kat.json` carried no provenance record and no ACVP cases. This
file is that record, for all three committed corpora. Every claim below was
re-verified against the live upstream source while writing this file (byte-
for-byte content diff for Wycheproof, a fresh production-primitive run for
ACVP), not carried over from memory of the original import.

## 1. `ff1_wycheproof_kat.json` (282 cases, valid KATs)

- **Upstream**: [C2SP/wycheproof](https://github.com/C2SP/wycheproof),
  `testvectors_v1/aes_ff1_base10_test.json`,
  `testvectors_v1/aes_ff1_base36_test.json`,
  `testvectors_v1/aes_ff1_base62_test.json`,
  `testvectors_v1/aes_ff1_radix64_test.json`.
- **Commit pinned** (last commit to touch each file, verified via the GitHub
  commits API, 2026-09-12): `base10`/`base36`/`base62` at
  `770c84ebf74dbcaa1c0109b5a0d74014b187522f` (2026-08-11); `radix64` at
  `3b1f1993d20ef710c67b2daada68d39ec6a0d53a` (2026-08-11).
- **License**: Apache License 2.0 (`C2SP/wycheproof` repository license).
- **Selection recipe** (reverse-verified field-for-field against a fresh
  fetch of the four source files; every one of the 282 committed vectors
  matches its source `tcId`'s `key`/`tweak`/`msg`/`ct` exactly, byte for
  byte): from the pool of `result: "valid"` test cases with `keySize: 256`
  across the four files (**3,317 candidates** -- 1,106 + 809 + 713 + 689 per
  file), take the first *N* cases **in ascending `tcId` order** for each
  distinct message length (`msgSize`) present in that pool, where *N* is 1
  or 2 for most lengths and up to 4 at `msgSize == 6` (the first length
  clearing the FF1 minimum-domain floor at radix 10, where Wycheproof itself
  carries more cases per length). This yields one-or-few representative
  cases per length across the full `msgSize` range the source provides
  (2-260), rather than every case at every length.
- **Skipped-case accounting**: of the 3,317 AES-256/valid candidates,
  3,035 were not selected -- every one is a *further* case at a `msgSize`
  already represented by an earlier (lower-`tcId`) case, so skipping them
  loses per-length redundancy, not length-range coverage. Separately
  excluded outright (not part of the 3,317 pool): every `keySize: 128`
  and `keySize: 192` group (3,372 and 3,309 valid tests respectively across
  the four files, verified against the pinned upstream commit below -- the
  deployed profile is AES-256 only, `FF1_KEY_BYTES == 32`);
  every `result: "invalid"`-flagged case (`InvalidMessageSize`,
  `InvalidKeySize`, `InvalidPlaintext`, and the malformed non-`FpeStrTest`
  auxiliary groups) -- see `ff1_wycheproof_invalid_kat.json` below, which
  pulls exactly this excluded class back in for negative-input coverage.
- **`kind` field**: `"str"` for base10/base36/base62 (Wycheproof encodes
  `msg`/`ct` as alphabet strings for these radixes); `"list"` for radix64
  (Wycheproof encodes `msg`/`ct` as raw numeral arrays already, since no
  single-character alphabet exists above radix 94 in its scheme and 64 is
  encoded as a list regardless). `tests/unit/transforms/test_ff1_primitive.py`
  branches on this field the same way for both corpora below.

## 2. `ff1_wycheproof_invalid_kat.json` (44 cases, malformed-input KATs)

New in round 2 (BLOCKER-2c: committed malformed-input boundary coverage).
Same upstream, license, and four source files as above, same commit pins.
Pulls every `result: "invalid"` case flagged `InvalidKeySize` (20 cases: key
sizes such as 0/8/64/160/320 bits, none of which is 128/192/256) or
`InvalidMessageSize` (24 cases: an empty message, or a single-numeral
message -- both violate the `n >= 2` NIST FF1 precondition `_ff1.py`
enforces directly), across all four files (20 + 24 = the 44 committed cases;
counts verified against the committed file, `flags` field). Every one of the 44 was replayed
against the production `_ff1.encrypt` while writing this file: all 44 raise
`Ff1Error`, none silently succeeds. `InvalidPlaintext` (an out-of-alphabet
character) is Wycheproof's third invalid-input flag but is NOT pulled in
here: it is a wrapper/charset-resolution concern (`transforms/fpe.py`
resolves the string-to-numeral mapping before the raw primitive ever sees
it), not a raw-primitive precondition `_ff1.py` itself enforces -- the
primitive-level equivalent (a numeral value outside `[0, radix)`) is covered
by hand-authored cases in `test_ff1_primitive.py` instead (the BLOCKER-1a
mutation-kill test), since Wycheproof's alphabet-level framing doesn't
translate to a numeral-list input directly.

## 3. `ff1_acvp_aes256_kat.json` (250 cases, valid KATs)

- **Upstream**: [usnistgov/ACVP-Server](https://github.com/usnistgov/ACVP-Server),
  `gen-val/json-files/ACVP-AES-FF1-1.0/prompt.json` (plaintext/ciphertext
  inputs) and `.../expectedResults.json` (the matching expected outputs).
- **Commit pinned**: `master` at `975de31eb83d87039ec88934fdc47d8c312b892d`
  (fetched 2026-09-12). File digests at that commit: `prompt.json`
  SHA-256 `517e29bbef07e9ba5b16453178fc6901df82b36133e8726e7cd5182fb911c956`;
  `expectedResults.json` SHA-256
  `4eef9826755a77b4d8de235f17e35e1b69eb11a12bca7cafe0f8e4e5772055e2`.
- **License**: NIST-developed software / test-vector notice (see the
  repository README's "License" section): freely usable, copyable, and
  distributable, with attribution to NIST; NIST-developed software is not
  subject to US copyright and is provided "AS IS" with no warranty.
- **Why this corpus**: this is the real ACVP AES-FF1 vector set the plan
  asked for (round-1 could not reach a live, session-authenticated ACVP
  server and substituted Wycheproof alone; this fetch used the public
  `ACVP-Server` GitHub mirror's static gen-val JSON files directly, no
  session needed).
- **Selection recipe**: `prompt.json`/`expectedResults.json` cover 30 test
  groups (`keyLen` in {128, 192, 256} x `radix` in {2, 4, 16, 32, 64} x
  `direction` in {encrypt, decrypt}, 25 cases each, 750 total). The
  committed subset is every one of the 10 `keyLen: 256` groups in full (the
  deployed profile is AES-256 only) -- **250 cases**, `radix` in
  {2, 4, 16, 32, 64}, `msgSize` from 10 to 512 numerals (crossing both the
  deployed `FF1_MAX_LEN == 256` boundary and the `d > 16` S-block-expansion
  boundary), both directions. Nothing beyond the keyLen filter was dropped:
  every AES-256 case in the source pair is committed here.
- **`msg`/`ct` orientation**: an `encrypt`-direction group's `tcId` supplies
  `pt` in `prompt.json` and the matching `ct` comes from
  `expectedResults.json`; a `decrypt`-direction group's `tcId` supplies `ct`
  in `prompt.json` and the matching `pt` (stored here as `msg`) comes from
  `expectedResults.json`. The `direction` field records which.
- **Verification**: every one of the 250 cases was replayed against the
  production `_ff1.encrypt`/`_ff1.decrypt` while writing this file (both
  directions, independent of the case's own recorded `direction`): all 250
  match exactly, 0 failures.

## Committed-file digests (reproducibility anchor)

The exact bytes under test are pinned by SHA-256 of each committed corpus (the
upstream sources are pinned by the commit hashes above; these digests pin the
derived, committed vectors so a reviewer can confirm nothing drifted):

```
ff1_wycheproof_kat.json          a9e6e8ecbdc6f952568c9c92aedc2fb67d4b5531f3f0427daafca00c14b612f7
ff1_wycheproof_invalid_kat.json  c5009c4a5d5dbf8e7dcbe112a06d2e264cc02b8f05954fc3dcdd5231e2ba45c9
ff1_acvp_aes256_kat.json         78ca4bec0b0ef9682eee8f0a14b75dc62faa4fb7bacd782442689efcaf8d2559
```

Recompute with `sha256sum tests/vectors/ff1_*.json`. Because the selection is a
subset heuristic (documented above) rather than a fully re-derivable algorithm,
the committed file plus its digest is the reproducible artifact of record; each
vector's `src`/`tcId` maps back to the pinned upstream file for independent
re-verification.
