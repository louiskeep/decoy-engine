### Added (engine sample request, SAMPLE SE1, 2026-11-12)

`decoy_engine.subset` gains `sample_request(config, *, rows, root=None, use_config_subset=True,
display_rows=0)`, which returns a `SubsetRequest` that picks the first `rows` rows of every
child-free table and closes upward over the retained edges, so a sample keeps every foreign key.
An explicit `root` walks both ways in its own component. It also exports `SampleSpec`,
`SAMPLE_ROWS_MIN`, `SAMPLE_ROWS_MAX` and `sample_refusal_edge`, which names the first edge from a
generated parent to a masked child (a sample refuses that shape). `SubsetRequest` gains an optional
`sample`, `SubsetScope` gains `head`, `SeedSpec` gains `cap` and the `head` mode. `head` and
`cap` are set in code only: the `subset:` block in YAML still accepts `sample`, `filter` and
`keys`, and the config hash is unchanged. No reason code is added.
