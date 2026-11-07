"""Address parsing and domain handling for the `email_mask` strategy.

Pattern: Domain-preserving email masking (Delphix Email algorithm framework, "Preserve Value"
domain with a keyed name mask; Tonic excluded-domains list; RFC 5321 / RFC 5322 addr-spec;
RFC 1035 / RFC 1123 host labels; RFC 2606 reserved domains). See: RFC 5321 section 4.1.2,
RFC 2606 section 2.

The domain is the text after the LAST '@' (a domain cannot contain '@'; a quoted local part
can). The local part is never parsed: the strategy replaces it whole, so quoted forms, dots and a
stray '@' cannot leak. The domain must pass the host-label rule below or the value is malformed;
RFC 6531 allows UTF-8 labels, so a Unicode letter or combining mark counts as a label character
(the caller passes NFC text); digits are ASCII only. This is a narrow subset of IDNA2008, not
a full check.

`domain_class` is an ARX-style one-step generalisation (value to class). The rules are short and
lean to "corporate", which only replaces a domain: a non-ASCII domain never matches a rule,
because some non-ASCII characters lower-case to ASCII.

No key material passes through this module.
"""

from __future__ import annotations

import unicodedata

from decoy_engine.execution._strategies._email_mask._freemail import FREEMAIL_DOMAINS

KEEP_DOMAIN = "domain"
KEEP_DOMAIN_CLASS = "domain_class"
KEEP_CHOICES: tuple[str, ...] = (KEEP_DOMAIN, KEEP_DOMAIN_CLASS)

# RFC 2606 section 2 reserves `.example` and `.invalid`: no real organisation can own these.
MALFORMED_DOMAIN = "unknown.invalid"
CLASS_DOMAINS: dict[str, str] = {
    "gov": "gov.example",
    "edu": "edu.example",
    "corporate": "corporate.example",
}

_MAX_DOMAIN = 253
_MAX_LABEL = 63
_GOV_TLDS = frozenset({"gov", "mil"})
_GOV_SECOND_LEVEL = frozenset({"gov", "gouv", "govt", "gob", "go", "mil"})
_EDU_SECOND_LEVEL = frozenset({"ac", "edu"})
_ASCII_DIGITS = frozenset("0123456789")


def _letter_or_mark(ch: str) -> bool:
    return unicodedata.category(ch)[0] in "LM"


def _label_ok(label: str) -> bool:
    if not 1 <= len(label) <= _MAX_LABEL or label[0] == "-" or label[-1] == "-":
        return False
    # Not str.isalnum: that accepts superscripts, Roman numerals and non-ASCII digits.
    return all(ch == "-" or ch in _ASCII_DIGITS or _letter_or_mark(ch) for ch in label)


def split_domain(value: object) -> str | None:
    """The domain of `value` as written, or None when `value` is not a usable address."""
    if not isinstance(value, str):
        return None
    local, at, domain = value.rpartition("@")
    if not at or not local or len(domain) > _MAX_DOMAIN:
        return None
    labels = domain.split(".")
    if len(labels) < 2 or not all(_label_ok(label) for label in labels):
        return None
    # An all-digit last label is an IPv4 address, not a host name.
    if not any(ch.isalpha() for ch in labels[-1]):
        return None
    return domain


def _is_country_code(label: str) -> bool:
    return len(label) == 2 and label.isalpha()


def domain_class(domain: str) -> str:
    """`freemail`, `gov`, `edu` or `corporate` for a domain `split_domain` accepted."""
    if not domain.isascii():
        return "corporate"
    lowered = domain.lower()
    if lowered in FREEMAIL_DOMAINS:
        return "freemail"
    labels = lowered.split(".")
    tld, second = labels[-1], labels[-2]
    cc = _is_country_code(tld)
    if tld in _GOV_TLDS or (cc and second in _GOV_SECOND_LEVEL):
        return "gov"
    if tld == "edu" or (cc and second in _EDU_SECOND_LEVEL):
        return "edu"
    return "corporate"


def _keep_domain(domain: str) -> str:
    return domain


def _keep_class(domain: str) -> str:
    klass = domain_class(domain)
    if klass == "freemail":
        # The list's spelling, so the source's casing (a per-user habit) is not kept.
        return domain.lower()
    return CLASS_DOMAINS[klass]


_OUTPUT = {KEEP_DOMAIN: _keep_domain, KEEP_DOMAIN_CLASS: _keep_class}


def output_domain(domain: str | None, keep: str) -> str:
    """The domain written after the masked local part."""
    if domain is None:
        return MALFORMED_DOMAIN
    return _OUTPUT[keep](domain)
