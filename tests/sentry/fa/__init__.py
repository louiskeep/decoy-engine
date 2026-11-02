"""Static scanning library behind the file-access sentries (docs/security/file-access-policy.md).

Built on the stdlib `ast`, not on a third-party def-use library: the sentries are test-only and
this keeps the dependency set (and the certified DP profile's pinned fingerprint) unchanged.
"""
