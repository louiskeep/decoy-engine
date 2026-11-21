"""S7a sentry: every `"<name>/vN"` tag literal in engine source has one class (AT-S7a).

Classes: a frozen format in contract 3.1, a frozen derivation label in contract 3.3 (and
`DERIVATION_LABELS`), or an internal tag with a reason (`INTERNAL_TAGS`). A new literal fails
until it is classified. The artifact scan for internal tags is in `test_tag_artifacts.py`.
"""

from __future__ import annotations

from decoy_engine.contracts import stability as st
from tests.sentry.release import _tags


def test_every_tag_literal_is_classified() -> None:
    found = _tags.source_tags()
    # Not vacuous: the walk sees the tags the contract names and the ones S7a classified.
    assert len(found) >= 60, len(found)
    assert {"decoy-vault/v3", "tokenize-key/v1", "phase-event/v1", "decoy-telemetry/v1"} <= set(
        found
    )
    frozen, labels, internal, declared = _tags.classes()
    assert _tags.unclassified(set(found), frozen, labels, internal, declared) == set()


def test_the_census_reads_every_quoting_style_and_skips_comments() -> None:
    text = (
        'A = "x.tag/v1"\nB = b"snake_case/v12"\nC = "not a tag/v1"\nD = f"{x}/v1"\n'
        "E = 'single/v2'\nF = b'joint_mask/gen_row/v1'\nG = (\"multi\"\n    \"/v1\")\n"
        '# H = "commented/v1"\n"""docstring"""\nI = "a/b/c/v3"\n'
    )
    assert _tags.literals(text) == {
        "x.tag/v1",
        "snake_case/v12",
        "single/v2",
        "multi/v1",  # adjacent literals are one constant to the parser
        "joint_mask/gen_row/v1",
        "a/b/c/v3",
    }
    assert _tags.unclassified({"x.tag/v1", "ok/v1"}, {"ok/v1"}, set(), set(), set()) == {"x.tag/v1"}


def test_a_tag_has_one_class() -> None:
    frozen, labels, internal, declared = _tags.classes()
    assert _tags.overlaps(frozen, labels, internal, declared) == set()
    assert _tags.overlaps({"a/v1"}, {"a/v1"}, {"b/v1"}, set()) == {"a/v1"}
    assert _tags.overlaps(set(), set(), {"c/v1"}, {"c/v1"}) == {"c/v1"}


def test_the_declared_format_ids_are_a_live_registry() -> None:
    declared = _tags.declared_format_ids()
    assert len(declared) >= 14
    assert declared <= set(_tags.source_tags())


def test_every_label_and_internal_row_names_a_live_literal() -> None:
    found = _tags.source_tags()
    for table in (
        {tag: owner for tag, owner in st.DERIVATION_LABELS.items()},
        {tag: owner for tag, (owner, _) in st.INTERNAL_TAGS.items()},
    ):
        for tag, owner in table.items():
            assert owner in found.get(tag, []), f"{tag}: not a literal in {owner}"


def test_every_internal_tag_has_a_reason() -> None:
    assert st.INTERNAL_TAGS
    for tag, (_, reason) in st.INTERNAL_TAGS.items():
        assert len(reason.split()) >= 5, tag


def test_contract_3_3_lists_the_derivation_labels() -> None:
    contract = _tags.CONTRACT.read_text(encoding="utf-8")
    assert _tags.contract_3_3_labels(contract) == set(st.DERIVATION_LABELS)


def test_contract_3_1_lists_no_internal_tag_and_no_label() -> None:
    contract = _tags.CONTRACT.read_text(encoding="utf-8")
    in_3_1 = _tags.contract_3_1_tags(contract)
    assert not in_3_1 & set(st.INTERNAL_TAGS)
    assert not in_3_1 & set(st.DERIVATION_LABELS)
    assert not in_3_1 & _tags.declared_format_ids()


def test_contract_3_1_reads_the_code_block_and_three_named_prose_tags() -> None:
    contract = _tags.CONTRACT.read_text(encoding="utf-8")
    block = set(_tags.WHOLE.findall(_tags.contract_3_1_block(contract)))
    assert _tags.contract_3_1_tags(contract) == block | _tags.PROSE_ONLY_3_1
    assert not block & _tags.PROSE_ONLY_3_1, "an allowlisted tag moved into the block: drop it"
    # Other tags in the prose do not count: this made-up section lists one tag in the block only.
    made_up = f"{_tags.H31}\n```\nin-block/v1\n```\nprose-only/v9 and distribution-snapshot/v2\n"
    assert _tags.contract_3_1_tags(made_up) == {"in-block/v1", "distribution-snapshot/v2"}


def test_the_retired_fpe_key_label_is_not_a_live_label() -> None:
    assert "fpe-key/v1" not in st.DERIVATION_LABELS
    contract = _tags.CONTRACT.read_text(encoding="utf-8")
    assert "fpe-key/v1" not in _tags.contract_3_3_labels(contract)
