"""Decoy acceptance test-flight suite.

Deliberately-run, multi-job acceptance suite that proves Phase-5 strategies
compose, post-run distribution is intact, and relationships hold. Not part of
the default regression loop; invoke via:

  pytest testflight -m testflight
  python scripts/test_flight.py

See testflight/_spec.py for the data model (JobSpec / InvariantSpec), and
testflight/_runner.py for the seven-step runner contract.
"""
