"""Where each draw-site provider lives after the `native/protocol/` split, and
that `_draw_site_providers` still re-exports the same objects."""

from __future__ import annotations

from decoy_engine.execution.native import _draw_site_providers as providers
from decoy_engine.execution.native.protocol import providers_base, providers_gen, providers_mask

_REGISTRY_MODULE = "decoy_engine.execution.native._draw_site_providers"
_ALLOWED_MODULES = {
    providers_base.__name__,
    providers_gen.__name__,
    providers_mask.__name__,
    _REGISTRY_MODULE,
    "decoy_engine.execution.native._draw_site_providers_gen_pool",
}


def test_every_registered_provider_class_lives_in_a_known_module() -> None:
    modules = {type(p).__module__ for p in providers.all_providers().values()}
    assert modules <= _ALLOWED_MODULES


def test_site_family_split() -> None:
    by_module: dict[str, set[str]] = {}
    for site, provider in providers.all_providers().items():
        by_module.setdefault(type(provider).__module__, set()).add(site.split(".")[0])
    assert by_module[providers_mask.__name__] == {"mask"}
    assert by_module[providers_gen.__name__] == {"gen"}


def test_text_mask_span_providers_stay_beside_span_key() -> None:
    assert providers._span_key.__module__ == _REGISTRY_MODULE
    assert providers.TextMaskDateShiftProvider.__module__ == _REGISTRY_MODULE
    assert providers.TextMaskFakerProvider.__module__ == _REGISTRY_MODULE


def test_reexports_are_the_defining_objects() -> None:
    assert providers.DrawSiteProvider is providers_base.DrawSiteProvider
    assert providers.DrawSiteProtocolError is providers_base.DrawSiteProtocolError
    assert providers.SourceKeyedHmacProvider is providers_base.SourceKeyedHmacProvider
    assert providers.ShuffleProvider is providers_mask.ShuffleProvider
    assert providers.FakerPerRowProvider is providers_gen.FakerPerRowProvider
    assert providers._hmac_hex is providers_mask._hmac_hex
