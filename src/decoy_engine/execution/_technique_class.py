"""MG-1 S1 (2026-06-01): GDPR technique classification.

Industry-standard taxonomy used by GDPR-aware data tools (Privitar,
Tonic, K2View, ARX Anonymization Tool) to label a masking strategy
by what it does to identifiability. The label drives operator-facing
badges in the strategy picker; it does NOT change any runtime
behavior.

Reference: ICO + EDPB Guidelines 05/2020 on "Consent under
Regulation 2016/679" + Article 4(5) GDPR + ENISA's "Recommendations
on shaping technology according to GDPR provisions" (2018).

Buckets:
  - pseudonymisation: keyed transform; original recoverable from the
    transform + key. Linkage is broken but reversible. Per Art 4(5)
    GDPR, the key must be held separately.
  - anonymisation: irreversible transform; no key recovers the
    original. Linkage is destroyed. Per Recital 26 GDPR, this is
    the only label that takes the data out of the GDPR scope.
  - synthetic: the output is generated rather than derived from the
    input. There is no semantic linkage between source and output.
  - passthrough: no transformation. The column is left untouched.
"""

from __future__ import annotations

from typing import Literal

# The four user-visible classes. None means "the strategy hasn't been
# classified yet" -- defensive default for new strategies that ship
# without a label. The FE renders a "needs review" badge in that case.
TechniqueClass = Literal[
    "pseudonymisation",
    "anonymisation",
    "synthetic",
    "passthrough",
]


# Strategy name -> technique class. Single source of truth; the
# strategy registry + plan compile + manifest serialization all read
# from this map. Adding a new strategy without an entry here is
# allowed (the manifest will carry technique_class=None and the FE
# will surface "needs classification") but every strategy SHOULD be
# classified before its first release.
#
# Classification rationale per strategy:
#   - hash / fpe: keyed, deterministic; original recoverable from
#     the key. Pseudonymisation per Art 4(5).
#   - date_shift: keyed offset; the relative ordering survives + the
#     original date is recoverable with the key. Pseudonymisation.
#   - redact: replaces the value with a fixed string; no key
#     recovers the original. Anonymisation.
#   - truncate: drops part of the value; the dropped portion is not
#     recoverable. Anonymisation (note: when truncate keeps enough
#     bits to remain identifying, the operator should classify the
#     COLUMN as needing additional measures -- the strategy itself
#     is anonymising).
#   - bucketize: replaces with a range/bucket label; the original
#     value is not recoverable. Anonymisation.
#   - shuffle: permutes values within a column; the original mapping
#     is lost. Anonymisation.
#   - faker: generates a new value from a distribution; the source
#     value does not contribute. Synthetic.
#   - categorical: picks from a fixed list; the source value does
#     not contribute. Synthetic.
#   - formula: arbitrary expression; cannot be classified statically
#     since the formula may copy the source through. Default to
#     pseudonymisation (the safer assumption) so the operator sees
#     a warning if the formula is actually irreversible (they can
#     override at the column level).
#   - passthrough: no transformation.
TECHNIQUE_CLASS_BY_STRATEGY: dict[str, TechniqueClass] = {
    "passthrough": "passthrough",
    "redact": "anonymisation",
    "truncate": "anonymisation",
    "bucketize": "anonymisation",
    "shuffle": "anonymisation",
    "hash": "pseudonymisation",
    "fpe": "pseudonymisation",
    "date_shift": "pseudonymisation",
    "formula": "pseudonymisation",
    "faker": "synthetic",
    "categorical": "synthetic",
    # text_redact (MG-2, 2026-05-31): replaces matched PII spans inside
    # free-text with a fixed token; the original span is not recoverable.
    # Anonymisation, same class as `redact` (which replaces the whole cell).
    "text_redact": "anonymisation",
    # `nested` (MG-3 M2, 2026-05-31) is intentionally absent. It is a
    # wrapper -- its GDPR posture is the child strategy's posture, not
    # its own. Until the FE surfaces the child-strategy badge for
    # nested columns, technique_class_for("nested") returns None and
    # the operator sees the unclassified badge as a prompt to inspect
    # the child config. Tracked in the MG-1.5-FE-pickers follow-up.
}


def technique_class_for(strategy: str | None) -> TechniqueClass | None:
    """Return the technique class for a strategy name, or None when
    the strategy is unknown / unset. None is the FE's signal to
    render the "needs classification" badge."""
    if not strategy:
        return None
    return TECHNIQUE_CLASS_BY_STRATEGY.get(strategy)
