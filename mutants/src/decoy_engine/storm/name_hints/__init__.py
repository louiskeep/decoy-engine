"""STORM detector name-hint library.

The 26 built-in detectors' column-name hint patterns live as YAML
under ``v1/``; see ``loader.py`` for how they are read at import time
and ``v1/README.md`` for the file format + contribution conventions.

The hard-coded ``_NAME_HINTS`` dict that previously lived in
``decoy_engine.storm.detectors`` was extracted to this package on
2026-05-25 so the patterns can be reviewed in YAML diffs rather
than buried in a 400-line Python literal.
"""
