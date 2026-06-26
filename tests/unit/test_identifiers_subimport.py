"""F9: the identifier sub-import re-exports the families from providers_v2.

The identifier validators/adapters/domains were trimmed from the top-level
``decoy_engine.__all__`` and re-homed under ``decoy_engine.identifiers``.
This guards that the sub-import resolves and stays in sync with the source
package's objects.
"""

import decoy_engine.identifiers as identifiers
from decoy_engine.providers_v2 import identifiers as source


def test_subimport_exports_identifier_families():
    expected = {
        "EinAdapter",
        "EinDomain",
        "EinValidator",
        "IdentifierError",
        "IdentifierFormatError",
        "MrnAdapter",
        "MrnDomain",
        "MrnValidator",
        "NdcAdapter",
        "NdcDomain",
        "NdcValidator",
        "NpiAdapter",
        "NpiDomain",
        "NpiValidator",
        "SsnAdapter",
        "SsnDomain",
        "SsnValidator",
    }
    assert set(identifiers.__all__) == expected


def test_subimport_objects_are_the_source_objects():
    for name in identifiers.__all__:
        assert getattr(identifiers, name) is getattr(source, name)


def test_top_level_attribute_access_still_resolves():
    # The top-level module bindings are kept for backward compatibility even
    # though these names are no longer in decoy_engine.__all__.
    import decoy_engine

    assert decoy_engine.EinValidator is identifiers.EinValidator
    assert decoy_engine.IdentifierError is identifiers.IdentifierError
