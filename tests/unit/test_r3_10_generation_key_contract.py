"""Generation key contract.

- The strategy fingerprint ignores the display name and null probability.
- A column's draw derives from the key (or seed), the fingerprint and its
  (table, column) identity: the same inputs reproduce the same bytes.
- Renaming a column, or giving two columns the same settings, shifts the
  stream, so identical settings never repeat across columns.
"""

from __future__ import annotations

from decoy_engine.context import make_key_resolver
from decoy_engine.generation import _faker_pool
from decoy_engine.generation.synthesize import _faker
from decoy_engine.generators.columns import ColumnGenerator
from decoy_engine.generators.derivation import strategy_config_fingerprint, with_table

_MASTER = bytes(range(32))


def _resolver(label: str = "demo-pipeline"):
    return make_key_resolver(_MASTER, label)


def _gen_v2_pooled(config: dict, *, derive_key=None, num_rows: int = _faker_pool.N_THRESHOLD):
    """Drive the v2 `_faker` pool bridge directly (V1 `ColumnGenerator` never
    pools -- R3.10's `_gen` helper above exercises V1 only). `num_rows`
    defaults to the pool threshold so every caller here actually pools."""
    return _faker(with_table(config, "t"), num_rows, 42, derive_key, None)


def _gen(config, *, derive_key=None, num_rows: int = 10, table: str = "t"):
    gen = ColumnGenerator(seed=42, derive_key=derive_key)
    return list(
        gen.generate_column(
            num_rows=num_rows,
            column_config=config,
            table_name=table,
            reference_data={},
        )
    )


# ── Fingerprint properties ───────────────────────────────────────────────────


def test_fingerprint_ignores_column_name():
    """The fingerprint is the seed-info label; renames must not shift it."""
    a = strategy_config_fingerprint(
        {"name": "first_name", "type": "faker", "faker_type": "first_name"}
    )
    b = strategy_config_fingerprint(
        {"name": "parent_first_name", "type": "faker", "faker_type": "first_name"}
    )
    assert a == b


def test_fingerprint_ignores_null_probability():
    """Null is a post-filter, not part of the strategy contract."""
    a = strategy_config_fingerprint({"name": "x", "type": "faker", "faker_type": "first_name"})
    b = strategy_config_fingerprint(
        {
            "name": "x",
            "type": "faker",
            "faker_type": "first_name",
            "null_probability": 0.1,
        }
    )
    assert a == b


def test_fingerprint_changes_when_strategy_config_changes():
    """Anything that actually affects output must change the fingerprint."""
    a = strategy_config_fingerprint({"type": "faker", "faker_type": "first_name"})
    b = strategy_config_fingerprint({"type": "faker", "faker_type": "last_name"})
    assert a != b


def test_fingerprint_is_key_order_stable():
    """Canonical JSON sorts keys; dict-iteration order must not leak."""
    a = strategy_config_fingerprint({"type": "faker", "faker_type": "first_name"})
    b = strategy_config_fingerprint({"faker_type": "first_name", "type": "faker"})
    assert a == b


# ── Column identity ──────────────────────────────────────────────────────────


def test_rename_changes_keyed_output():
    resolver = _resolver()
    base = {"name": "first_name", "type": "faker", "faker_type": "first_name"}
    renamed = {**base, "name": "given_name"}
    assert _gen(base, derive_key=resolver) != _gen(renamed, derive_key=resolver)


def test_rename_changes_unkeyed_output():
    base = {"name": "first_name", "type": "faker", "faker_type": "first_name"}
    renamed = {**base, "name": "given_name"}
    assert _gen(base) != _gen(renamed)


def test_rename_for_categorical_changes_output():
    base = {
        "name": "status",
        "type": "categorical",
        "categories": ["new", "shipped", "delivered"],
    }
    renamed = {**base, "name": "order_status"}
    resolver = _resolver()
    assert _gen(base, derive_key=resolver) != _gen(renamed, derive_key=resolver)


def test_same_name_other_table_changes_output():
    config = {"name": "x", "type": "faker", "faker_type": "first_name"}
    assert _gen(config, table="t1") != _gen(config, table="t2")


def test_two_columns_same_config_produce_different_output():
    """Identical settings under different names must not repeat each other."""
    resolver = _resolver()
    a = {"name": "first_name", "type": "faker", "faker_type": "first_name"}
    b = {"name": "parent_first_name", "type": "faker", "faker_type": "first_name"}
    assert _gen(a, derive_key=resolver) != _gen(b, derive_key=resolver)


def test_different_faker_type_produces_different_output():
    """Sanity: when the strategy/config actually differs, output differs."""
    resolver = _resolver()
    first = {"name": "x", "type": "faker", "faker_type": "first_name"}
    last = {"name": "x", "type": "faker", "faker_type": "last_name"}
    assert _gen(first, derive_key=resolver) != _gen(last, derive_key=resolver)


# ── Key scope properties ─────────────────────────────────────────────────────


def test_same_master_same_label_reproduces():
    """Pipeline Key mode: same master + same label must reproduce."""
    config = {"name": "x", "type": "faker", "faker_type": "first_name"}
    assert _gen(config, derive_key=_resolver("p1")) == _gen(config, derive_key=_resolver("p1"))


def test_different_pipeline_label_shifts_output():
    """Changing the pipeline key changes generated output."""
    config = {"name": "x", "type": "faker", "faker_type": "first_name"}
    assert _gen(config, derive_key=_resolver("p1")) != _gen(config, derive_key=_resolver("p2"))


# The synthetic_column_seed contract (int32 range, fresh path, M18 raise)
# moved to GenDeriveContext at the v6 F2/F3 rewrite; see
# tests/unit/generators/test_gen_derive_context.py.


# ── GP2: pooled faker follows the same identity contract ────────────────────
#
# Both the pool BUILD and SELECTION seeds derive from the same per-column
# GenDeriveContext root the per-row path uses, and `pooled` is excluded from
# the fingerprint, so identity behaves for pooled output exactly as it does
# for per-row output.


def test_pooled_rename_changes_output():
    resolver = _resolver()
    base = {"name": "hometown", "type": "faker", "faker_type": "city"}
    renamed = {**base, "name": "birth_city"}
    assert _gen_v2_pooled(base, derive_key=resolver) != _gen_v2_pooled(renamed, derive_key=resolver)


def test_pooled_rename_changes_unkeyed_output():
    base = {"name": "hometown", "type": "faker", "faker_type": "city"}
    renamed = {**base, "name": "birth_city"}
    assert _gen_v2_pooled(base) != _gen_v2_pooled(renamed)


def test_pooled_two_columns_same_config_produce_different_output():
    resolver = _resolver()
    a = {"name": "hometown", "type": "faker", "faker_type": "city"}
    b = {"name": "birth_city", "type": "faker", "faker_type": "city"}
    assert _gen_v2_pooled(a, derive_key=resolver) != _gen_v2_pooled(b, derive_key=resolver)


def test_pooled_different_faker_type_produces_different_output():
    resolver = _resolver()
    city = {"name": "x", "type": "faker", "faker_type": "city"}
    company = {"name": "x", "type": "faker", "faker_type": "company"}
    assert _gen_v2_pooled(city, derive_key=resolver) != _gen_v2_pooled(company, derive_key=resolver)


def test_pooled_flag_itself_does_not_shift_pooled_output():
    """`pooled` is fingerprint-excluded: an explicit `pooled: true` on an
    already-eligible column must not shift the bytes a bare eligible column
    produces (both pool; the flag is a no-op documentation knob for the
    already-auto-pooled case)."""
    resolver = _resolver()
    bare = {"name": "x", "type": "faker", "faker_type": "city"}
    explicit = {**bare, "pooled": True}
    assert _gen_v2_pooled(bare, derive_key=resolver) == _gen_v2_pooled(
        explicit, derive_key=resolver
    )


def test_pooled_opt_out_reproduces_pre_gp2_bytes_and_root():
    """GP2.A4 (the ratchet promise): `pooled: false` on an OTHERWISE-eligible
    column (allowlisted type, at the pool threshold) keeps the EXACT pre-GP2
    per-row bytes AND seed root -- checked against the V1 oracle (`_gen`,
    real `ColumnGenerator`), which never pools at all."""
    resolver = _resolver()
    n = _faker_pool.N_THRESHOLD
    col = {"name": "x", "type": "faker", "faker_type": "city"}
    opted_out = {**col, "pooled": False}

    assert strategy_config_fingerprint(col) == strategy_config_fingerprint(opted_out)
    assert not _faker_pool.pool_eligible("city", None, n, opted_out=True)

    v1 = _gen(col, derive_key=resolver, num_rows=n)
    assert _faker(with_table(opted_out, "t"), n, 42, resolver, None) == v1


def test_pooled_field_via_validated_config_does_not_shift_fingerprint_or_root():
    """GP2.A4 regression, the exact case Codex round-2 spec A flagged: the new
    typed `pooled` field on `GenerateColumnConfig` means
    `PipelineConfig(...).model_dump()` now stamps EVERY generate column with
    `"pooled": None`, even one whose YAML never mentioned it -- a key pre-GP2
    configs never carried. If `pooled` weren't fingerprint-excluded, that
    stamp would shift the seed ROOT (and therefore every byte) for every
    existing faker column the day GP2 ships, pool-eligible or not. Checked
    for a non-allowlisted type (never pools) and an eligible-but-opted-out
    one (would pool if not for `pooled: false`)."""
    from decoy_engine.config._pipeline import PipelineConfig

    def _dumped_column(col: dict) -> dict:
        cfg = {
            "version": 2,
            "global_settings": {"seed": 42},
            "sources": {},
            "tables": [{"name": "t", "row_count": 10, "generate_columns": [col]}],
            "targets": {"t": {"type": "file", "format": "csv", "path": "o.csv"}},
        }
        dumped = PipelineConfig.model_validate(cfg).model_dump()
        return dumped["tables"][0]["generate_columns"][0]

    for pre_gp2_shape in (
        {"name": "x", "type": "faker", "faker_type": "pyint"},
        {"name": "x", "type": "faker", "faker_type": "city", "pooled": False},
    ):
        dumped = _dumped_column(pre_gp2_shape)
        assert dumped["pooled"] is pre_gp2_shape.get("pooled")  # model_dump() always stamps the key
        assert strategy_config_fingerprint(dumped) == strategy_config_fingerprint(pre_gp2_shape)
        n = _faker_pool.N_THRESHOLD
        assert _faker(with_table(dumped, "t"), n, 42, None, None) == _faker(
            with_table(pre_gp2_shape, "t"), n, 42, None, None
        )
