Breaking: a profile's `generated_at` ends `+00:00` instead of `Z`; a stored profile with `Z` still loads.

### Changed (one generated-at shape and one NER mismatch message, DUP-FREEZE-E4, 2026-11-14)

- `ColumnProfile.generated_at` is now `datetime.now(timezone.utc).isoformat()`, the shape the
  report, quality and post-mask writers already use. A stored profile that carries the older
  `Z` suffix is a plain string field and loads unchanged.
- The out-of-core `text_mask` route and the whole-frame `text_mask` handler call
  `enforce_ner_model_version` instead of raising `ner_model_version_mismatch` inline. Both routes
  now give the helper's text for the same column and versions (the out-of-core text used to be
  shorter and named no column). The code is unchanged.
- Two sentries hold the result: `tests/sentry/profiling/test_generated_at_shape.py` fails a `Z`
  suffix or a naive `datetime.utcnow()` in the source, and
  `tests/sentry/defects/test_ner_mismatch_single_site.py` fails any second raise site of the code.
