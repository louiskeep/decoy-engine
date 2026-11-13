### Added (model findings in the post-scan, SCAN-ML M4, 2026-11-12)

The post-scan runs the classifier and spaCy NER detectors a pipeline names in
`post_scan.detectors`, after the regex detectors, over every output table. `run_post_mask` takes a
keyword `detectors`; without it the post-scan runs the regex detectors only, as before.
`run_pipeline` loads the requested models strictly at run start, so a model that cannot run
refuses the run before any row is masked. A model finding names its `source`, the `model` that
ran and a rate (NER) or a band (classifier), never a value; its severity comes from one table by
the column's configured strategy, and classifier findings are never above `warning`. NER scores
free-text columns only. Each model pass runs in its own `try`: a coded engine error propagates
unchanged, and any other exception adds one `error` finding and keeps the regex findings.
`post_scan.dismissed` entries lower the exact `warning` they name to `info`; a stale one is counted
in the `post_scan.models` log line. Under
`post_scan.ner_findings: fail` an NER warning becomes `fail` (`promoted`), and an NER `fail` or
`error` makes `run_pipeline` raise `PostScanBlockedError`, now exported from `decoy_engine`, before
it returns, so no output is published. `post_scan_verdict` takes the `ner_findings` mode in place
of `ner_blocked`: a model finding blocks only through new reason code `post_scan/ner_blocked`,
which ranks after every other code, and a classifier finding never blocks. The report gains
`detectors`, `tables_checked` and `config_sha256`; the tag stays `post-scan-integrity/v2`.
The regex-only hook, `run_post_mask` with `detectors=None`, now honours `post_scan.dismissed`: a
matching regex `warning` is reported as `info` with `dismissed: true`.
