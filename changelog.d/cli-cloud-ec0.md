### Changed (delivery plan covers cloud targets, CLI-CLOUD EC0, 2026-11-12)

`plan_delivery` now plans every declared table whose target type is `file`, `s3` or `gcs` (`DELIVERABLE_TARGET_TYPES`). A caller passes an outcome for each of those tables, and an outcome for an `s3` or `gcs` table no longer raises. A table without a target still has no delivery state. The mismatch message now reads "delivery outcomes do not match the deliverable tables".
