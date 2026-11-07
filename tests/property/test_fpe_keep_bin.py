"""Property test for the fpe `keep_bin` option (card keep-BIN mask).

For generated Luhn-valid card numbers (13 to 19 digits with 6 kept, 15 to 19 with 8, plain or
in groups of four with spaces or dashes), every masked value keeps the length, the separator
positions and the first `keep_bin` digits, is Luhn-valid, and decrypts to the source.

Run:  pytest tests/property/test_fpe_keep_bin.py -q
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from decoy_engine import checksums
from decoy_engine.transforms._ff1 import FF1_KEY_BYTES
from decoy_engine.transforms.fpe import fpe_decrypt_value, fpe_encrypt_value

_DIGITS = "0123456789"


@st.composite
def _card(draw: st.DrawFn) -> tuple[int, str]:
    keep_bin = draw(st.sampled_from([6, 8]))
    length = draw(st.integers(min_value=keep_bin + 7, max_value=19))
    first = draw(st.sampled_from("23456"))
    rest = draw(st.text(alphabet=_DIGITS, min_size=length - 2, max_size=length - 2))
    digits = first + rest
    digits += checksums.calc_check_digit("luhn", digits)
    sep = draw(st.sampled_from(["", " ", "-"]))
    grouped = sep.join(digits[i : i + 4] for i in range(0, len(digits), 4))
    return keep_bin, grouped


@settings(derandomize=True, max_examples=300, deadline=None)
@given(
    card=_card(),
    key=st.binary(min_size=FF1_KEY_BYTES, max_size=FF1_KEY_BYTES),
    tweak=st.binary(min_size=0, max_size=32),
)
def test_keep_bin_property(card: tuple[int, str], key: bytes, tweak: bytes) -> None:
    keep_bin, source = card
    out = fpe_encrypt_value(source, key, _DIGITS, tweak, True, False, "luhn", keep_bin=keep_bin)
    assert len(out) == len(source)
    assert [c for c in out if not c.isdigit()] == [c for c in source if not c.isdigit()]
    assert [i for i, c in enumerate(out) if not c.isdigit()] == [
        i for i, c in enumerate(source) if not c.isdigit()
    ]
    out_digits = "".join(c for c in out if c.isdigit())
    src_digits = "".join(c for c in source if c.isdigit())
    assert out_digits[:keep_bin] == src_digits[:keep_bin]
    assert checksums.validate("luhn", out_digits)
    back = fpe_decrypt_value(out, key, _DIGITS, tweak, True, False, "luhn", keep_bin=keep_bin)
    assert back == source
