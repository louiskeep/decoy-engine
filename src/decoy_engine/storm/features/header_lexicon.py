"""Header-role lexicon: map cryptic/abbreviated column headers to canonical roles.

CH-1 (acronym/synonym lexicon) + CH-2 (fuzzy char-n-gram fallback). The STORM
column classifier featurizes a header as ``hdr_{token}`` indicator features
(featurizer.py). Those only fire for tokens the ``DictVectorizer`` saw at
training time, so a cryptic header seen only at inference (``dx_cd``, ``mbr_id``)
contributes no header signal at all. This module adds a second, vocabulary-stable
channel: it maps header tokens to a small closed set of canonical *roles* aligned
with the label space, so ``diagnosis_code`` (training) and ``dx_cd`` (inference)
both emit ``role_icd10`` and the model can lean on the header even when the exact
token is novel.

Design (auditable, near-zero dependency -- per CH-1):
    * The lexicon is an EXPLICIT table (``_ROLE_SYNONYMS``): canonical role ->
      the exact tokens/acronyms that name it. No learned weights, no embeddings;
      a reviewer can read the whole mapping. Source patterns: HIPAA/HL7 field
      naming, common EHR/claims column conventions.
    * Exact match runs first (CH-1). A token that is a known synonym contributes
      its role directly.
    * Fuzzy match (CH-2) is a bounded fallback: a token that is NOT an exact hit
      is compared to every synonym by character-bigram Dice coefficient, and the
      best role above ``_FUZZY_THRESHOLD`` is taken. This catches typos and
      morphological variants (``diagnossis``, ``insurnce``) without a model. It is
      deliberately conservative -- a miss falls through to content features, which
      is the safe direction (no false role beats a wrong role).

Determinism: pure function of the input header; returns a sorted list so feature
emission order is stable across runs (required by the golden retrain gate).
No I/O, no global state.
"""

from __future__ import annotations

from decoy_engine.storm.features.builder import tokenize_header

#: Canonical roles align 1:1 with the content label space (types.py labels),
#: EXCEPT ``none`` (absence of a role is represented by emitting nothing). A role
#: is emitted as an ``role_{canonical}`` feature. Birth-date headers (``dob``,
#: ``birthdate``) map to ``iso_date`` -- there is no separate DOB label, and a
#: DOB column IS labeled ``iso_date``, so folding them in gives a bare ``dob``
#: header the same date signal a spelled-out ``service_date`` gets (rather than
#: a dangling role with no matching label, which would split the signal).
#: Synonyms are SINGLE tokens only (headers are tokenized on ``_ - . / space`` and
#: camelCase before lookup, so a multi-word phrase would never exact-match and
#: would only pollute the fuzzy channel). Ambiguous/generic tokens that name more
#: than one role or none of them (``security`` -> ssn|cvv, ``account`` -> pan|iban,
#: ``record``/``tax``/``primary``/``uid``/``id``) are deliberately excluded; the
#: model falls back to content features for those, which is the safe direction.
_ROLE_SYNONYMS: dict[str, tuple[str, ...]] = {
    "ssn": ("ssn", "ssno", "ssnum", "ss", "ssn9", "social", "sin", "nino"),
    "email": ("email", "emails", "eml", "mail", "eaddr", "econtact"),
    "pan": ("pan", "card", "cc", "ccno", "ccnum", "credit", "debit"),
    "iban": ("iban", "bban", "sepa", "swift"),
    "icd10": (
        "icd",
        "icd10",
        "icd9",
        "dx",
        "dxcd",
        "diag",
        "diagnosis",
        "diagnostic",
        "condition",
        "morbidity",
    ),
    "iso_date": (
        # Clinical-event words (admit/discharge/visit) are intentionally NOT
        # here: "visit_date" already maps via "date", and bare "visit"/"admit"
        # name an event, not a date, so they only mis-fire on "visit_id" etc.
        "date",
        "dt",
        "dos",
        "timestamp",
        "ts",
        "datetime",
        # Birth-date headers fold into iso_date (no separate DOB label).
        "dob",
        "birthdate",
        "birthday",
        "bday",
        "bdate",
    ),
    "npi": ("npi", "npino", "prov"),
    "mrn": ("mrn", "mr", "mrno", "chart", "emr", "ehr", "medical", "patient", "pt"),
    "health_plan_id": (
        "plan",
        "payer",
        "insurance",
        "ins",
        "hmo",
        "ppo",
        "carrier",
        "benefit",
        "coverage",
        "member",
        "mbr",
        "subscriber",
        "medicaid",
        "medicare",
    ),
    "cvv": ("cvv", "cvv2", "cvc", "cvc2", "cvn", "cid"),
}

#: Tokens that name a role but ALSO appear routinely as non-PII structural
#: columns; excluded from single-token role emission to avoid over-firing.
#: (e.g. bare "id"/"code"/"number"/"ref" carry no type signal on their own.)
_STOPWORD_TOKENS: frozenset[str] = frozenset(
    {"id", "code", "number", "num", "no", "ref", "col", "field", "value", "val", "key"}
)

#: Minimum character-bigram Dice coefficient for a fuzzy (CH-2) role match
#: (inclusive). 0.80 accepts single-char typos/omissions on a >=6-char token
#: ("insurnce"~"insurance" -> Dice 0.80) while the ``_FUZZY_MIN_LEN`` guard
#: rejects short-token noise ("plan"~"plane"). Validated against the corpus
#: clear/cryptic headers + common non-PII headers to emit no false positives.
_FUZZY_THRESHOLD = 0.80

#: Only attempt fuzzy matching for tokens at least this long; short tokens
#: (<=4 chars) produce too many spurious bigram overlaps.
_FUZZY_MIN_LEN = 6


def _normalize(token: str) -> str:
    """Collapse a header token to its comparison form (alnum, lowercased)."""
    return "".join(ch for ch in token.lower() if ch.isalnum())


# Reverse index: normalized synonym -> role. Built once at import (pure).
_SYNONYM_TO_ROLE: dict[str, str] = {}
for _role, _syns in _ROLE_SYNONYMS.items():
    for _syn in _syns:
        _SYNONYM_TO_ROLE.setdefault(_normalize(_syn), _role)


def _bigrams(s: str) -> list[str]:
    """Character bigrams of *s* (list, so repeated bigrams count -- Dice)."""
    return [s[i : i + 2] for i in range(len(s) - 1)]


def _dice(a: str, b: str) -> float:
    """Sorensen-Dice coefficient over character bigrams, in [0, 1]."""
    ba, bb = _bigrams(a), _bigrams(b)
    if not ba or not bb:
        return 1.0 if a == b else 0.0
    # Multiset intersection size.
    from collections import Counter

    ca, cb = Counter(ba), Counter(bb)
    inter = sum((ca & cb).values())
    return 2.0 * inter / (len(ba) + len(bb))


def _fuzzy_role(norm_token: str) -> str | None:
    """Best fuzzy (CH-2) role for a normalized token, or None below threshold."""
    if len(norm_token) < _FUZZY_MIN_LEN:
        return None
    best_role: str | None = None
    best_score = 0.0
    for syn, role in _SYNONYM_TO_ROLE.items():
        if len(syn) < _FUZZY_MIN_LEN:
            continue
        score = _dice(norm_token, syn)
        # Strictly-greater keeps the FIRST (insertion-order) synonym at the max,
        # so ties resolve deterministically for the golden gate.
        if score > best_score:
            best_score = score
            best_role = role
    return best_role if best_score >= _FUZZY_THRESHOLD else None


def roles_from_tokens(header_tokens: list[str]) -> list[str]:
    """Return the sorted canonical roles implied by already-tokenized header.

    Runs CH-1 (exact synonym) then CH-2 (fuzzy bigram fallback) over each
    token. Returns a de-duplicated, sorted list of canonical role names
    (e.g. ``["icd10"]``, ``["cvv", "pan"]``); empty when no token maps,
    in which case the classifier relies on content features alone. Stopword
    tokens (bare ``id``/``code``/...) never emit a role on their own.

    This is the featurizer entry point (features are built from ``header_tokens``,
    matching ``tokenize_header``). Pure and deterministic.
    """
    roles: set[str] = set()
    for token in header_tokens:
        norm = _normalize(token)
        if not norm or norm in _STOPWORD_TOKENS:
            continue
        role = _SYNONYM_TO_ROLE.get(norm)
        if role is None:
            role = _fuzzy_role(norm)
        if role is not None:
            roles.add(role)
    return sorted(roles)


def header_roles(column_name: str) -> list[str]:
    """Convenience wrapper: canonical roles for a raw column name.

    Tokenizes with the same ``tokenize_header`` the featurizer uses, then
    delegates to :func:`roles_from_tokens`. Pure and deterministic.
    """
    return roles_from_tokens(tokenize_header(column_name))
