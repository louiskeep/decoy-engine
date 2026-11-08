"""The ``sub_floor_span: "synthetic"`` replacement for a text_mask span FF1 cannot encrypt.

Split from ``text_mask`` (module-size cap). Keyed character synthesis is RFC 2104 HMAC-SHA256 in
counter mode, the keyed-deterministic construction ``text_mask._span_key`` and ``derive_index``
use; check digits come from ``checksums.calc_check_digit``.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac_mod


def _synthetic_digits(seed_key: bytes, charset: str, length: int) -> str:
    """``length`` deterministic keyed characters over ``charset`` (RFC 2104
    HMAC-SHA256 counter-mode selection, the same style of keyed-deterministic
    construction as ``_span_key`` / ``derive_index`` elsewhere in the engine).
    Pure character generation, no format/checksum awareness -- callers place
    the result and, where a scheme applies, overwrite its check-digit
    position. Not FF1; carries no reversibility claim of any kind."""
    try:
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
    except BaseException:
        del seed_key
        raise


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
    try:
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
    except BaseException:
        del seed_key
        raise
