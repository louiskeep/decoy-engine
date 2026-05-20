"""Sprint A · Item 18 — locale + custom-provider coverage for the Faker
strategy and the ColumnGenerator. Built to catch the two regressions the
roadmap entry calls out: a UK / EU / JP locale silently rendering en_US
output, or a custom provider failing to seed deterministically."""

import json

import pandas as pd
import pytest

from decoy_engine.transforms.faker_based import FakerStrategy
from decoy_engine.generators.columns import ColumnGenerator
from decoy_engine import (
    load_custom_providers,
    register_faker_list_provider,
    register_faker_provider,
    unregister_faker_provider,
)


@pytest.fixture
def sample_emails():
    return pd.Series(['ana@example.com', 'kwame@example.com', 'jay@example.com'])


def test_locale_de_de_legacy_produces_german_names(sample_emails, mock_logger):
    """Without `derive_key` the strategy takes the legacy path. With
    `locale: 'de_DE'` the names should come out of the German pool, not the
    en_US one. We can't assert exact values (Faker output varies by version)
    but we can assert the de_DE output differs from the default en_US path
    given the same seed — locale is the only changed input."""
    s = FakerStrategy(seed=42, logger=mock_logger)
    en = s.apply(sample_emails, {'column': 'name', 'type': 'faker', 'faker_type': 'first_name'})
    de = s.apply(
        sample_emails,
        {'column': 'name', 'type': 'faker', 'faker_type': 'first_name', 'locale': 'de_DE'},
    )
    assert not en.equals(de), "de_DE locale produced identical output to en_US — locale ignored"


def test_locale_unknown_falls_back(sample_emails, mock_logger):
    """An unknown locale should NOT raise. Engine logs a warning and
    falls back to the default Faker — pipeline runs to completion."""
    s = FakerStrategy(seed=42, logger=mock_logger)
    fallback = s.apply(
        sample_emails,
        {'column': 'name', 'type': 'faker', 'faker_type': 'first_name', 'locale': 'xx_YY'},
    )
    default = s.apply(
        sample_emails,
        {'column': 'name', 'type': 'faker', 'faker_type': 'first_name'},
    )
    # Same seed + same fallback locale (en_US) should produce same series.
    pd.testing.assert_series_equal(fallback, default)


def test_custom_provider_registered_and_callable(sample_emails, mock_logger):
    """Registering a custom provider lets `faker_type: <name>` resolve to
    the user's function. The function receives the seeded Faker instance,
    so calling fake.user_name() inside a custom provider stays
    deterministic across runs."""
    register_faker_provider('mrn', lambda fake: f"MRN-{fake.random_int(10000, 99999)}")
    try:
        s = FakerStrategy(seed=42, logger=mock_logger)
        out = s.apply(
            sample_emails,
            {'column': 'mrn', 'type': 'faker', 'faker_type': 'mrn'},
        )
        assert len(out) == len(sample_emails)
        assert all(isinstance(v, str) and v.startswith('MRN-') for v in out)
        # Same input + same seed → same output every run.
        out2 = s.apply(
            sample_emails,
            {'column': 'mrn', 'type': 'faker', 'faker_type': 'mrn'},
        )
        pd.testing.assert_series_equal(out, out2)
    finally:
        unregister_faker_provider('mrn')


def test_custom_provider_overrides_builtin(sample_emails, mock_logger):
    """Registering a name that matches a built-in (`first_name`) replaces
    the built-in. Lets users override defaults if they need to."""
    register_faker_provider('first_name', lambda fake: 'OVERRIDE')
    try:
        s = FakerStrategy(seed=42, logger=mock_logger)
        out = s.apply(
            sample_emails,
            {'column': 'first_name', 'type': 'faker', 'faker_type': 'first_name'},
        )
        assert all(v == 'OVERRIDE' for v in out)
    finally:
        unregister_faker_provider('first_name')


def test_register_validates_input():
    """Bad arguments raise instead of silently no-op-ing."""
    with pytest.raises(ValueError):
        register_faker_provider('', lambda fake: 'x')
    with pytest.raises(TypeError):
        register_faker_provider('x', 'not callable')


def test_register_faker_list_provider_is_deterministic(sample_emails, mock_logger):
    register_faker_list_provider('custom.departments', ['Cardiology', 'Oncology'])
    try:
        s = FakerStrategy(seed=42, logger=mock_logger)
        out = s.apply(
            sample_emails,
            {'column': 'department', 'type': 'faker', 'faker_type': 'custom.departments'},
        )
        out2 = s.apply(
            sample_emails,
            {'column': 'department', 'type': 'faker', 'faker_type': 'custom.departments'},
        )
        assert set(out).issubset({'Cardiology', 'Oncology'})
        pd.testing.assert_series_equal(out, out2)
    finally:
        unregister_faker_provider('custom.departments')


def test_register_faker_list_provider_validates_values():
    with pytest.raises(ValueError):
        register_faker_list_provider('custom.empty', [])
    with pytest.raises(TypeError):
        register_faker_list_provider('custom.bad', 'not-a-list')


def test_load_custom_providers_reads_txt_and_json(tmp_path):
    (tmp_path / 'departments.txt').write_text(
        'Cardiology\n\n# comment\nOncology\n',
        encoding='utf-8',
    )
    (tmp_path / 'regions.json').write_text(
        json.dumps(['North', 'South']),
        encoding='utf-8',
    )
    (tmp_path / 'ignored.csv').write_text('x,y\n', encoding='utf-8')

    loaded = load_custom_providers(tmp_path)
    try:
        assert loaded == {
            'custom.departments': ['Cardiology', 'Oncology'],
            'custom.regions': ['North', 'South'],
        }
    finally:
        unregister_faker_provider('custom.departments')
        unregister_faker_provider('custom.regions')


def test_load_custom_providers_skips_empty_and_malformed_files(tmp_path):
    (tmp_path / 'empty.txt').write_text('\n# only comments\n', encoding='utf-8')
    (tmp_path / 'bad.json').write_text('{"not": "a list"}', encoding='utf-8')

    assert load_custom_providers(tmp_path) == {}


def test_column_generator_locale(mock_logger):
    """ColumnGenerator's faker path also honors `locale`. Same seed, two
    locales — outputs should differ."""
    g = ColumnGenerator(seed=42, logger=mock_logger)
    en = g._generate_faker_column(
        num_rows=5,
        column_config={'name': 'first_name', 'faker_type': 'first_name'},
        table_name='customers',
        reference_data={},
    )
    g2 = ColumnGenerator(seed=42, logger=mock_logger)
    de = g2._generate_faker_column(
        num_rows=5,
        column_config={'name': 'first_name', 'faker_type': 'first_name', 'locale': 'de_DE'},
        table_name='customers',
        reference_data={},
    )
    assert not en.equals(de), "de_DE column-gen produced identical output to en_US"


def test_column_generator_instance_default_locale(mock_logger):
    """Tier-4 audit (2026-05-20): ColumnGenerator built with
    instance_default_locale uses that locale for the no-column-override
    path. Same seed, default en_US vs default de_DE should produce
    different output even when no column sets `locale`."""
    g_en = ColumnGenerator(seed=42, logger=mock_logger, instance_default_locale='en_US')
    en_out = g_en._generate_faker_column(
        num_rows=5,
        column_config={'name': 'first_name', 'faker_type': 'first_name'},
        table_name='customers',
        reference_data={},
    )
    g_de = ColumnGenerator(seed=42, logger=mock_logger, instance_default_locale='de_DE')
    de_out = g_de._generate_faker_column(
        num_rows=5,
        column_config={'name': 'first_name', 'faker_type': 'first_name'},
        table_name='customers',
        reference_data={},
    )
    assert not en_out.equals(de_out), (
        "instance_default_locale=de_DE produced identical output to en_US"
    )


def test_column_level_locale_overrides_instance_default(mock_logger):
    """Per-column `locale` still wins over the instance default — the
    setting is a fallback, not an override."""
    # Instance default = de_DE, but column says fr_FR.
    g = ColumnGenerator(seed=42, logger=mock_logger, instance_default_locale='de_DE')
    fr_out = g._generate_faker_column(
        num_rows=5,
        column_config={'name': 'first_name', 'faker_type': 'first_name', 'locale': 'fr_FR'},
        table_name='customers',
        reference_data={},
    )
    # And same column rendered with explicit fr_FR (no instance default).
    g2 = ColumnGenerator(seed=42, logger=mock_logger)
    fr_explicit = g2._generate_faker_column(
        num_rows=5,
        column_config={'name': 'first_name', 'faker_type': 'first_name', 'locale': 'fr_FR'},
        table_name='customers',
        reference_data={},
    )
    # Both should produce the same fr_FR output — column locale wins.
    assert fr_out.equals(fr_explicit), (
        "column-level locale failed to override instance_default_locale"
    )


def test_column_generator_custom_provider(mock_logger):
    """Custom providers visible from the generator path too."""
    register_faker_provider(
        'employee_id',
        lambda fake: f"EMP-{fake.random_int(10000, 99999)}",
    )
    try:
        g = ColumnGenerator(seed=42, logger=mock_logger)
        out = g._generate_faker_column(
            num_rows=5,
            column_config={'name': 'eid', 'faker_type': 'employee_id'},
            table_name='employees',
            reference_data={},
        )
        assert len(out) == 5
        assert all(isinstance(v, str) and v.startswith('EMP-') for v in out)
    finally:
        unregister_faker_provider('employee_id')
