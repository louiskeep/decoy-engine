"""Shipped corpus files for the code_set masking strategy (SP-09).

Each ``.parquet`` file in this directory is a free-license code corpus
that the :mod:`decoy_engine.transforms.code_set` strategy loads by name.

Shipped corpora
---------------
icd10   -- ICD-10-CM (CMS/WHO FY2024). Public domain (US Govt work, 17 U.S.C. 105).
hcpcs   -- HCPCS Level II Q1 2024 (CMS). Public domain (US Govt work, 17 U.S.C. 105).
ndc     -- FDA National Drug Code Directory. Public domain (US Govt work, 17 U.S.C. 105).
mcc     -- ISO 18245 Merchant Category Codes. Standard reference enumeration;
           no copyright restriction on the published MCC list.

Schema
------
Every corpus file has three string columns::

    code        -- the code value (e.g. "I21.9", "G0008", "00093052105", "5411").
    chapter     -- category/chapter label (e.g. "I", "G", "A", "retail").
    description -- human-readable description of the code.

See ``scripts/build_codesets.py`` for the build script and source citations.
"""
