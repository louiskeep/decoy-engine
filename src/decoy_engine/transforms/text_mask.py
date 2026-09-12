"""text_mask strategy (SP-07, 2026-06-28): span-level PII masking with per-detector dispatch.

Walks each cell with ``storm.detectors.iter_spans`` and masks each matched span
using a per-detector strategy. Non-matched text portions are controlled by
``unmatched_span_policy`` (default: ``redact``).

Cross-cell keyed determinism: for the ``faker`` and ``date_shift`` span
strategies, each span is masked by deriving a per-value key as
HMAC-SHA256(mask_key, matched_text) (``_span_key``). The same real value in
any two cells always produces the same masked value regardless of surrounding
context; the context is intentionally excluded from the key so cross-cell
consistency holds.

FF1 keying (Task 5.2 plan P3-final; BEHAVIOR CHANGE from the pre-FF1 model):
the ``fpe`` span strategy does NOT use ``_span_key``. It is keyed like the
column ``fpe`` strategy: a namespace-scoped, per-detector AES-256 key,
``derive(mask_key, f"text.{detector_id}", FF1_KEY_LABEL)``, with the FF1
tweak built from the detector id (``build_ff1_tweak(FF1_TWEAK_SCOPE_TEXT_SPAN,
detector_id)``). This reconciles the span model with the column model (same
key-derivation function, same tweak framing) instead of a bespoke
plaintext-keyed HMAC + detector-id-as-raw-tweak construction. Only the FF1
branch changes; ``faker``/``date_shift`` keep their plaintext-keyed
``_span_key`` model unchanged.

NER exception (TX-2, 2026-07-20): ``_span_key`` (still used by faker and
date_shift) is HMAC(mask_key, matched_text), context-free, so per-cell
reproducibility and in-core/out-of-core parity always hold for every span
source, and the cross-cell guarantee above holds in full for the regex
detectors (whose detector_id is a function of the value's shape alone). It
does NOT hold for the one context-SENSITIVE span source: opt-in spaCy NER
(``extra_spans=`` from ``storm.ner.iter_ner_spans``). NER assigns the
``detector_id`` (person_name vs location) from surrounding context, and the
synthesis STRATEGY + faker method are selected by detector_id, so an
ambiguous surface string (e.g. "Jordan" classified as a person in one cell
and a place in another) can synthesize to two different values across cells.
The span KEY is still identical in both cells; only the entity TYPE, and
therefore the chosen faker method, differs. Cross-cell synthetic consistency
is thus guaranteed only for unambiguous entities under NER; it is not a
regression in key/mask determinism.

STORM single source of truth: ``iter_spans`` is called directly from
``storm.detectors``; any detector added to ``_SPAN_DETECTORS`` is automatically
available to ``text_mask`` in the same release. No separate detector registry.

Raw-value isolation: ``matched_text`` is never emitted to logs or evidence.
Both the HMAC (``_span_key``) and the FF1 keying derive keying material from
``matched_text`` without ever logging it. Sentry tests
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

Sub-floor spans (Task 5.2 plan P3-final): FF1 is undefined/insecure below the
minimum admissible domain (``radix ** in_charset_length < FF1_MIN_DOMAIN``,
~1,000,000). Span detection cannot be pre-configured per field the way a
column can, so a sub-floor match (the common case: a 5-digit US ZIP, domain
10**5) cannot simply be routed to a wider charset at compile time. There is
no silent fallback: the operator must set ``sub_floor_span: "redact" |
"synthetic"`` (no default) whenever a configured detector's preset CAN
produce a sub-floor match; an unset policy on such a column fails closed. A
per-MATCH decision, not per-detector: ``us_zip`` matches both a 5-digit ZIP
(sub-floor) and a 9-digit ZIP+4 (10**9, clears the floor), so the SAME
detector routes some matches through FF1 and others through the configured
policy, decided by each match's own in-charset length. No configuration can
force a sub-floor match through FF1 anyway; the domain floor is enforced
inside ``transforms.fpe._permute`` itself, not by this module's config
reading. ``redact`` reuses the existing redaction token; ``synthetic``
produces a deterministic, non-reversible, valid-format replacement
(``_synthetic_span_value``). Both directions are irreversible by
construction, the honest consequence of a domain too small for FF1; this is
surfaced as a runtime warning (see ``mask_cell``'s ``sub_floor_notices``),
not a silent substitution.

Pattern: HMAC-SHA256 keyed span determinism (RFC 2104) for faker/date_shift;
NIST SP 800-38G FF1 (AES-256) for the fpe span strategy, matching the column
``fpe`` strategy (``transforms/fpe.py``, ``transforms/_ff1.py``).
See: https://datatracker.ietf.org/doc/html/rfc2104

Methodology: reuses STORM ``iter_spans`` (single detector source),
``fpe_encrypt_value`` (NIST SP 800-38G FF1; see ``transforms/fpe.py``), and
stdlib HMAC for the faker/date_shift per-span key derivation. Per-detector
defaults documented in ``DETECTOR_DEFAULTS``.

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

from decoy_engine.determinism import derive
from decoy_engine.errors import FpeChecksumError, FpeUnencryptableError
from decoy_engine.storm.detectors import Span, iter_spans
from decoy_engine.transforms.date_shift import _COMMON_FORMATS
from decoy_engine.transforms.fpe import (
    _CHARSETS,
    FF1_KEY_LABEL,
    FF1_TWEAK_SCOPE_TEXT_SPAN,
    build_ff1_tweak,
    fpe_encrypt_value,
)

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


def _synthetic_digits(seed_key: bytes, charset: str, length: int) -> str:
    """``length`` deterministic keyed characters over ``charset`` (RFC 2104
    HMAC-SHA256 counter-mode selection, the same style of keyed-deterministic
    construction as ``_span_key`` / ``derive_index`` elsewhere in the engine).
    Pure character generation, no format/checksum awareness -- callers place
    the result and, where a scheme applies, overwrite its check-digit
    position. Not FF1; carries no reversibility claim of any kind."""
    radix = len(charset)
    out: list[str] = []
    counter = 0
    while len(out) < length:
        block = _hmac_mod.new(
            seed_key, f"fpe-sub-floor-synthetic/v1:{counter}".encode(), hashlib.sha256
        ).digest()
        for byte in block:
            if len(out) >= length:
                break
            out.append(charset[byte % radix])
        counter += 1
    return "".join(out)


# Structural body rule per checksum scheme reachable through `_FPE_CONFIG`
# (pan -> luhn, npi -> npi): how many leading characters of the in-charset
# body are PINNED from the source (carrying no more information than the
# real FF1 checksum-mode encrypt path already pins the same way -- see
# `_fpe_checksum_permute`'s "npi" branch) versus synthesized, before the
# scheme's own check digit is computed and appended. Luhn has no structural
# pin (any digit string is a valid Luhn body); NPI pins its single leading
# digit (NPPES requires 1 or 2, so copying the source's actual leading digit
# keeps the synthetic value NPI-format-valid without leaking the other 9).
_SYNTHETIC_CHECKSUM_PINNED_PREFIX: dict[str, int] = {"luhn": 0, "npi": 1}


def _synthetic_span_value(
    seed_key: bytes, matched_text: str, charset: str, checksum: str | None
) -> str:
    """A deterministic, non-reversible, valid-format replacement for
    ``matched_text``, keyed on ``seed_key``.

    Used ONLY for the ``sub_floor_span`` ``"synthetic"`` policy: a span whose
    domain is below the FF1 floor, or whose checksum failed validation,
    cannot be format-preserving-ENCRYPTED (no bijection to invert, or no
    honest source to permute), but the operator may still want a
    plausible-looking, deterministic stand-in rather than a bare redaction
    token.

    Round-2 LOW-3 remediation: out-of-charset characters (format separators,
    e.g. the dashes in a PAN written ``4111-1111-1111-1111``) are preserved
    verbatim at their original positions -- mirroring the real FF1 path's
    ``preserve_separators=True`` -- rather than overwritten with synthetic
    digits. And for a checksum-configured detector (pan/npi), the LAST
    in-charset character is the scheme's own recomputed check digit (via
    ``checksums.calc_check_digit``), not another synthetic digit: a
    'valid-format' synthetic PAN/NPI must actually pass the scheme's check,
    the same claim the real encrypt path makes for its output.
    """
    charset_set = set(charset)
    positions = [i for i, ch in enumerate(matched_text) if ch in charset_set]
    body_len = len(positions)
    out = list(matched_text)
    if not positions:
        return matched_text

    pinned_prefix = _SYNTHETIC_CHECKSUM_PINNED_PREFIX.get(checksum) if checksum else None
    if checksum is not None and pinned_prefix is not None and body_len > pinned_prefix:
        # Checksum-backed: pin the scheme's structural prefix (if any) from
        # the source, synthesize the rest of the body, then overwrite the
        # last in-charset position with the recomputed check digit -- exactly
        # the pin/permute/check-digit shape `_fpe_checksum_permute` uses for
        # the same two schemes, substituting keyed synthesis for FF1
        # permutation on the body.
        from decoy_engine.checksums import calc_check_digit

        pinned = "".join(matched_text[positions[i]] for i in range(pinned_prefix))
        synth_body_len = body_len - pinned_prefix - 1  # minus pin, minus check digit
        synth_body = _synthetic_digits(seed_key, charset, synth_body_len)
        body = pinned + synth_body  # everything but the check digit
        check_digit = calc_check_digit(checksum, body)
        for offset, ch in enumerate(body):
            out[positions[offset]] = ch
        out[positions[-1]] = check_digit
    else:
        # No checksum scheme (ssn/us_phone/us_zip/fax_number), or the matched
        # span is too short for the scheme's own structural minimum (should
        # not happen given the detector's own regex already enforces length;
        # defensive fallback to plain synthetic digits rather than crash).
        synth = _synthetic_digits(seed_key, charset, body_len)
        for offset, ch in enumerate(synth):
            out[positions[offset]] = ch
    return "".join(out)


def _apply_sub_floor_span_policy(
    matched_text: str,
    seed_key: bytes,
    detector_id: str,
    token: str,
    *,
    reason_code: str,
    sub_floor_span_policy: str | None,
    sub_floor_notices: dict[str, int] | None,
) -> str:
    """Handle a span that FF1 cannot encrypt (sub-floor domain, or an
    invalid-checksum false-positive match), per the operator's
    ``sub_floor_span`` choice. No default: an unset policy fails closed.
    """
    if sub_floor_span_policy is None:
        raise FpeUnencryptableError(
            f"span detector {detector_id!r} matched a value FF1 cannot encrypt "
            f"({reason_code}) and no `sub_floor_span` policy ('redact' or "
            "'synthetic') is configured. The engine fails closed rather than "
            "silently choose a fallback; set sub_floor_span on this column.",
            code="fpe.unencryptable_domain",
        )
    if sub_floor_span_policy not in ("redact", "synthetic"):
        raise FpeUnencryptableError(
            f"unknown sub_floor_span policy {sub_floor_span_policy!r} for detector "
            f"{detector_id!r}; expected 'redact' or 'synthetic'.",
            code="fpe.unencryptable_domain",
        )
    if sub_floor_notices is not None:
        sub_floor_notices[detector_id] = sub_floor_notices.get(detector_id, 0) + 1
    # Visible, not silent (plan P3-final): a structured notice at WARNING, never
    # the matched_text itself (raw-value isolation).
    _log.warning(
        "text_mask: detector %s matched a value FF1 cannot encrypt (%s); applying "
        "the configured sub_floor_span policy %r. This value is NOT reversible.",
        detector_id,
        reason_code,
        sub_floor_span_policy,
    )
    if sub_floor_span_policy == "redact":
        return token
    charset_name, checksum = _FPE_CONFIG.get(detector_id, ("digits", None))
    charset = _CHARSETS.get(charset_name, charset_name)
    return _synthetic_span_value(seed_key, matched_text, charset, checksum)


def _mask_fpe(
    matched_text: str,
    mask_key: bytes,
    detector_id: str,
    token: str = _DEFAULT_TOKEN,
    *,
    sub_floor_span_policy: str | None = None,
    sub_floor_notices: dict[str, int] | None = None,
) -> str:
    """FPE-encrypt a span using the per-detector charset + checksum config.

    Task 5.2 plan P3-final: this is the ONE span strategy keyed like the
    column ``fpe`` strategy, NOT via ``_span_key``. The key is namespace-
    scoped per detector (``derive(mask_key, f"text.{detector_id}",
    FF1_KEY_LABEL)``) and the tweak is built from the detector id
    (``build_ff1_tweak(FF1_TWEAK_SCOPE_TEXT_SPAN, detector_id)``), matching
    the column model's key/tweak framing exactly. Uses ``fpe_encrypt_value``
    from ``transforms.fpe`` (NIST SP 800-38G FF1; see that module).

    A match whose in-charset domain falls below the FF1 minimum (the common
    case: a 5-digit US ZIP under the ``us_zip`` detector, which also matches
    9-digit ZIP+4 values that DO clear the floor) or whose value fails
    checksum validation for a checksum-backed detector (pan/npi) cannot be
    FF1'd; there is no silent fallback for either. Both route through
    ``_apply_sub_floor_span_policy`` under the SAME operator-chosen
    ``sub_floor_span`` policy, since both are "this specific match cannot be
    safely FF1'd" cases the operator must have an explicit answer for. Any
    OTHER ``FpeUnencryptableError``/``FpeChecksumError`` (a real
    config/wiring bug, not a per-match domain or checksum outcome)
    propagates and fails the job, per the plan's removal of the old
    catch-all `except -> static token` fallback.
    """
    cfg = _FPE_CONFIG.get(detector_id, ("digits", None))
    charset_name, checksum = cfg
    charset = _CHARSETS.get(charset_name, charset_name)
    span_namespace = f"text.{detector_id}"
    key = derive(mask_key, span_namespace, FF1_KEY_LABEL)
    tweak = build_ff1_tweak(FF1_TWEAK_SCOPE_TEXT_SPAN, detector_id)
    try:
        return fpe_encrypt_value(
            matched_text,
            key,
            charset,
            tweak,
            preserve_separators=True,
            validate_luhn=False,
            checksum=checksum,
        )
    except FpeUnencryptableError as exc:
        if exc.code != "fpe.unencryptable_domain":
            raise
        return _apply_sub_floor_span_policy(
            matched_text,
            key,
            detector_id,
            token,
            reason_code="sub_minimum_domain",
            sub_floor_span_policy=sub_floor_span_policy,
            sub_floor_notices=sub_floor_notices,
        )
    except FpeChecksumError as exc:
        if exc.code not in ("fpe.checksum_invalid_source", "fpe.checksum_unsupported"):
            raise
        return _apply_sub_floor_span_policy(
            matched_text,
            key,
            detector_id,
            token,
            reason_code="checksum_invalid",
            sub_floor_span_policy=sub_floor_span_policy,
            sub_floor_notices=sub_floor_notices,
        )


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
    *,
    sub_floor_span_policy: str | None = None,
    sub_floor_notices: dict[str, int] | None = None,
) -> str:
    """Dispatch one detected span to its configured masking strategy.

    Raw-value isolation: ``span.matched_text`` is consumed here (and inside
    ``_span_key`` / ``_mask_fpe``'s own keying) to produce keying material and
    drive the strategy. It is never written to any log or evidence output;
    the logger writes only the strategy name and detector_id, never the value.

    Task 5.2 plan P3-final: the ``fpe`` branch is keyed independently of
    ``_span_key`` (see ``_mask_fpe``), so it is handled BEFORE ``_span_key``
    is computed; ``faker``/``date_shift`` still use it, unchanged.
    """
    if strategy == "fpe":
        return _mask_fpe(
            span.matched_text,
            mask_key,
            span.detector_id,
            str(cfg.get("token", _DEFAULT_TOKEN)),
            sub_floor_span_policy=sub_floor_span_policy,
            sub_floor_notices=sub_floor_notices,
        )
    span_key = _span_key(mask_key, span.matched_text)
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
    sub_floor_span_policy: str | None = None,
    sub_floor_notices: dict[str, int] | None = None,
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
        sub_floor_span_policy: Task 5.2 plan P3-final. Required ("redact" or
                              "synthetic") whenever a configured detector's fpe
                              strategy can produce a sub-FF1-floor match (the
                              common case: a 5-digit us_zip). No default: a
                              sub-floor match with this left ``None`` raises
                              ``FpeUnencryptableError`` rather than choose a
                              fallback silently. Ignored by columns that never
                              hit a sub-floor match.
        sub_floor_notices:    Optional mutable ``{detector_id: count}`` the
                              caller supplies to learn how many sub-floor spans
                              were handled under the configured policy, so it
                              can surface an aggregate structured warning (this
                              function's own per-span log line at WARNING is
                              the always-on signal; this dict is for a
                              caller-level rollup, e.g. one QualityWarning per
                              column instead of one log line per cell).
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
        parts.append(
            _mask_span(
                span,
                mask_key,
                strategy,
                extra_cfg,
                sub_floor_span_policy=sub_floor_span_policy,
                sub_floor_notices=sub_floor_notices,
            )
        )
        cursor = span.end

    if cursor < len(text):
        remaining = text[cursor:]
        parts.append(_apply_unmatched(remaining, unmatched_span_policy, token))

    return "".join(parts)
