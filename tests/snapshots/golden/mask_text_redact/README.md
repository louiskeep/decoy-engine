# mask_text_redact golden fixture

Hand-curated input + expected output for the `text_redact` masking strategy
(engine-v2 MG-2, 2026-05-31).

## Source

Synthetic clinical-style prose. The PHI shapes (names, MRNs, NPIs, phone
numbers, SSNs, emails, addresses) are faker-generated; none of the
identifiers are real. Hospital + clinic names are fictional.

## Use

Re-baseline only when the strategy contract changes intentionally. To
re-generate the expected output after a contract change, re-run the
strategy on the input and review the diff by hand before overwriting
`clinical_notes_output.txt`.

The `clinical_notes_e2e.txt` test cell asserts byte-for-byte parity.

## Detectors active in the baseline

Default set: every detector in `_SPAN_DETECTORS`. The fixture exercises
email + ssn + us_phone + pan + npi + icd10 + iban + url.
