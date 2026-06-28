"""Test-flight job registry.

Each subdirectory is one test-flight job. Add a new job by creating a
subdirectory with a manifest.yaml (validated against TestFlightManifest) and
a fixture.py (seeded generator or committed CSVs). No edit to the runner or
invariant library is required to add a job.
"""
