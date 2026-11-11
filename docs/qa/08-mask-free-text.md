# 08. Free-text strategies

Status: plan

## What it covers

`text_redact` and `text_mask`, which find PII inside a cell of prose and change only
those spans, and the optional spaCy NER for person names and locations.

## Expected behaviour

- **text_redact** replaces each matched span with a token (default `[REDACTED]`, or
  `[REDACTED:<detector>]` with `label_token`). An empty or missing `detectors` list runs
  every built-in span detector. It never means "redact nothing".
- **text_mask** sends each span to a per-detector strategy (`fpe`, `faker`,
  `date_shift`, `redact`, `passthrough`), so an SSN becomes another SSN and an email can
  be redacted.
- Eleven detectors fire on their own (tier 1): `email`, `ssn`, `us_phone`, `us_zip`,
  `pan`, `iban`, `ipv4`, `icd10`, `npi`, `url`, `street_address`. Fifteen others (names,
  dates, free-text addresses, MRN and more) fire only when spans are supplied by NER.
- `unmatched_span_policy` defaults to `redact`: text no detector matched is replaced
  too. `passthrough` leaves it and warns on every cell.
- The same matched text gives the same replacement in every cell.
- A span below FF1's minimum domain (a bare 5-digit ZIP) needs `sub_floor_span` set to
  `redact` or `synthetic`. Unset, the job fails the first time such a span is matched.
- Overlapping spans: leftmost wins, then longest.
- Matched text is never written to logs or evidence.
- NER output is deterministic per model version, and the plan records the version.

## Checks

| Id | Run this | What should happen |
|---|---|---|
| TEXT-01 | `text_redact` on notes containing an email, an SSN, a phone number and a street address. | All four spans become `[REDACTED]`. The surrounding words are unchanged. |
| TEXT-02 | `text_redact` with `detectors: []`. | Every built-in detector runs. The PII is redacted. |
| TEXT-03 | `text_redact` with `label_token: true`. | Spans read `[REDACTED:email]`, `[REDACTED:ssn]` and so on. |
| TEXT-04 | `text_redact` with no `ner` on a note containing "Dr. Maria Chen". | The name is not redacted. Confirm the docs and any run summary make that limit clear. |
| TEXT-05 | `text_redact` with `ner: true` and the model installed; then without the model. | With: the name is redacted. Without: a clear model-unavailable refusal, not a silent run without NER. |
| TEXT-06 | `text_mask` with defaults on the TEXT-01 notes. | Tier-1 spans are replaced by their default strategies. All other text is replaced by the token. |
| TEXT-07 | `text_mask` with `unmatched_span_policy: passthrough`. | Prose is kept. A warning is emitted for each cell saying only the tier-1 detectors ran. |
| TEXT-08 | `text_mask` with `per_detector_strategy: {ssn: fpe}` on two cells holding the same SSN. | Both cells get the same replacement SSN, in SSN shape. |
| TEXT-09 | `text_mask` with `us_zip: fpe` on text containing `90210`, with `sub_floor_span` unset. | Job fails closed on the first match. Nothing is published. |
| TEXT-10 | Same with `sub_floor_span: synthetic`, and a cell holding `90210-1234`. | The 5-digit ZIP becomes a synthetic valid ZIP with a `text_mask_sub_floor_span_handled` warning. The ZIP+4 goes through FF1. |
| TEXT-11 | A cell where an email contains a URL-like substring. | One replacement, for the leftmost then longest span. No doubled or nested tokens. |
| TEXT-12 | Run TEXT-06 with debug logging on, then search logs and evidence for a known SSN from the source. | Absent everywhere. |

## Open points

- `text_mask` says unknown detector ids are "skipped silently". A typo in a detector
  name then silently masks less than intended. Decide whether V1 should refuse it.
- The docs say the `ner:` key is not yet wired into the `text_mask` column handler
  (deferred), and that names reach `text_mask` only through `extra_spans=` in code.
  Confirm the current state, since `text_redact` does take `ner:`.
- The docs give tier 1 as 11 ids and the full table as 26, while the capability matrix
  lists 25 detectors. Reconcile the counts.
- `what-we-cannot-prove.md` states the engine does not guarantee semantic correctness of
  free text. QA should record recall on a labelled sample, not treat it as pass or fail.
