"""Job A: Healthcare claims (one-to-many, multi-level depth 3).

Topology: members -> claims -> claim_lines (two FK hops).

This job proves:
- Parents-before-children ordering survives two FK hops.
- FPE + checksum (luhn / npi) columns are structurally valid after masking.
- geo_generalize produces Safe Harbor suppression for restricted ZIP3 rows.
- code_set (icd10 / hcpcs / ndc) remaps codes to valid corpus entries.
- derived / case_when / derived_aggregate produce correct computed values.
- Quarantine removes exactly the planted invalid-Luhn SSN rows.
- Sentinel raw PII strings are absent from all output columns.

Phase 0: skeleton manifest only. Fixture generator and full pipeline config
are implemented in Phase 2.
"""
