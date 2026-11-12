### Added (required-check verifier, CI-E2b, 2026-11-12)

`scripts/dev/required_checks.py` prints the check names of the merge tier, says whether a pull request head has every required job green on its first attempt, and says whether last night's scheduled runs on main are fresh and green. It reads the required list from the main tip, so a branch cannot edit its own list.
