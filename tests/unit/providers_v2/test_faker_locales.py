"""Faker locale behavior: unchanged output, coded refusals, advertised rows.

The golden digests were captured on origin/main before the locale change
(seeded batches of 25 values per provider and locale form), so a change to
any value a working config produces fails here.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine import reasons
from decoy_engine.config import PipelineConfig
from decoy_engine.execution import run_pipeline
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.providers_v2._adapter import ProviderSpec
from decoy_engine.providers_v2._errors import AdapterError
from decoy_engine.providers_v2._faker_adapter import (
    FakerAdapter,
    _unregister_faker_provider_v2,
    register_faker_provider_v2,
)

# address_city with locale "it" is left out: it is not reproducible across
# processes on origin/main either (a pre-existing determinism bug, recorded in
# the slice plan), so it cannot be pinned.
GOLDEN: dict[str, str] = {
    "person_first_name|en_US+de_DE": "6798e1da3de3ea299585545db604b77a6733c6680d7dfb79b76d1d9b293da4c7",
    "address_zip|en_US+de_DE": "60042dcfc3bba79e6f45d7b52f133f1db9eb64f4bcdf8dcc06decb8dac9d2c19",
    "person_dob|en_US+de_DE": "0f2d60e978ac153d5bd35b31a3a22080e4f63e3ea135ea796543dea4c9b1734b",
    "person_phone|en_US+de_DE": "d84abaa4db3498a5d575e1c7afb05b2deebe97352fff12afce51d1f5c04fe661",
    "address_city|None": "59d9838143b23e1c42c03501823be240249b51c7272ff7bded7b4d88652c3c7e",
    "address_city|de_DE": "6e8cd8aebaa9537b1c4e9393671754466aac7411b5c19c9401d9671e9802a346",
    "address_city|de_de": "6e8cd8aebaa9537b1c4e9393671754466aac7411b5c19c9401d9671e9802a346",
    "address_city|en_US": "59d9838143b23e1c42c03501823be240249b51c7272ff7bded7b4d88652c3c7e",
    "address_city|pt-BR": "23ff649813daa59c5feec64a461f24cba1741389c0d1b5fed94fd42097c3a49e",
    "address_full|None": "078c2684d206554afa909ddebe60a2755e621f0531f0c6a68cc8340037c8593e",
    "address_full|de_DE": "d3e73ce36b06db7d85615f88578dd328435222abe68cf03c5d7465953f5d1670",
    "address_full|de_de": "d3e73ce36b06db7d85615f88578dd328435222abe68cf03c5d7465953f5d1670",
    "address_full|en_US": "078c2684d206554afa909ddebe60a2755e621f0531f0c6a68cc8340037c8593e",
    "address_full|it": "3e7a522c9b0eaa5f5b5a9192032ca9145a04961b90fc4344da1c878bbd09b844",
    "address_full|pt-BR": "60996364fec7e7a6584ed8f9ad5e71f07de926daa6ac122b46b208da7fc9d301",
    "address_state|None": "0d4bc41e40cf12b65cfb17910505c2fe3ba5afc7de2a210105a4161109e8ed9a",
    "address_state|de_DE": "733b70c99c76232f0eca94b2aa632ede5db17cab161810dcc8e8db6ae021c02d",
    "address_state|de_de": "733b70c99c76232f0eca94b2aa632ede5db17cab161810dcc8e8db6ae021c02d",
    "address_state|en_US": "0d4bc41e40cf12b65cfb17910505c2fe3ba5afc7de2a210105a4161109e8ed9a",
    "address_state|it": "18ce608c7c549d8332029b77e1684b8ea988eb29840fd884da39724f0b3e30f5",
    "address_state|pt-BR": "9381ed41925604c83db2e76a8ffeb6fb038b230f8c059bf284fbd30c4313a5bc",
    "address_street|None": "bc88836d6e47f564af25813908a80218586bb5c15b369b704ced92b4316cc717",
    "address_street|de_DE": "b008b62b8d997b1de9c3ae450982ce0ae3fdcf9fce44cf32d81f62b0309bf84b",
    "address_street|de_de": "b008b62b8d997b1de9c3ae450982ce0ae3fdcf9fce44cf32d81f62b0309bf84b",
    "address_street|en_US": "bc88836d6e47f564af25813908a80218586bb5c15b369b704ced92b4316cc717",
    "address_street|it": "ad0bd85e7e41f26645bdf3ac652fac88a741592cf12132d7f17a45c4f0f953b1",
    "address_street|pt-BR": "b52c91dd1a161e5820423fe6ff7d413d3b55c196ccc69cfbf280eb84bc051d78",
    "address_zip|None": "a9b6d445452eead7783dad868e6c0296d3c048801a5765aaa57e829f129e7aeb",
    "address_zip|de_DE": "4b3ca35d4512bd2c907333f75ae0b7d53046427b4d24811a1ba405928a1ed499",
    "address_zip|de_de": "4b3ca35d4512bd2c907333f75ae0b7d53046427b4d24811a1ba405928a1ed499",
    "address_zip|en_US": "a9b6d445452eead7783dad868e6c0296d3c048801a5765aaa57e829f129e7aeb",
    "address_zip|it": "94dcbb456be7aaf94eb45b24468e56ea2727cc187c3b3916503cdd1ae30f7171",
    "address_zip|pt-BR": "5aaad849ebe214941a380181167e955da11a3d33084b63c0d0a34cd9cb3ee003",
    "lorem_text|None": "bd0668e9576224af3dec37b234fcdc26d2d4b653950522b0fadb7a3ea9425503",
    "lorem_text|de_DE": "4434dbd8506aee09744a67e27354d68aa9dd46f307625de6aa997063cce02440",
    "lorem_text|de_de": "4434dbd8506aee09744a67e27354d68aa9dd46f307625de6aa997063cce02440",
    "lorem_text|en_US": "bd0668e9576224af3dec37b234fcdc26d2d4b653950522b0fadb7a3ea9425503",
    "lorem_text|it": "1d309834c523fd8b092b4f5b8bee04eb762c04151044a5888369c8bc1a3aa3ea",
    "lorem_text|pt-BR": "e43a4ef0dedb1d1ead38f933528594faf640cb0950158f49ff010cbb83c42e8d",
    "person_dob|None": "1b86f9b8649beb854be1552368b09634bfa89a616e1e081ce1786983eb5515ca",
    "person_dob|de_DE": "1b86f9b8649beb854be1552368b09634bfa89a616e1e081ce1786983eb5515ca",
    "person_dob|de_de": "1b86f9b8649beb854be1552368b09634bfa89a616e1e081ce1786983eb5515ca",
    "person_dob|en_US": "1b86f9b8649beb854be1552368b09634bfa89a616e1e081ce1786983eb5515ca",
    "person_dob|it": "1b86f9b8649beb854be1552368b09634bfa89a616e1e081ce1786983eb5515ca",
    "person_dob|pt-BR": "1b86f9b8649beb854be1552368b09634bfa89a616e1e081ce1786983eb5515ca",
    "person_email|None": "5e89911a8970f09e62b2315f2cd73617ea614a9bef73597972659e226811b881",
    "person_email|de_DE": "5fa5633d33833b063636f2529a6a522eb2e909fbd1df98b29e2df4a615242175",
    "person_email|de_de": "5fa5633d33833b063636f2529a6a522eb2e909fbd1df98b29e2df4a615242175",
    "person_email|en_US": "5e89911a8970f09e62b2315f2cd73617ea614a9bef73597972659e226811b881",
    "person_email|it": "690165b2843938b30c3247fc2b39f70c50089c7b1b911db2822840d74b3f9fd5",
    "person_email|pt-BR": "ef351a3760598b14bbbb114eee46498310338a36f045533f6077707ef3eb7565",
    "person_first_name|None": "6ad60c31aa80c9dd0c0566b30071d74fd841891a854cd0e4a08e8b251ca22313",
    "person_first_name|de_DE": "98c9e8723626553ba005218920649bff9c59e5d5c0881b8a8a9ab50f62e56c30",
    "person_first_name|de_de": "98c9e8723626553ba005218920649bff9c59e5d5c0881b8a8a9ab50f62e56c30",
    "person_first_name|en_US": "6ad60c31aa80c9dd0c0566b30071d74fd841891a854cd0e4a08e8b251ca22313",
    "person_first_name|it": "b07a1a25486cc2f8f3fc1b84c6d00833103b4d1477fac6cfb8bb90c3e49a0e27",
    "person_first_name|pt-BR": "de6b04a46361f60c470ccdf6d24556653e475e49c0976488814dca7b14b00c61",
    "person_full_name|None": "ddf6066360d9901ed5e831390ea8d8796f384245f950b417ca80a5e382620dc2",
    "person_full_name|de_DE": "95018448fdce2769cf6a1371bb77431bf472fdf7fb063b65ef11c567f6f67d96",
    "person_full_name|de_de": "95018448fdce2769cf6a1371bb77431bf472fdf7fb063b65ef11c567f6f67d96",
    "person_full_name|en_US": "ddf6066360d9901ed5e831390ea8d8796f384245f950b417ca80a5e382620dc2",
    "person_full_name|it": "5e10c27d2b213c12d5ce00fe0a2b63c394eedc4acec84de37a305ff2d5582717",
    "person_full_name|pt-BR": "c4cc414049391171a02e7080e80b2e4bedeb0aa31ef056cb95db1a28a32de246",
    "person_last_name|None": "afd19014163c6f59403887c9f2aa020920b038256f793719cb047d2cad57cc04",
    "person_last_name|de_DE": "732c9962ab218b870f5ded97758fe3bfa923f0dee260b855c3b74539b07ed188",
    "person_last_name|de_de": "732c9962ab218b870f5ded97758fe3bfa923f0dee260b855c3b74539b07ed188",
    "person_last_name|en_US": "afd19014163c6f59403887c9f2aa020920b038256f793719cb047d2cad57cc04",
    "person_last_name|it": "64ac6152fed4efab07cecf6913bfe63bc5c1bc74fa059c91e7f3c79a22e910a6",
    "person_last_name|pt-BR": "24df8952e2aa5af7e4ba6b033eac82ba0e1cfc92fea6e2300c9dc3ea2cf7b1cb",
    "person_name|None": "ddf6066360d9901ed5e831390ea8d8796f384245f950b417ca80a5e382620dc2",
    "person_name|de_DE": "95018448fdce2769cf6a1371bb77431bf472fdf7fb063b65ef11c567f6f67d96",
    "person_name|de_de": "95018448fdce2769cf6a1371bb77431bf472fdf7fb063b65ef11c567f6f67d96",
    "person_name|en_US": "ddf6066360d9901ed5e831390ea8d8796f384245f950b417ca80a5e382620dc2",
    "person_name|it": "5e10c27d2b213c12d5ce00fe0a2b63c394eedc4acec84de37a305ff2d5582717",
    "person_name|pt-BR": "c4cc414049391171a02e7080e80b2e4bedeb0aa31ef056cb95db1a28a32de246",
    "person_phone|None": "8ace22d7d96eb4d1728aee55963f5a769c92e52c8fbd44d99c68084647f8cd39",
    "person_phone|de_DE": "400f5be131baa4554f7c22f79fb220f84f45c6389f6064157cee5f385ed872f5",
    "person_phone|de_de": "400f5be131baa4554f7c22f79fb220f84f45c6389f6064157cee5f385ed872f5",
    "person_phone|en_US": "8ace22d7d96eb4d1728aee55963f5a769c92e52c8fbd44d99c68084647f8cd39",
    "person_phone|it": "afa5f3c184b0ca0e8bb64ece68441930812e0b1356aa51fb306ee48aa0abf932",
    "person_phone|pt-BR": "58b8fef5282681e863147b082ebdf7a4a15ce1d4f20087ec9668372024cc66be",
    "random_choice|None": "950269b14586875a133ced8604e28bb87b6ad5d057cf362421d965f621144f08",
    "random_choice|de_DE": "950269b14586875a133ced8604e28bb87b6ad5d057cf362421d965f621144f08",
    "random_choice|de_de": "950269b14586875a133ced8604e28bb87b6ad5d057cf362421d965f621144f08",
    "random_choice|en_US": "950269b14586875a133ced8604e28bb87b6ad5d057cf362421d965f621144f08",
    "random_choice|it": "950269b14586875a133ced8604e28bb87b6ad5d057cf362421d965f621144f08",
    "random_choice|pt-BR": "950269b14586875a133ced8604e28bb87b6ad5d057cf362421d965f621144f08",
    "random_int_range|None": "6159367e4c96dae7bcb63d43baa973d97c37d4781265f08876610b33b74ab473",
    "random_int_range|de_DE": "6159367e4c96dae7bcb63d43baa973d97c37d4781265f08876610b33b74ab473",
    "random_int_range|de_de": "6159367e4c96dae7bcb63d43baa973d97c37d4781265f08876610b33b74ab473",
    "random_int_range|en_US": "6159367e4c96dae7bcb63d43baa973d97c37d4781265f08876610b33b74ab473",
    "random_int_range|it": "6159367e4c96dae7bcb63d43baa973d97c37d4781265f08876610b33b74ab473",
    "random_int_range|pt-BR": "6159367e4c96dae7bcb63d43baa973d97c37d4781265f08876610b33b74ab473",
    "synthetic_account_number|None": "6c3987dbea2b48b6dfaedc5e0dbac855b9310ca615a3d6748cefb7c75a6e8f7b",
    "synthetic_account_number|de_DE": "2a01a5287b4f2a59619734644450d8d5752b0029125c123f978ef3da65e1df27",
    "synthetic_account_number|de_de": "2a01a5287b4f2a59619734644450d8d5752b0029125c123f978ef3da65e1df27",
    "synthetic_account_number|en_US": "6c3987dbea2b48b6dfaedc5e0dbac855b9310ca615a3d6748cefb7c75a6e8f7b",
    "synthetic_account_number|it": "34053fd96cf02deac4db4c268d3a844af9f43647b14bfc68ff4c959797dfaee9",
    "synthetic_account_number|pt-BR": "6c3987dbea2b48b6dfaedc5e0dbac855b9310ca615a3d6748cefb7c75a6e8f7b",
    "synthetic_member_id|None": "e887ac39b4698e195e33da4abd83cf8bc8a055e7d0ccccb8894d825f66b3451c",
    "synthetic_member_id|de_DE": "e887ac39b4698e195e33da4abd83cf8bc8a055e7d0ccccb8894d825f66b3451c",
    "synthetic_member_id|de_de": "e887ac39b4698e195e33da4abd83cf8bc8a055e7d0ccccb8894d825f66b3451c",
    "synthetic_member_id|en_US": "e887ac39b4698e195e33da4abd83cf8bc8a055e7d0ccccb8894d825f66b3451c",
    "synthetic_member_id|it": "e887ac39b4698e195e33da4abd83cf8bc8a055e7d0ccccb8894d825f66b3451c",
    "synthetic_member_id|pt-BR": "e887ac39b4698e195e33da4abd83cf8bc8a055e7d0ccccb8894d825f66b3451c",
    "synthetic_plan_id|None": "23aba221d4fa1a4448f2d9bc131c1b629bdd358c2a408b181f56f4c4731fb1e5",
    "synthetic_plan_id|de_DE": "23aba221d4fa1a4448f2d9bc131c1b629bdd358c2a408b181f56f4c4731fb1e5",
    "synthetic_plan_id|de_de": "23aba221d4fa1a4448f2d9bc131c1b629bdd358c2a408b181f56f4c4731fb1e5",
    "synthetic_plan_id|en_US": "23aba221d4fa1a4448f2d9bc131c1b629bdd358c2a408b181f56f4c4731fb1e5",
    "synthetic_plan_id|it": "23aba221d4fa1a4448f2d9bc131c1b629bdd358c2a408b181f56f4c4731fb1e5",
    "synthetic_plan_id|pt-BR": "23aba221d4fa1a4448f2d9bc131c1b629bdd358c2a408b181f56f4c4731fb1e5",
    "uuid|None": "5a1f2df794bc5331d9794761b18e14a4a0ff6c2be10a959ac988ce1c17581f06",
    "uuid|de_DE": "5a1f2df794bc5331d9794761b18e14a4a0ff6c2be10a959ac988ce1c17581f06",
    "uuid|de_de": "5a1f2df794bc5331d9794761b18e14a4a0ff6c2be10a959ac988ce1c17581f06",
    "uuid|en_US": "5a1f2df794bc5331d9794761b18e14a4a0ff6c2be10a959ac988ce1c17581f06",
    "uuid|it": "5a1f2df794bc5331d9794761b18e14a4a0ff6c2be10a959ac988ce1c17581f06",
    "uuid|pt-BR": "5a1f2df794bc5331d9794761b18e14a4a0ff6c2be10a959ac988ce1c17581f06",
}


def _locale(text: str) -> str | list[str] | None:
    if text == "None":
        return None
    return text.split("+") if "+" in text else text


def _spec(locale: str | list[str] | None, seed: bytes | None = b"\x07" * 8) -> ProviderSpec:
    return ProviderSpec(locale=locale, deterministic=False, namespace=None, seed=seed)


@pytest.mark.parametrize("key", sorted(GOLDEN))
def test_working_configs_produce_unchanged_output(key: str) -> None:
    provider, locale = key.split("|")
    registry = get_default_registry()
    values = registry.get_adapter(provider).generate_batch(
        provider, spec=_spec(_locale(locale)), count=25
    )
    assert hashlib.sha256(repr(list(values)).encode()).hexdigest() == GOLDEN[key]


@pytest.mark.parametrize(
    ("provider", "locale"), [("person_first_name", "xx_YY"), ("address_state", "fr_FR")]
)
@pytest.mark.parametrize("seed", [b"\x07" * 8, None])
def test_locale_faults_are_coded(provider: str, locale: str, seed: bytes | None) -> None:
    adapter = get_default_registry().get_adapter(provider)
    for call in (
        lambda: adapter.generate(provider, spec=_spec(locale, None)),
        lambda: adapter.generate_batch(provider, spec=_spec(locale, seed), count=2),
    ):
        with pytest.raises(AdapterError) as excinfo:
            call()
        assert excinfo.value.code == reasons.provider.UNSUPPORTED_LOCALE
        assert provider in str(excinfo.value) and locale in str(excinfo.value)
        assert excinfo.value.__cause__ is None


def test_faker_invalid_locale_message_prefix_is_pinned() -> None:
    import faker

    from decoy_engine.providers_v2._faker_adapter import _FAKER_INVALID_LOCALE

    with pytest.raises(AttributeError) as excinfo:
        faker.Faker("xx_YY")
    assert str(excinfo.value).startswith(_FAKER_INVALID_LOCALE)


def test_bare_adapter_without_a_lookup_behaves_the_same() -> None:
    adapter = FakerAdapter()
    assert adapter.generate_batch("person_first_name", spec=_spec("pt-BR"), count=2)
    with pytest.raises(AdapterError) as excinfo:
        adapter.generate_batch("address_state", spec=_spec("fr_FR"), count=1)
    assert excinfo.value.code == reasons.provider.UNSUPPORTED_LOCALE


def test_custom_provider_under_a_catalogue_name_is_not_method_checked() -> None:
    register_faker_provider_v2("address_state", lambda fake: "custom-state")
    try:
        values = FakerAdapter().generate_batch("address_state", spec=_spec("fr_FR"), count=2)
    finally:
        _unregister_faker_provider_v2("address_state")
    assert list(values) == ["custom-state", "custom-state"]


def test_other_method_failures_stay_capability_violations() -> None:
    spec = ProviderSpec(
        locale="en_US", deterministic=False, namespace=None, seed=None, extra={"no_such_kw": 1}
    )
    with pytest.raises(AdapterError) as excinfo:
        FakerAdapter().generate("person_first_name", spec=spec)
    assert excinfo.value.code == reasons.provider.CAPABILITY_VIOLATION


def test_rows_advertise_observed_locales() -> None:
    registry = get_default_registry()
    state = registry.get_capabilities("address_state").supported_locales
    phone = registry.get_capabilities("person_phone").supported_locales
    first = registry.get_capabilities("person_first_name").supported_locales
    assert "fr_FR" not in state and "de_DE" in state
    assert "en_PH" not in phone and len(phone) == len(first) - 3
    assert registry.get_capabilities("composite_person").supported_locales == ("en_US",)


def _config(provider: str, locale: str, path: str) -> dict[str, object]:
    return PipelineConfig.model_validate(
        {
            "version": 2,
            "global_settings": {"seed": 42},
            "sources": {"t": {"type": "file", "format": "parquet", "path": path}},
            "tables": [
                {
                    "name": "t",
                    "columns": [
                        {
                            "name": "c",
                            "strategy": "faker",
                            "provider": provider,
                            "deterministic": True,
                            "namespace": "ns_c",
                            "pool_size": 50,
                            "provider_config": {"locale": locale},
                        }
                    ],
                }
            ],
            "targets": {"t": {"type": "file", "format": "parquet", "path": "/dev/null"}},
        }
    ).model_dump()


def test_config_with_an_unsupported_locale_fails_coded(tmp_path: Path) -> None:
    source = pa.table({"c": [f"v{i}" for i in range(20)]})
    path = tmp_path / "t.parquet"
    pq.write_table(source, path)
    ok = run_pipeline(
        _config("address_state", "de_DE", str(path)), sources={"t": source}, engine_version="t"
    )
    assert ok.outputs["t"].num_rows == 20
    with pytest.raises(Exception) as excinfo:
        run_pipeline(
            _config("address_state", "fr_FR", str(path)), sources={"t": source}, engine_version="t"
        )
    assert (
        reasons.provider.UNSUPPORTED_LOCALE in repr(excinfo.value)
        or getattr(excinfo.value, "code", None) == reasons.provider.UNSUPPORTED_LOCALE
    )
