"""Shipped corpus files for the code_set masking strategy (SP-09 / HC-1).

Each ``.parquet`` file in this directory is a free-license code corpus
that the :mod:`decoy_engine.transforms.code_set` strategy loads by name.
This corpus list is documentation, hand-kept in sync with the single source
of truth, ``CODESET_REGISTRY`` in ``transforms/_codeset_provenance.py``
(HC-1 slice 1 item 5); a drift-guard test
(``tests/unit/transforms/test_code_set.py::TestShippedCorpora
::test_codesets_docstring_lists_every_registry_corpus``) fails CI if a
registry corpus name is not named below.

Shipped corpora
---------------
icd10   -- ICD-10-CM diagnosis codes (CDC/NCHS + CMS). Public domain (US Govt
           work, 17 U.S.C. 105).
hcpcs   -- HCPCS Level II procedure/supply codes (CMS). Public domain (US
           Govt work, 17 U.S.C. 105).
ndc     -- FDA National Drug Code Directory entries. Public domain (US Govt
           work, 17 U.S.C. 105).
mcc     -- ISO 18245 Merchant Category Codes. Standard reference enumeration;
           no copyright restriction on the published MCC list.

HC-1 slice 1 (2026-07-17): every file above is an ABBREVIATED SEED (a
handful of codes per chapter/section, not the full public code set) --
``is_seed: true`` in each file's Parquet metadata marks this explicitly.
Row counts are intentionally not documented here since slice 2 (a separate,
larger change) replaces these with the full CMS/CDC/FDA data; call
``decoy_engine.transforms.code_set.load_corpus(name)`` and check
``len(...)`` for the current count, or ``load_corpus_provenance(name)`` for
the full provenance record (source, source_version, effective_date,
license, is_seed).

Schema
------
Every corpus file has three string columns::

    code        -- the code value (e.g. "I21.9", "G0008", "00093052105", "5411").
    chapter     -- category/chapter label (e.g. "I", "G", "A", "retail").
    description -- human-readable description of the code.

Every corpus file also carries Parquet key/value metadata (``source``,
``source_url``, ``license``, ``citation``, ``source_version``,
``effective_date``, ``is_seed``), read back into a typed
``CodeSetProvenance`` record by
``decoy_engine.transforms._codeset_provenance.CodeSetProvenance
.from_parquet_metadata``. A shipped corpus missing required provenance
fields fails closed at load time (see ``transforms/code_set.py``'s module
docstring).

See ``scripts/build_codesets.py`` for the build script and source citations,
and ``docs/strategies.md``'s ``code_set`` section for the full provenance
and evidence-surfacing contract.
"""
