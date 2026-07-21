"""Real-data ETL for code_set corpora (HC-1 slice 2).

Fetches full public-domain code-set data (NDC, ICD-10-CM, HCPCS, MS-DRG) from
the issuing federal agency, normalizes it to the EXACT on-disk shape
``decoy_engine.transforms._codeset_loader`` validates (a ``code`` column,
sorted ascending, plus the HC-1 slice-1 provenance metadata block -- see
``decoy_engine.transforms._codeset_provenance.CodeSetProvenance``), and writes
it to a local cache directory. Deliberately lives under ``scripts/``, not
``src/decoy_engine/``: ``[tool.hatch.build.targets.wheel] packages =
["src/decoy_engine"]`` in ``pyproject.toml`` means anything under ``scripts/``
is never packaged into the wheel, which is what makes this opt-in rather than
bundled -- a user who never runs ``python -m scripts.codeset_etl.update``
sees exactly today's shipped-seed behavior, unchanged.

This module does NOT modify ``transforms/code_set.py``'s selection logic,
the shipped-corpus registry, or ``src/decoy_engine/codesets/``. It writes
its output to a separate cache directory (see ``_cache.default_cache_dir``);
a pipeline opts in by pointing ``provider_config.corpus_source`` at
``customer:<cache_dir>/<name>.parquet`` -- the customer-corpus path the
loader already supports, unchanged. No new loader code path is needed.

Architecture (per-source plug-in over a shared pipeline):
  ``parsers/_base.py``   -- ``ParsedCorpus`` result type + ``CorpusParser``
                            protocol every source implements.
  ``parsers/_ndc.py``    -- FDA NDC Directory (``ndctext.zip``). Implemented
                            end-to-end; this slice's proof corpus.
  ``parsers/_icd10cm.py``-- CDC NCHS ICD-10-CM. Scaffolded: wired with the
                            real source URL and file layout, raises
                            ``NotImplementedError`` from ``parse_archive``
                            (see that module's docstring for the follow-on
                            spec). HCPCS and MS-DRG are not scaffolded in
                            this slice; add a sibling parser module + a
                            ``PARSERS`` registry entry to extend.
  ``_fetch.py``          -- HTTPS-only download with an injectable fetch
                            function (tests mock this; never hit the network
                            in the unit suite) and a fail-closed minimum-size
                            floor per source.
  ``_write.py``          -- the ONE normalize -> validate -> write path every
                            parser's output goes through: sorts ascending by
                            code (CS.1-CS.9 determinism contract), rejects
                            duplicate/empty codes, stamps the HC-1 slice-1
                            provenance block with ``is_seed=false``, and
                            writes atomically (temp file + rename) so a
                            crash mid-write cannot leave a partial corpus.
  ``update.py``          -- orchestrates one named corpus's fetch -> parse ->
                            validate -> write; the CLI entrypoint.
"""

from __future__ import annotations
