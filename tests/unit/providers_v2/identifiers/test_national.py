"""National identifier providers: validity, determinism, registry and config path.

Every value must pass ``checksums.validate`` for its scheme and the spec's
``format_regex``; the end-to-end tests run a real ``strategy: faker`` config,
which is the only way a config reaches a provider (through the pool builder).
"""

from __future__ import annotations

import random
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import decoy_engine
from decoy_engine import checksums, reasons
from decoy_engine.config import PipelineConfig
from decoy_engine.execution import run_pipeline
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.providers_v2._adapter import ProviderSpec
from decoy_engine.providers_v2._errors import ProviderError
from decoy_engine.providers_v2.identifiers import (
    NATIONAL_ID_SPECS,
    NationalIdAdapter,
    NationalIdDomain,
    NationalIdValidator,
    _national_specs,
)
from decoy_engine.providers_v2.identifiers._errors import IdentifierError

SPECS = {spec.provider: spec for spec in NATIONAL_ID_SPECS}
SEED = b"\x01" * 8


def _spec(
    *,
    locale: str | None = None,
    deterministic: bool = False,
    namespace: str | None = None,
    seed: bytes | None = None,
) -> ProviderSpec:
    return ProviderSpec(locale=locale, deterministic=deterministic, namespace=namespace, seed=seed)


def _det(spec_name: str, source: str, namespace: str = "ns") -> str:
    spec = SPECS[spec_name]
    return NationalIdAdapter(spec).generate(
        spec_name,
        spec=_spec(deterministic=True, seed=SEED, namespace=namespace),
        source_value=source,
    )


def _batch(spec_name: str, count: int, seed: bytes | None = SEED) -> list[str]:
    return list(
        NationalIdAdapter(SPECS[spec_name]).generate_batch(
            spec_name, spec=_spec(seed=seed), count=count
        )
    )


def _assert_valid(spec_name: str, values: list[str]) -> None:
    spec = SPECS[spec_name]
    for value in values:
        assert checksums.validate(spec.scheme, value), value
        assert re.fullmatch(spec.format_regex, value), value


@pytest.mark.parametrize("name", sorted(SPECS))
def test_deterministic_and_batch_values_are_valid(name: str) -> None:
    _assert_valid(name, [_det(name, f"source-{i}") for i in range(2000)])
    _assert_valid(name, _batch(name, 2000))
    validator = NationalIdValidator(SPECS[name])
    assert validator.is_valid(_det(name, "x"))


@pytest.mark.parametrize("name", sorted(SPECS))
def test_determinism_and_namespace_divergence(name: str) -> None:
    assert _det(name, "alice") == _det(name, "alice")
    assert _batch(name, 50) == _batch(name, 50)
    sources = [f"s{i}" for i in range(200)]
    changed = sum(_det(name, s, "ns_a") != _det(name, s, "ns_b") for s in sources)
    assert changed >= 190


def test_seeded_batch_is_identical_in_a_new_process() -> None:
    code = (
        "from decoy_engine.providers_v2.identifiers import NATIONAL_ID_SPECS, NationalIdAdapter\n"
        "from decoy_engine.providers_v2._adapter import ProviderSpec\n"
        "for s in NATIONAL_ID_SPECS:\n"
        "    print(','.join(NationalIdAdapter(s).generate_batch(s.provider, "
        "spec=ProviderSpec(locale=None, deterministic=False, namespace=None, seed=b'\\x01' * 8), count=5)))\n"
    )
    out = subprocess.run(  # noqa: S603 -- args are test literals
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        cwd=Path(decoy_engine.__file__).parents[2],
    ).stdout.split()
    assert out == [",".join(_batch(s.provider, 5)) for s in NATIONAL_ID_SPECS]


def _count_repairs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    hits: list[str] = []
    real = _national_specs._with_check

    def spy(scheme: str, body: str) -> str:
        try:
            return real(scheme, body)
        except ValueError:
            hits.append(scheme)
            raise

    monkeypatch.setattr(_national_specs, "_with_check", spy)
    return hits


def test_nhs_and_tfn_repair_runs_and_still_validates(monkeypatch: pytest.MonkeyPatch) -> None:
    hits = _count_repairs(monkeypatch)
    for name in ("synthetic_gb_nhs", "synthetic_au_tfn"):
        _assert_valid(name, _batch(name, 3000))
    assert hits.count("gb_nhs") > 100
    assert hits.count("au_tfn") > 100


def test_aadhaar_palindrome_fix_produces_a_valid_value() -> None:
    # Bytes giving body 2000000000 + "0"... force the palindrome branch directly.
    spec = SPECS["synthetic_in_aadhaar"]
    for first in range(8):
        for digit in range(10):
            body = "23456789"[first] + str(digit) * 10
            value = checksums.calc_check_digit("in_aadhaar", body)
            if (body + value) == (body + value)[::-1]:
                raw = bytes([first]) + (int(str(digit) * 10)).to_bytes(8, "big") + bytes(23)
                result = spec.build(raw)
                assert result != result[::-1]
                _assert_valid("synthetic_in_aadhaar", [result])


def test_idnr_leading_zero_is_rotated_away() -> None:
    rng = random.Random("idnr-rotation")
    inputs = [rng.randbytes(32) for _ in range(3000)]
    zero_led = [raw for raw in inputs if _national_specs._idnr_digits(raw)[0] == "0"]
    assert len(zero_led) > 100
    values = [SPECS["synthetic_de_idnr"].build(raw) for raw in zero_led]
    assert all(v[0] != "0" for v in values)
    _assert_valid("synthetic_de_idnr", values)


def test_different_seeds_give_different_batches() -> None:
    for name in SPECS:
        assert _batch(name, 20, b"\x01" * 8) != _batch(name, 20, b"\x02" * 8)
        assert _batch(name, 20, b"\x01" * 32) != _batch(name, 20, b"\x01" * 8)


def test_locale_rules() -> None:
    adapter = NationalIdAdapter(SPECS["synthetic_gb_nhs"])
    assert adapter.generate("synthetic_gb_nhs", spec=_spec())
    assert adapter.generate("synthetic_gb_nhs", spec=_spec(locale="en_GB"))
    with pytest.raises(ProviderError) as excinfo:
        adapter.generate("synthetic_gb_nhs", spec=_spec(locale="en_US"))
    assert excinfo.value.code == reasons.provider.UNSUPPORTED_LOCALE
    assert "en_US" in str(excinfo.value)


def test_errors_use_existing_codes() -> None:
    adapter = NationalIdAdapter(SPECS["synthetic_es_dni"])
    with pytest.raises(ProviderError) as excinfo:
        adapter.generate_batch(
            "synthetic_es_dni", spec=_spec(deterministic=True, namespace="n", seed=SEED), count=1
        )
    assert excinfo.value.code == reasons.provider.BATCH_DETERMINISTIC_UNSUPPORTED
    with pytest.raises(ProviderError) as excinfo:
        adapter.generate("synthetic_ssn", spec=_spec())
    assert excinfo.value.code == reasons.provider.UNKNOWN_PROVIDER
    with pytest.raises(IdentifierError) as id_exc:
        NationalIdDomain(SPECS["synthetic_es_dni"]).from_bytes(b"short")
    assert id_exc.value.code == reasons.provider.INVALID_INPUT_LENGTH


def test_registry_binds_every_spec_once_with_the_pinned_row() -> None:
    registry = get_default_registry()
    national = {
        name
        for name in registry.known_providers()
        if isinstance(registry.get_adapter(name), NationalIdAdapter)
    }
    assert national == set(SPECS)
    for name, spec in SPECS.items():
        caps = registry.get_capabilities(name)
        assert caps.poolable and caps.supports_deterministic
        assert not caps.supports_uniqueness and not caps.participates_in_fk_pk
        assert caps.backend_type == "decoy_native"
        assert caps.format_regex == spec.format_regex
        assert caps.supported_locales == spec.locales
        assert caps.blocklist_validators == (f"checksums:{spec.scheme}",)


def _config(provider: str, path: str) -> dict[str, Any]:
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
                            "name": "national_id",
                            "strategy": "faker",
                            "provider": provider,
                            "deterministic": True,
                            "namespace": "ns_national_id",
                            "pool_size": 500,
                        }
                    ],
                }
            ],
            "targets": {"t": {"type": "file", "format": "parquet", "path": "/dev/null"}},
        }
    ).model_dump()


@pytest.mark.parametrize("name", sorted(SPECS))
def test_strategy_faker_config_reaches_the_provider(name: str, tmp_path: Path) -> None:
    source = pa.table({"national_id": [f"real-{i % 37}" for i in range(120)]})
    path = tmp_path / "t.parquet"
    pq.write_table(source, path)
    config = _config(name, str(path))
    first = run_pipeline(config, sources={"t": source}, engine_version="v1p-test")
    again = run_pipeline(config, sources={"t": source}, engine_version="v1p-test")
    values = first.outputs["t"].column("national_id").to_pylist()
    _assert_valid(name, values)
    assert values == again.outputs["t"].column("national_id").to_pylist()
    # Equal sources mask to equal values.
    by_source: dict[str, set[str]] = {}
    for src, out in zip(source.column("national_id").to_pylist(), values, strict=True):
        by_source.setdefault(src, set()).add(out)
    assert all(len(outs) == 1 for outs in by_source.values())


def test_no_default_config_or_disguise_names_the_providers() -> None:
    root = Path(decoy_engine.__file__).parent
    files = [*root.glob("disguises/*.yaml"), *root.rglob("templates/**/*.yaml")]
    assert files
    for path in files:
        text = path.read_text(encoding="utf-8")
        for name in SPECS:
            assert name not in text, f"{name} named in {path.name}"
