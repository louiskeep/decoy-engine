"""Thread-local reusable Faker instance for text_mask span synthesis.

``text_mask._mask_faker`` re-seeds a Faker instance per span (``seed_instance``
resets the generator before every draw), so a single reused instance produces
byte-identical output to constructing a fresh ``Faker()`` each span, at ~8-10x
lower cost (the per-span ``Faker()`` construction dominated the faker path; see
the C6b-fakerfix plan). The instance is thread-local so concurrent caller
threads never share a generator across a reseed->draw sequence (the GIL does not
protect the whole seed->draw window of a module-global instance).

Established methodology: Faker documents instance reuse with ``seed_instance``
as its reproducibility pattern; we do not roll our own synthesis.
"""

from __future__ import annotations

import threading

from faker import Faker

_FAKER_TLS = threading.local()


def shared_faker() -> Faker:
    """Return this thread's reused default-locale ``Faker``, creating it lazily.

    Callers MUST ``seed_instance`` before every draw; reuse is then byte-identical
    to a fresh ``Faker()`` (see module docstring). One instance per thread.
    """
    fake = getattr(_FAKER_TLS, "instance", None)
    if fake is None:
        fake = Faker()  # default locale/providers, identical to the old per-span Faker()
        _FAKER_TLS.instance = fake
    return fake
