"""The pure core of email_mask: parse rule, classifier, output domain and the free-mail list."""

from __future__ import annotations

import hashlib

import pytest

from decoy_engine.execution._strategies._email_mask import _address as address
from decoy_engine.execution._strategies._email_mask._freemail import FREEMAIL_DOMAINS

VALID = {
    "alice@acme.com": "acme.com",
    "Bob@Gmail.com": "Gmail.com",
    '"john doe"@acme.com': "acme.com",
    '"a@b"@acme.com': "acme.com",
    "a@b@acme.com": "acme.com",
    "ü@bücher.de": "bücher.de",
    "x@xn--bcher-kva.de": "xn--bcher-kva.de",
    "x@a-b.co.uk": "a-b.co.uk",
    "x@1.example": "1.example",
    "x@" + "a" * 63 + ".com": "a" * 63 + ".com",
}

MALFORMED = [
    "",
    "not-an-email",
    "@acme.com",
    "a@",
    "a@acme.com\n",
    "a@acme.com ",
    " a@acme.com\t",
    "a@acme.com.",
    "a@[192.0.2.1]",
    "a@192.0.2.1",
    "a@-acme.com",
    "a@acme-.com",
    "a@acme..com",
    "a@.acme.com",
    "a@acme",
    "a@acme.com (work)",
    "a@acme_corp.com",
    "a@" + "a" * 64 + ".com",
    "a@" + ".".join(["abcdefg"] * 32) + ".com",
    "a@acme.12",
    # Unicode alphanumerics that are not letters, marks or ASCII digits.
    "a@gmail.co\u00b2",
    "a@\u217dom.com",
    "a@acme\u0663.com",
    12345,
    b"a@acme.com",
]


@pytest.mark.parametrize(("value", "domain"), sorted(VALID.items()))
def test_a_valid_address_yields_the_text_after_the_last_at(value: str, domain: str) -> None:
    assert address.split_domain(value) == domain


@pytest.mark.parametrize("value", MALFORMED, ids=repr)
def test_a_malformed_value_yields_no_domain(value: object) -> None:
    assert address.split_domain(value) is None


def test_the_253_character_limit_is_inclusive() -> None:
    label = "a" * 61
    domain = ".".join([label] * 4) + ".c"  # 4*61 + 4 dots + 1 = 249
    domain = "b" * (253 - len(domain) - 1) + "." + domain
    assert len(domain) == 253
    assert address.split_domain("x@" + domain) == domain
    assert address.split_domain("x@b" + domain) is None


@pytest.mark.parametrize(
    ("domain", "klass"),
    [
        ("gmail.com", "freemail"),
        ("GMail.COM", "freemail"),
        ("acme.com", "corporate"),
        ("mail.gmail.com", "corporate"),
        ("cs.stanford.edu", "edu"),
        ("ox.ac.uk", "edu"),
        ("unimelb.edu.au", "edu"),
        ("agency.gov", "gov"),
        ("dwp.gov.uk", "gov"),
        ("army.mil", "gov"),
        ("interieur.gouv.fr", "gov"),
        ("mof.go.jp", "gov"),
        ("sat.gob.mx", "gov"),
        ("ird.govt.nz", "gov"),
        ("ac.com", "corporate"),
        ("go.com", "corporate"),
        ("gov.com", "corporate"),
        ("edu.example.com", "corporate"),
        ("bücher.de", "corporate"),
        ("xn--bcher-kva.edu", "edu"),
        # KELVIN SIGN lower-cases to ASCII "k"; a non-ASCII domain must never match a rule.
        (chr(0x212A) + "orea.gov", "corporate"),
    ],
)
def test_domain_class(domain: str, klass: str) -> None:
    assert address.domain_class(domain) == klass


def test_output_domain_keep_domain_is_the_domain_as_written() -> None:
    assert address.output_domain("Gmail.com", "domain") == "Gmail.com"
    assert address.output_domain("Acme.COM", "domain") == "Acme.COM"


@pytest.mark.parametrize(
    ("domain", "expected"),
    [
        ("Gmail.COM", "gmail.com"),
        ("gmail.com", "gmail.com"),
        ("acme.com", "corporate.example"),
        ("cs.stanford.edu", "edu.example"),
        ("dwp.gov.uk", "gov.example"),
    ],
)
def test_output_domain_keep_class_uses_canonical_or_reserved_domains(
    domain: str, expected: str
) -> None:
    assert address.output_domain(domain, "domain_class") == expected


@pytest.mark.parametrize("keep", ["domain", "domain_class"])
def test_a_malformed_value_gets_the_invalid_domain_in_both_modes(keep: str) -> None:
    assert address.output_domain(None, keep) == "unknown.invalid"


def test_keep_choices_are_the_two_tokens() -> None:
    assert address.KEEP_CHOICES == ("domain", "domain_class")


def test_reserved_domains_are_under_reserved_tlds() -> None:
    # RFC 2606 section 2: .example and .invalid never resolve to a real organisation.
    for domain in (*address.CLASS_DOMAINS.values(), address.MALFORMED_DOMAIN):
        assert domain.endswith((".example", ".invalid")), domain


# --- the committed free-mail list ---

FREEMAIL_DIGEST = "0982be7cfc7a0830fbe374d406013e1f2855535a6d5b24e2efe2142f84f54171"


def test_the_free_mail_list_is_pinned() -> None:
    digest = hashlib.sha256("\n".join(sorted(FREEMAIL_DOMAINS)).encode("ascii")).hexdigest()
    assert digest == FREEMAIL_DIGEST
    assert len(FREEMAIL_DOMAINS) == 45


def test_every_free_mail_entry_is_a_canonical_parseable_domain() -> None:
    for domain in FREEMAIL_DOMAINS:
        assert domain.isascii() and domain == domain.lower(), domain
        assert address.split_domain(f"x@{domain}") == domain
        assert address.domain_class(domain) == "freemail"


def test_no_government_or_education_domain_is_listed_as_free_mail() -> None:
    for domain in FREEMAIL_DOMAINS:
        labels = domain.split(".")
        assert not {"gov", "gouv", "govt", "gob", "mil", "edu", "ac"} & set(labels), domain
