### Added (resume fingerprint and reasons, F23-E1, 2026-11-13)

Thirteen reasons in the `decoy` namespace, all pending release: `decoy/resume_run_dir_invalid`, `decoy/resume_layout_unsupported`, `decoy/resume_route_unsupported`, `decoy/resume_vault_unsupported`, `decoy/resume_platform_unsupported`, `decoy/resume_no_checkpoint`, `decoy/resume_store_invalid`, `decoy/resume_superseded`, `decoy/resume_fingerprint_changed` and `decoy/resume_source_changed` (usage failures), `decoy/resume_in_progress` (failure, retry the operation), `decoy/resume_checkpoint_invalid` (decision) and `decoy/resume_store_not_removed` (warning).

New `decoy_engine.delivery.resume` sub-package with `RunFingerprint`, `build_fingerprint` and `binding_digest`: the value-free key a resume store is tied to (config hash, injected component versions, an HMAC key check, chunk size) and the per-table hash of a source and target spec. Nothing in the engine calls them yet.
