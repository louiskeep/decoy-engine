"""F22 output publication: the ``decoy-publication/v1`` descriptor and the publication types.

This slice holds the descriptor codec (``_descriptor.py``) and the request and receipt types
(``_types.py``). The judge and bind (``publish_unit``), vault ordering and the sink state table
arrive in later slices of the same package. Contract: ``docs/publication.md``.
"""

from decoy_engine.publication._descriptor import (
    DESCRIPTOR_NAME,
    MAX_DESCRIPTOR_BYTES,
    SCHEMA,
    Artifact,
    Descriptor,
    DescriptorVault,
    build_descriptor,
    compute_result_id,
    decode_descriptor,
    descriptor_digest,
    encode_descriptor,
)
from decoy_engine.publication._types import (
    UNIT_IMMUTABLE_DIRECTORY,
    ArtifactSummary,
    CommitReceipt,
    Outcome,
    PublicationRequest,
    VaultBinding,
)

__all__ = [
    "DESCRIPTOR_NAME",
    "MAX_DESCRIPTOR_BYTES",
    "SCHEMA",
    "UNIT_IMMUTABLE_DIRECTORY",
    "Artifact",
    "ArtifactSummary",
    "CommitReceipt",
    "Descriptor",
    "DescriptorVault",
    "Outcome",
    "PublicationRequest",
    "VaultBinding",
    "build_descriptor",
    "compute_result_id",
    "decode_descriptor",
    "descriptor_digest",
    "encode_descriptor",
]
