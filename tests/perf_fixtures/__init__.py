"""Reproducible synthetic fixtures for PERF.BASE.2 / PERF.BASE.3.

The fixtures are committed Parquet files (small + medium) plus an
on-demand generation script for the large tier. The loader and schema
contract are public to tests under the ``perf`` pytest marker; nothing
in the runtime engine imports from here.
"""
