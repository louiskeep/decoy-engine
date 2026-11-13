### Fixed (isolated probe child hang, nightly, 2026-11-13)

- Classifying a run failure no longer imports engine modules. The classifier used to import every
  registered error family, including `quality.dp` with opendp, scikit-learn and scipy's OpenBLAS.
  A memory-capped isolated child that had just run out of address space then spun at 100% CPU in
  OpenBLAS's init retry loop instead of reporting the failure, so the route probe hung until its
  timeout and the nightly `test_width_change_flips_route_at_a_fixed_budget_and_row_count` failed.
  The same path covered `safe_error_type` while a failure is serialized, and two table entries that
  named a re-export module instead of the defining one, so a live exception could miss its family.
