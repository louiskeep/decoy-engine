# Free-text NER (person names + locations)

`text_redact`'s regex detector catalog covers structured PII (SSNs, emails,
phone numbers, street addresses) but has no regex shape for person names or
place names in free prose -- "Contact Jane Doe in Boston" has no pattern to
anchor on. The `ner` option fills that gap with spaCy named-entity
recognition, feeding detected spans into the same span-splice path the regex
detectors use.

This guide covers activating `ner` on both `text_redact` (which replaces
detected spans with a token) and `text_mask` (TX-2, which SYNTHESIZES a
plausible replacement -- a fake name or place -- instead of redacting). The
`ner` config is identical on both strategies; the difference is what the
detected span becomes. The synthetic-value cross-cell caveat below applies to
the `text_mask` path only.

## Install

`spacy` ships behind the optional `ner` extra; the language model is a
separate download (not pip-resolvable, so it isn't pulled in automatically):

```
pip install 'decoy-engine[ner]'
python -m spacy download en_core_web_sm
```

Without this extra installed, `decoy_engine` imports and runs normally --
`ner` is off by default and every module that touches spaCy imports it lazily
(function-local, not at module top level). Only setting `ner: true` (or
`ner: {...}`) on a `text_redact` or `text_mask` column requires the extra; the
engine fails at **plan compile**, not mid-run, with a clear message if it's
missing (row-13 check for text_redact, row-30 for text_mask).

## Enable it

Set `ner: true` in a `text_redact` column's `provider_config`:

```yaml
tables:
  - name: notes
    columns:
      - name: clinical_notes
        strategy: text_redact
        provider_config:
          ner: true
          label_token: true   # optional: emit [REDACTED:person_name] etc.
```

`ner` also accepts a dict to pick a non-default model or restrict which
entity types run:

```yaml
provider_config:
  ner:
    model: en_core_web_sm       # default; any installed spaCy NER model works
    entities: [person_name]     # optional: omit to detect both person_name + location
```

To SYNTHESIZE the detected entities instead of redacting them, use `text_mask`
with the same `ner` config -- detected `person_name` spans become a fake name,
`location` spans become a fake city (per `DETECTOR_DEFAULTS`), and the
surrounding prose is handled by `unmatched_span_policy` as usual:

```yaml
tables:
  - name: notes
    columns:
      - name: clinical_notes
        strategy: text_mask
        provider_config:
          ner: true
          unmatched_span_policy: passthrough   # keep known-safe prose verbatim
```

Non-English models work through the same key -- `de_core_news_sm`,
`es_core_news_sm`, the multilingual `xx_ent_wiki_sm` -- because both the
OntoNotes label scheme (`PERSON`/`GPE`/`LOC`/`FAC`, used by English models)
and the WikiNER scheme (`PER`/`LOC`, used by most non-English models) map onto
the same `person_name`/`location` detector ids. Install any additional model
the same way: `python -m spacy download de_core_news_sm`.

## What it detects

| spaCy label(s)         | Decoy detector id | Notes |
|-------------------------|--------------------|-------|
| `PERSON`, `PER`         | `person_name`      | |
| `GPE`, `LOC`, `FAC`      | `location`         | |

Other entity labels (`ORG`, `DATE`, `MONEY`, ...) are deliberately excluded in
v1: redacting them shreds legitimate prose for little PII value, the same
rationale the regex catalog uses for name-hint-only detectors. Dates and
addresses are tracked separately as TX-3 (not started -- see the backlog doc
above); do not treat their absence here as a bug.

NER spans merge into the same leftmost-longest overlap resolution as the
regex detectors (`storm.detectors.iter_spans(..., extra_spans=...)`), so a
regex hit (e.g. an SSN) and an NER hit never double-splice the same text.

## Cost

NER inference runs once per non-null cell (`iter_ner_spans` inside the
handler's row loop) plus a one-time ~1s model load per process. It is opt-in
and off by default for that reason -- turn it on for columns that actually
carry free-text prose, not blanket-enabled across every text_redact column.

## Determinism

spaCy NER inference is deterministic for a pinned model version (greedy
transition-based decoding, no sampling), so both `text_redact` and `text_mask`
output stay a pure function of `(input, config, model version)`. A model
**upgrade** can change which spans are found. To protect reproducibility, the
plan compiler stamps the installed model version (`ner_model_version`) onto the
column's plan at compile time; if the installed version at run time no longer
matches what was stamped, the run fails closed with `ner_model_version_mismatch`
rather than silently producing different output for the same config and seed.
Pin the model package version in any deployment that needs byte-stable output
across environments. Per-cell reproducibility and in-core/out-of-core (streaming)
parity hold for every span source, including NER.

### Cross-cell synthetic consistency (text_mask + NER only)

`text_mask` normally guarantees that the same real value in any two cells masks
to the same synthetic value: the span mask key is `HMAC(mask_key, matched_text)`,
which is context-free (see `transforms/text_mask.py`). That guarantee holds in
full for the regex detectors, and the KEY is still context-free under NER too.

NER adds one caveat, because it is the first context-SENSITIVE span source. NER
assigns the entity TYPE (`person_name` vs `location`) from surrounding context,
and `text_mask` picks the synthesis method (fake name vs fake city) from that
type. So an ambiguous surface string -- "Jordan" as a person in one cell and a
place in another -- can synthesize to two different values, even though the span
key is identical in both cells. This is a property of context-dependent entity
typing, not a break in key/mask determinism: for unambiguous entities the
synthetic stays consistent across cells, and every run is still fully
reproducible for the same `(input, config, model version)`. If you need
guaranteed cross-cell consistency for a column, prefer `text_redact` (all
matches become the same token) or a structured detector over free-text NER.

## Demo

`examples/ner_redaction_demo.py` runs `text_redact` with `ner: true` over a
few free-text notes end to end and prints source vs. redacted output. It
requires the `ner` extra + `en_core_web_sm` installed; run it with:

```
python examples/ner_redaction_demo.py
```

## Tests

- `tests/unit/storm/test_ner_spans.py` -- span detection, entity filtering,
  determinism, the compile-time availability check, the locale label-scheme
  mapping, and `text_redact` end to end via `TextRedactHandler`.
- `tests/unit/execution/test_text_redact.py` -- the `TestF14bNerVersionGuard`
  cell locks the version-drift guard (no spaCy needed, the guard runs before
  any model call) plus a real-model smoke test gated on spaCy being
  installed.
- `tests/unit/execution/test_text_mask_ner.py` (TX-2) -- the `text_mask` NER
  path: span routing to faker synthesis, the version-drift guard, the
  compile-time availability check, the `ner_model_version` stamp, and a
  real-model synthesis smoke test.
- `tests/parity/test_out_of_core_group_c_parity.py::test_text_mask_ner_path_parity`
  (TX-2) -- in-core vs out-of-core NER synthesis is byte-identical at multiple
  batch sizes.
- `tests/unit/transforms/test_text_mask.py::TestLocationDefault` (TX-2) -- an
  injected `location` span synthesizes rather than falling to the redact
  fallback.

Tests that need the real model are gated with a `pytest.mark.skipif` on
`spacy_installed() and model_installed(DEFAULT_NER_MODEL)` (mirroring the
`ml`/`geo` optional-extra `importorskip` convention elsewhere in the suite) --
they SKIP rather than fail in the extras-free CI environment, and PASS when
the `ner` extra + model are installed.
