"""Seeded violation for the secret-material census sentry: copied into a scratch package tree.

A new engine function that takes a signing key and raises with it in its frame. The sentry must
report it as missing from the committed census, and an entry claiming it is wiped must fail the
verdict check until the function really wipes it.
"""


def verify_upload(signing_key: bytes, payload: bytes) -> None:
    if not payload:
        raise ValueError("empty payload")
