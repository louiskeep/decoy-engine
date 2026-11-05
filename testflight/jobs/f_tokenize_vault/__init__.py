"""Job F: reversible tokenization through a platform token vault.

tokenize fails closed without a vault, so the runner injects an in-memory one
for this job only (a vault forces full-frame execution, which would drop the
production route of every other job).
"""
