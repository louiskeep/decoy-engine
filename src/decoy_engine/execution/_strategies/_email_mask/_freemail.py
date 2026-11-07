"""The free-mail domains `email_mask` keeps in `keep: domain_class` mode.

A short, conservative list of consumer mail providers with millions of users each. A domain on
this list is kept (in its spelling here); every other domain is replaced by a reserved class
domain. A wrong "free-mail" entry would keep an exact, possibly identifying domain, while a
missing one only replaces a domain, so the list errs short.

Provenance (domain names are facts; the selection is ours, nothing is copied under a licence):

- Faker 40.23 per-locale ``free_email_domains`` (``faker.providers.internet.<locale>``): the
  first group below.
- npm ``free-email-domains`` 1.15.29 (Kikobeats, MIT; built from HubSpot's blocked-domains list),
  tarball sha256 ``e44182759683cd45c9b34231313fdfab8f466cc6abbd138ff61d548fcf25d384``: the second
  group, and every entry of the first group is also in it (checked 2026-11-07).

Changing the list changes `domain_class` output, so additions are reviewed one by one, each with
a source, and pinned by a digest test.
"""

from __future__ import annotations

_FROM_FAKER = (
    "daum.net",
    "free.fr",
    "gmail.com",
    "gmx.de",
    "googlemail.com",
    "hanmail.net",
    "hotmail.co.uk",
    "hotmail.com",
    "hotmail.de",
    "hotmail.fr",
    "hotmail.it",
    "icloud.com",
    "laposte.net",
    "libero.it",
    "live.com",
    "mail.ru",
    "naver.com",
    "orange.fr",
    "outlook.com",
    "protonmail.com",
    "web.de",
    "yahoo.co.uk",
    "yahoo.com",
    "yahoo.com.au",
    "yahoo.com.br",
    "yahoo.de",
    "yahoo.fr",
    "yandex.com",
    "yandex.ru",
    "ymail.com",
)

_FROM_NPM_FREE_EMAIL_DOMAINS = (
    "126.com",
    "163.com",
    "aol.com",
    "fastmail.com",
    "gmx.com",
    "gmx.net",
    "mac.com",
    "mail.com",
    "me.com",
    "msn.com",
    "proton.me",
    "qq.com",
    "rediffmail.com",
    "t-online.de",
    "zoho.com",
)

FREEMAIL_DOMAINS: frozenset[str] = frozenset(_FROM_FAKER + _FROM_NPM_FREE_EMAIL_DOMAINS)
