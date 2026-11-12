"""F22 output publication: the ``decoy-publication/v1`` descriptor and the publication types.

The descriptor codec (``_descriptor.py``), the request and receipt types and the typed errors
(``_types.py``), and the judge and bind under the destination lock (``_publish.py``:
``publish_unit``, ``publish_tables``). Vault ordering and the sink state table arrive in later
slices of the same package. Contract: ``docs/publication.md``.
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
from decoy_engine.publication._publish import publish_tables, publish_unit
from decoy_engine.publication._types import (
    UNIT_IMMUTABLE_DIRECTORY,
    ArtifactSummary,
    CommitReceipt,
    Outcome,
    PublicationFailed,
    PublicationRefused,
    PublicationRequest,
    VaultBinding,
    raise_for_outcome,
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
    "PublicationFailed",
    "PublicationRefused",
    "PublicationRequest",
    "VaultBinding",
    "build_descriptor",
    "compute_result_id",
    "decode_descriptor",
    "descriptor_digest",
    "encode_descriptor",
    "publish_tables",
    "publish_unit",
    "raise_for_outcome",
]
