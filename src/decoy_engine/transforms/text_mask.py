"""text_mask strategy (SP-07, 2026-06-28): span-level PII masking with per-detector dispatch.

Walks each cell with ``storm.detectors.iter_spans`` and masks each matched span
using a per-detector strategy. Non-matched text portions are controlled by
``unmatched_span_policy`` (default: ``redact``).

Cross-cell keyed determinism: each span is masked by deriving a per-value key as
HMAC-SHA256(mask_key, matched_text). The same real value in any two cells always
produces the same masked value regardless of surrounding context; the context is
intentionally excluded from the key so cross-cell consistency holds.

NER exception (TX-2, 2026-07-20): the key derivation above is unchanged -- it is
still HMAC(mask_key, matched_text), context-free -- so per-cell reproducibility
and in-core/out-of-core parity always hold for every span source, and the
cross-cell guarantee above holds in full for the regex detectors (whose
detector_id is a function of the value's shape alone). It does NOT hold for the
one context-SENSITIVE span source: opt-in spaCy NER (``extra_spans=`` from
``storm.ner.iter_ner_spans``). NER assigns the ``detector_id`` (person_name vs
location) from surrounding context, and the synthesis STRATEGY + faker method
are selected by detector_id, so an ambiguous surface string (e.g. "Jordan"
classified as a person in one cell and a place in another) can synthesize to two
different values across cells. The span KEY is still identical in both cells;
only the entity TYPE, and therefore the chosen faker method, differs. Cross-cell
synthetic consistency is thus guaranteed only for unambiguous entities under
NER; it is not a regression in key/mask determinism.

STORM single source of truth: ``iter_spans`` is called directly from
``storm.detectors``; any detector added to ``_SPAN_DETECTORS`` is automatically
available to ``text_mask`` in the same release. No separate detector registry.

Raw-value isolation: ``matched_text`` is never emitted to logs or evidence.
The HMAC derivation hides the raw value behind a one-way function. Sentry tests
(``tests/unit/transforms/test_text_mask.py``) verify this invariant.

Overlap resolution: when two detected spans overlap, the leftmost span wins; ties on
start position resolve to the longer match (leftmost-then-longest). Spans are sorted
by ``(start, -length)`` and a greedy non-overlap sweep keeps the first non-conflicting
match. Earlier spec text described this as "longer-match-wins", which is imprecise: the
primary sort key is start position, not length.

Unmatched-span interpretation: "unmatched" means text segments NOT covered by
any detector match (e.g. clinical prose surrounding an SSN). The default
``redact`` policy treats these as potentially undetected PII and replaces them
with the token. ``passthrough`` is operator opt-in for columns where surrounding
context is known safe. ``replace_with_token`` substitutes the sentinel
``"[UNMATCHED]"`` as a lighter-weight marker distinct from per-span redaction.

Pattern: HMAC-SHA256 keyed span determinism (RFC 2104).
See: https://datatracker.ietf.org/doc/html/rfc2104

Methodology: reuses STORM ``iter_spans`` (single detector source), ``fpe_encrypt_value``
(Feistel+HMAC, Type-II Feistel 1973; HMAC RFC 2104), and stdlib HMAC for per-span
key derivation. Per-detector defaults documented in ``DETECTOR_DEFAULTS``.

TX-2 (2026-07-20): the ``text_mask`` strategy handler (``execution/_strategies/
_text_mask.py``) and its out-of-core twin (``execution/out_of_core/
_mask_group_c.py``) opt-in NER the same way ``text_redact`` does (WS2): resolve
``storm.ner.iter_ner_spans`` and pass the result as this module's ``extra_spans``.
No new methodology here -- it reuses the spaCy NER pattern already registered for
``storm/ner.py`` and this module's own already-registered HMAC-SHA256 keyed span
determinism. The one addition specific to this module is a ``location`` entry in
``DETECTOR_DEFAULTS`` (below): NER emits ``location`` spans (GPE/LOC/FAC) but the
table had no default for them, so they silently fell to ``redact`` instead of
synthesizing like the other Tier-2 NER detectors.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac_mod
import logging
from datetime import datetime, timedelta
from typing import Any

from faker import Faker

from decoy_engine.storm.detectors import Span, iter_spans
from decoy_engine.transforms.date_shift import _COMMON_FORMATS
from decoy_engine.transforms.fpe import _CHARSETS, fpe_encrypt_value

_log = logging.getLogger(__name__)

_DEFAULT_TOKEN: str = "[REDACTED]"  # noqa: S105 - redaction placeholder, not a credential
_UNMATCHED_TOKEN: str = "[UNMATCHED]"  # noqa: S105 - sentinel for replace_with_token policy

# ── Per-detector default dispatch table (SP-07) ───────────────────────────────
#
# REACHABILITY TIERS - read before adding or trusting entries:
#
# TIER 1 - Built-in span detectors (fire automatically via iter_spans):
#   These 11 detectors produce spans under the built-in path and their
#   defaults below are ACTIVE for every mask_cell call:
#     email, ssn, us_phone, us_zip, pan, iban, ipv4, icd10, npi, url,
#     street_address.
#
# TIER 2 - NER/custom-only detectors (defaults apply ONLY when spans are
#   supplied via extra_spans= or custom=):
#   These entries define sensible defaults for operators who inject spans
#   from NER (storm.ner.iter_ner_spans -> extra_spans=) or custom= patterns.
#   Under the BUILT-IN path they are UNREACHABLE - iter_spans never emits
#   a span with these detector_ids because name-hint-only regexes are
#   intentionally excluded from _SPAN_DETECTORS.
#   Do NOT advertise Tier-2 detectors as "masked" in operator docs for
#   the built-in path.  Tier-2 detectors:
#     person_name, first_name, last_name, address, location   (require NER)
#     iso_date, us_date, eu_date                    (require NER or custom=)
#     fax_number, cvv, mrn, health_plan_id,
#     license_num, vehicle_id, device_id, biometric_id
#
# Passthrough risk: unmatched_span_policy="passthrough" lets any text NOT
#   covered by a detected span ride through unchanged. Under the built-in
#   path, names, addresses, and dates are NEVER detected, so they ride
#   through in the clear. Use the default "redact" policy (or inject NER
#   extra_spans) when the column may contain such values.
#
# Strategy names: "fpe", "faker", "date_shift", "redact", "passthrough".
# Operators override per-detector via YAML ``per_detector_strategy: {id: strategy}``.
#
# Default rationale per group:
#   fpe        - digit-structured fields where format preservation adds value
#                and the field length is long enough to resist brute-force.
#   faker      - semantic-class names/addresses where synthetic replacement is
#                most useful (regex too loose for FPE without column-name hint).
#   date_shift - temporal fields; keyed offset preserves temporal ordering.
#   redact     - anything too structurally complex for FPE, too short (CVV), or
#                name-hint-only without a meaningful value pattern (health IDs).

DETECTOR_DEFAULTS: dict[str, str] = {
    # Format-preserving encryption (digit strings, separators preserved in-place)
    "ssn": "fpe",
    "us_phone": "fpe",
    "us_zip": "fpe",
    "pan": "fpe",  # Luhn check digit recomputed via checksum="luhn"
    "fax_number": "fpe",
    "npi": "fpe",  # 10-digit NPI; checksum="npi" validates CMS check digit
    # Faker generation (synthetic replacement preserving semantic class)
    "person_name": "faker",
    "first_name": "faker",
    "last_name": "faker",
    "address": "faker",
    # TX-2 (2026-07-20): NER emits `location` (GPE/LOC/FAC); without a
    # default it fell through to "redact" (`effective_map.get(...,
    # "redact")` below) even though it is semantically closer to
    # `address` -- a plausible synthetic place name keeps the sentence
    # readable where a bare token does not.
    "location": "faker",
    # Date shifting (keyed temporal offset; format preserved)
    "iso_date": "date_shift",
    "us_date": "date_shift",
    "eu_date": "date_shift",
    # Redact (complex format, too short, or name-hint-only with no span detection)
    "email": "redact",  # domain structure too complex for digit FPE
    "cvv": "redact",  # 3-4 digits; FPE trivially reversible at this length
    "iban": "redact",  # per-country BBAN; FPE not supported (see fpe.py)
    "ipv4": "redact",  # octet range constraint breaks under digit FPE
    "icd10": "redact",  # chapter+code structure
    "mrn": "redact",  # no standard format; name-hint-only
    "url": "redact",  # nested host/path/query structure
    "health_plan_id": "redact",  # no standard format; name-hint-only
    "license_num": "redact",  # varies by state/body; name-hint-only
    "vehicle_id": "redact",  # VIN ISO 3779 check-char; full-VIN FPE not in scope
    "device_id": "redact",  # no standard format; name-hint-only
    "biometric_id": "redact",  # no standard format; name-hint-only
    "street_address": "redact",  # prose address; present in _SPAN_DETECTORS
}

# FPE config per detector: (charset_name, checksum_scheme_or_None).
# ``charset_name`` is a key into ``fpe.py``'s ``_CHARSETS`` dict.
_FPE_CONFIG: dict[str, tuple[str, str | None]] = {
    "ssn": ("digits", None),
    "us_phone": ("digits", None),
    "us_zip": ("digits", None),
    "pan": ("digits", "luhn"),
    "fax_number": ("digits", None),
    "npi": ("digits", "npi"),
}

# Faker method name per detector (maps to a Faker instance method).
_FAKER_METHOD: dict[str, str] = {
    "person_name": "name",
    "first_name": "first_name",
    "last_name": "last_name",
    "address": "address",
    # TX-2: "city" rather than "address" -- NER `location` spans (GPE/LOC/
    # FAC) are typically a bare place name ("Boston"), not a street address.
    "location": "city",
}


# ── Core primitives ───────────────────────────────────────────────────────────


def _span_key(mask_key: bytes, matched_text: str) -> bytes:
    """Per-span mask key: HMAC-SHA256(mask_key, matched_text) (RFC 2104).

    The key depends on ``matched_text`` ONLY -- not on surrounding cell text,
    column name, or row index. The same real SSN in any two cells therefore
    always produces the same masked SSN because both produce the same
    (mask_key, matched_text) pair and hence the same HMAC digest.

    DE-02: ``mask_key`` is the keyed-mask IKM -- the 8-byte ``job_seed`` when no
    secret is present (byte-identical to pre-DE-02) or a 32-byte KeyProvider root
    under a secret. HMAC accepts a key of any length, so the substitution is
    transparent to this primitive.

    Raw-value isolation: this function consumes ``matched_text`` internally
    to produce keying material only; it never appears in logs or evidence.
    """
    msg = matched_text.encode("utf-8", errors="replace")
    return _hmac_mod.new(mask_key, msg, hashlib.sha256).digest()


def _mask_fpe(
    matched_text: str, span_key: bytes, detector_id: str, token: str = _DEFAULT_TOKEN
) -> str:
    """FPE-encrypt a span using the per-detector charset + checksum config.

    Uses ``fpe_encrypt_value`` from ``transforms.fpe`` (Feistel+HMAC, RFC 2104;
    no new crypto introduced). The tweak is the detector_id so the same real
    value encrypted as "ssn" vs "us_phone" produces different ciphertext, even
    with the same span_key.

    Falls back to ``token`` if FPE fails (e.g. value too short for a checksum
    scheme, or all-separator input). Honors the operator-configured token rather
    than hardcoding ``_DEFAULT_TOKEN``.
    """
    cfg = _FPE_CONFIG.get(detector_id, ("digits", None))
    charset_name, checksum = cfg
    charset = _CHARSETS.get(charset_name, charset_name)
    tweak = detector_id.encode("utf-8")
    try:
        return fpe_encrypt_value(
            matched_text,
            span_key,
            charset,
            tweak,
            preserve_separators=True,
            validate_luhn=False,
            checksum=checksum,
        )
    except Exception:
        # FPE failed (value too short for checksum scheme, degenerate input, etc.)
        # Fail closed: apply redact token rather than passing unmasked text.
        # Log strategy + detector_id only; never the matched_text (raw-value isolation).
        _log.debug("fpe strategy failed for detector %s; applying redact token", detector_id)
        return token


def _mask_faker(matched_text: str, span_key: bytes, detector_id: str) -> str:
    """Generate a synthetic replacement via a per-span-keyed Faker instance.

    The seed is the first 4 bytes of the HMAC span key (interpreted as a
    big-endian unsigned int) so the same real value always maps to the same
    synthetic value; seed_instance makes the Faker call reproducible.

    Raw-value isolation: ``matched_text`` is consumed only to derive
    ``span_key``; the Faker call uses only the derived integer seed.
    """
    method_name = _FAKER_METHOD.get(detector_id, "name")
    seed = int.from_bytes(span_key[:4], "big")
    fake = Faker()
    fake.seed_instance(seed)
    method = getattr(fake, method_name, None)
    if callable(method):
        try:
            return str(method())
        except Exception:
            pass
    # Fallback: generic name if the specific method is unavailable
    return str(fake.name())


def _detect_date_format(text: str) -> str | None:
    """Return the first format in ``_COMMON_FORMATS`` that parses ``text``, or None."""
    for fmt in _COMMON_FORMATS:
        try:
            datetime.strptime(text, fmt)
            return fmt
        except ValueError:
            continue
    return None


def _mask_date_shift(
    matched_text: str,
    span_key: bytes,
    min_days: int,
    max_days: int,
) -> str:
    """Shift a date span by a keyed deterministic offset in [min_days, max_days].

    The shift is derived from the first 8 bytes of the HMAC span key reduced
    mod range_size, so the same real date always shifts by the same number of
    days. The detected format string is preserved: "1990-01-15" reformats to
    "YYYY-MM-DD"; "01/15/1990" reformats to "MM/DD/YYYY".

    If the date cannot be parsed (unrecognised format), the original text is
    returned unchanged rather than failing.
    """
    fmt = _detect_date_format(matched_text)
    if fmt is None:
        return matched_text
    try:
        dt = datetime.strptime(matched_text, fmt)
    except ValueError:
        return matched_text
    range_size = max_days - min_days + 1
    offset_seed = int.from_bytes(span_key[:8], "big")
    shift = min_days + (offset_seed % range_size)
    shifted = dt + timedelta(days=shift)
    return shifted.strftime(fmt)


def _mask_span(
    span: Span,
    mask_key: bytes,
    strategy: str,
    cfg: dict[str, Any],
) -> str:
    """Dispatch one detected span to its configured masking strategy.

    Raw-value isolation: ``span.matched_text`` is consumed here and inside
    ``_span_key`` to produce keying material and drive the strategy. It is
    never written to any log or evidence output -- the logger writes only
    the strategy name and detector_id, never the value.
    """
    span_key = _span_key(mask_key, span.matched_text)
    if strategy == "fpe":
        return _mask_fpe(
            span.matched_text, span_key, span.detector_id, str(cfg.get("token", _DEFAULT_TOKEN))
        )
    if strategy == "faker":
        return _mask_faker(span.matched_text, span_key, span.detector_id)
    if strategy == "date_shift":
        min_days = int(cfg.get("min_days", -365))
        max_days = int(cfg.get("max_days", 365))
        return _mask_date_shift(span.matched_text, span_key, min_days, max_days)
    if strategy == "passthrough":
        return span.matched_text
    # "redact" and any unknown strategy: replace with token (fail-safe).
    return str(cfg.get("token", _DEFAULT_TOKEN))


def _apply_unmatched(text: str, policy: str, token: str) -> str:
    """Apply the unmatched_span_policy to a non-PII text segment.

    Called for each portion of the cell NOT covered by a detector match.

    ``redact`` (default): replace with ``token`` -- treats the segment as
        potentially undetected PII; safe but destroys surrounding context.
    ``passthrough``: keep verbatim -- operator opt-in for columns where
        the non-PII context is known safe.
    ``replace_with_token``: replace with ``_UNMATCHED_TOKEN`` ("``[UNMATCHED]``")
        -- lighter marker that distinguishes unmatched segments from
        per-span redaction tokens.

    Empty segments are returned as-is regardless of policy to avoid
    emitting spurious tokens when two spans are adjacent.
    """
    if not text:
        return text
    if policy == "passthrough":
        return text
    if policy == "replace_with_token":
        return _UNMATCHED_TOKEN
    # "redact" and any unknown policy: use token (fail-safe).
    return token


def mask_cell(
    text: Any,
    mask_key: bytes,
    *,
    detector_ids: list[str] | None = None,
    extra_spans: list[Span] | None = None,
    strategy_map: dict[str, str] | None = None,
    unmatched_span_policy: str = "redact",
    token: str = _DEFAULT_TOKEN,
    cfg: dict[str, Any] | None = None,
) -> Any:
    """Mask PII spans in a single free-text cell and return the masked string.

    Detects PII spans via ``storm.detectors.iter_spans`` (STORM single source
    of truth), dispatches each span to its configured strategy, and applies
    ``unmatched_span_policy`` to the non-PII portions. Returns the reassembled
    string.

    Non-string inputs (None, int, etc.) are returned unchanged to preserve
    null handling across all callers.

    Args:
        text:                 Cell value. Non-string returns unchanged.
        mask_key:             HMAC key material (RFC 2104): the 8-byte job_seed
                              (no secret) or a 32-byte KeyProvider mask root
                              (DE-02). Same key across all cells in a run ensures
                              cross-cell consistency: the same real value always
                              produces the same masked value.
        detector_ids:         Detector IDs to run. None = all span detectors.
                              Unknown IDs are silently skipped (``iter_spans``
                              contract).
        extra_spans:          Pre-computed spans to merge with built-in detection
                              results (e.g. NER hits from ``storm.ner.iter_ner_spans``).
                              Resolved via the same leftmost-then-longest overlap
                              sweep as built-in spans. Use this to mask Tier-2
                              detectors (person_name, iso_date, etc.) that are
                              not reachable via the built-in ``_SPAN_DETECTORS`` path.
        strategy_map:         Per-detector strategy overrides. Keys not in the
                              map fall back to ``DETECTOR_DEFAULTS``.
        unmatched_span_policy: Policy for text NOT covered by any detector match.
                              "redact" (default), "passthrough", or
                              "replace_with_token". WARNING: "passthrough" lets
                              any undetected text (including names and dates under
                              the built-in path) ride through unchanged.
        token:                Replacement token for "redact" unmatched policy and
                              for per-span redact strategy. Default "[REDACTED]".
        cfg:                  Extra strategy config (min_days, max_days for
                              date_shift, etc.).
    """
    if not isinstance(text, str) or not text:
        return text

    if unmatched_span_policy == "passthrough":
        _log.warning(
            "text_mask: unmatched_span_policy='passthrough' lets any text not covered by "
            "a detected span ride through unchanged. Built-in span detection covers only "
            "11 detectors (email, ssn, us_phone, us_zip, pan, iban, ipv4, icd10, npi, "
            "url, street_address). Names, addresses, and dates require NER (extra_spans=) "
            "or custom= patterns. Use the default 'redact' policy unless this column is "
            "known-safe."
        )

    effective_map: dict[str, str] = dict(DETECTOR_DEFAULTS)
    if strategy_map:
        effective_map.update(strategy_map)

    extra_cfg: dict[str, Any] = dict(cfg or {})
    extra_cfg.setdefault("token", token)

    spans: list[Span] = iter_spans(text, detector_ids, extra_spans=extra_spans)

    if not spans:
        # Entire cell is unmatched text.
        return _apply_unmatched(text, unmatched_span_policy, token)

    parts: list[str] = []
    cursor = 0
    for span in spans:
        if span.start > cursor:
            unmatched = text[cursor : span.start]
            parts.append(_apply_unmatched(unmatched, unmatched_span_policy, token))
        strategy = effective_map.get(span.detector_id, "redact")
        parts.append(_mask_span(span, mask_key, strategy, extra_cfg))
        cursor = span.end

    if cursor < len(text):
        remaining = text[cursor:]
        parts.append(_apply_unmatched(remaining, unmatched_span_policy, token))

    return "".join(parts)
