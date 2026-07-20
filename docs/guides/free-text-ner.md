# Free-text NER (person names + locations)

`text_redact`'s regex detector catalog covers structured PII (SSNs, emails,
phone numbers, street addresses) but has no regex shape for person names or
place names in free prose -- "Contact Jane Doe in Boston" has no pattern to
anchor on. The `ner` option fills that gap with spaCy named-entity
recognition, feeding detected spans into the same span-splice path the regex
detectors use.

This guide covers activating `ner` on `text_redact`. `text_mask` (synthesizing
detected entities instead of redacting them) is a separate, later capability
-- see `docs/backlog/tx-freetext-ner-implementation.md` (TX-2) if working on
that next.

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
`ner: {...}`) on a `text_redact` column requires the extra; the engine fails
at **plan compile**, not mid-run, with a clear message if it's missing.

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
transition-based decoding, no sampling), so `text_redact` output stays a pure
function of `(input, config, model version)`. A model **upgrade** can change
which spans are found. To protect reproducibility, the plan compiler stamps
the installed model version (`ner_model_version`) onto the column's plan at
compile time; if the installed version at run time no longer matches what was
stamped, the run fails closed with `ner_model_version_mismatch` rather than
silently producing different redactions for the same config and seed. Pin the
model package version in any deployment that needs byte-stable output across
environments.

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

Tests that need the real model are gated with a `pytest.mark.skipif` on
`spacy_installed() and model_installed(DEFAULT_NER_MODEL)` (mirroring the
`ml`/`geo` optional-extra `importorskip` convention elsewhere in the suite) --
they SKIP rather than fail in the extras-free CI environment, and PASS when
the `ner` extra + model are installed.
