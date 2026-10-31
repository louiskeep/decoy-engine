"""Masking-side per-draw-site determinism providers (native program, Task 0.3).

The compound source-keyed sites (the FF1 column key, the code_set and
joint_mask two-step keyed selections), the whole-column / per-group / per-row
numpy streams (shuffle, grouped-series walk, windowed date) and the
self-seeded ``mask.formula`` Random. The text-mask span providers stay in
``_draw_site_providers.py`` beside their ``_span_key``. Re-exported from
``_draw_site_providers.py``; see its docstring for the cited methodology.
"""

from __future__ import annotations

import hashlib
import hmac
import random
from typing import Any

import numpy as np

from decoy_engine.determinism import derive
from decoy_engine.execution.native.protocol.providers_base import DrawSiteProvider

# ---------------------------------------------------------------------------
# source_keyed_hmac COMPOUND sites: the draw is a two-step keyed derivation
# (a derive() key, then an HMAC-hex modular selection) or a fixed-label column
# key. A single derive() call cannot express these, so each is its own provider
# that reproduces the EXACT keyed material the shipped transform computes. The
# downstream arithmetic (FF1 rounds for fpe; hole-resolve for code_set) is
# transform semantics, not randomness, and stays with the transform / Task 0.4.
#
# The three byte-constants below are cited from the shipped modules; a drift
# test (test_determinism_protocol.py) asserts each equals the shipped symbol.
# hmac_hex / hole_resolve are reimplemented inline (they are three-line pure
# utilities) rather than imported, to keep this frozen protocol module free of
# a heavy transforms-package import cycle; drift tests pin both to the shipped
# implementations byte-for-byte.
# ---------------------------------------------------------------------------

# transforms/fpe.py (FF1_KEY_LABEL). Task 5.2: retired the Feistel-era
# `b"fpe-key/v1"` label; the drift test now pins this to the FF1 key label.
_FPE_KEY_LABEL = b"ff1-key/v1"
# transforms/code_set.py:132 (_KEYED_SALT).
_CODE_SET_KEYED_SALT = b"decoy.code_set.keyed_access.v1"
# transforms/joint_mask.py:64 (_KEYED_ROW_SOURCE).
_JOINT_MASK_KEYED_ROW_SOURCE = b"joint_mask/keyed_row/v1"


def _hmac_hex(key: bytes, value: Any) -> str | None:
    """HMAC-SHA256(key, str(value)) hex. Mirrors ``internal/crypto.py:hmac_hex``."""
    if value is None:
        return None
    return hmac.new(key, str(value).encode("utf-8", errors="replace"), hashlib.sha256).hexdigest()


def _hole_resolve(idx: int, position: int | None) -> int:
    """Map an index into ``seq`` minus the hole at ``position`` back to ``seq``.

    Mirrors ``transforms/_codeset_index.py:hole_resolve`` exactly.
    """
    if position is None or idx < position:
        return idx
    return idx + 1


class FpeKeyProvider(DrawSiteProvider):
    """``mask.fpe``: the per-column FF1 KEY (arithmetic deferred to Task 0.4).

    Shipped FPE derives ONE AES-256 key per ``(mask_key, namespace)`` as
    ``derive(mask_key, namespace, FF1_KEY_LABEL)`` (a fixed label, NOT a
    per-value source), then applies NIST SP 800-38G FF1 with a tweak built
    from the column name (``execution/_strategies/_fpe.py``,
    ``transforms/fpe.py``). This provider reproduces the KEY only; the FF1
    arithmetic is the transform's (and Task 0.4's pure-Python reference). The
    key is source-independent, so partitionable.
    """

    draw_site_id = "mask.fpe"

    def column_key(self, mask_key: bytes, namespace: str) -> bytes:
        return derive(mask_key, namespace, _FPE_KEY_LABEL)

    def partitioned_draw(self, mask_key: bytes, namespace: str) -> bytes:
        self.assert_partitionable()
        return self.column_key(mask_key, namespace)


class CodeSetKeyedSelectProvider(DrawSiteProvider):
    """``mask.code_set`` (mask mode): two-step keyed selection index.

    Shipped mask-mode selection (``transforms/code_set.py:_pick_from_seq``):
    ``hmac_key = derive(mask_key, namespace or "code_set", _KEYED_SALT)``, then
    ``idx = int(hmac_hex(hmac_key, key_value)[:8], 16) % candidate_count``, then
    ``seq[hole_resolve(idx, position)]``. Keyed on the masked key value, so
    partitionable. (Gen mode is a separate ``derive_index(job_seed, ...)`` path;
    the strategy's primary draw is the mask-mode selection reproduced here.)
    """

    draw_site_id = "mask.code_set"

    def hmac_key(self, mask_key: bytes, namespace: str | None) -> bytes:
        return derive(mask_key, namespace or "code_set", _CODE_SET_KEYED_SALT)

    def select_index(
        self, mask_key: bytes, namespace: str | None, key_value: str, candidate_count: int
    ) -> int:
        hex_digest = _hmac_hex(self.hmac_key(mask_key, namespace), key_value)
        if hex_digest is None:
            raise ValueError("hmac_hex returned None for a non-None key_value")
        return int(hex_digest[:8], 16) % candidate_count

    def resolve_hole(self, idx: int, position: int | None) -> int:
        return _hole_resolve(idx, position)

    def partitioned_draw(
        self, mask_key: bytes, namespace: str | None, key_value: str, candidate_count: int
    ) -> int:
        self.assert_partitionable()
        return self.select_index(mask_key, namespace, key_value, candidate_count)


class JointMaskKeyedRowProvider(DrawSiteProvider):
    """``mask.joint_mask_keyed_row``: two-step keyed reference-row index.

    Shipped selection (``reference_tables/_types.py:keyed_row`` via
    ``transforms/joint_mask.py``): ``hmac_key = derive(mask_key, namespace,
    _KEYED_ROW_SOURCE)``, then ``index = int(hmac_hex(hmac_key, key_value)[:8], 16)
    % row_count`` into the id-sorted table. Keyed on the masked key value, so
    partitionable (deterministic WITHIN a table version only; the index shifts if
    ``row_count`` changes).
    """

    draw_site_id = "mask.joint_mask_keyed_row"

    def hmac_key(self, mask_key: bytes, namespace: str) -> bytes:
        return derive(mask_key, namespace, _JOINT_MASK_KEYED_ROW_SOURCE)

    def row_index(self, mask_key: bytes, namespace: str, key_value: str, row_count: int) -> int:
        hex_digest = _hmac_hex(self.hmac_key(mask_key, namespace), key_value)
        if hex_digest is None:
            raise ValueError("hmac_hex returned None for a non-None key_value")
        return int(hex_digest[:8], 16) % row_count

    def partitioned_draw(
        self, mask_key: bytes, namespace: str, key_value: str, row_count: int
    ) -> int:
        self.assert_partitionable()
        return self.row_index(mask_key, namespace, key_value, row_count)


# ---------------------------------------------------------------------------
# numpy_pcg64 whole-column streams: NON-partitionable (stream-positional).
# ---------------------------------------------------------------------------


class ShuffleProvider(DrawSiteProvider):
    """``mask.shuffle``: whole-column multiset permutation (numpy PCG64).

    Seed EXACTLY ``int.from_bytes(derive(mask_key, namespace, column.encode())[:8], "big")``,
    then ``np.random.default_rng(seed).permutation(len(non_null))``. Whole-column,
    so NON-partitionable: a partition sees only its slice and cannot reproduce the global order.
    """

    draw_site_id = "mask.shuffle"

    def seed_int(self, mask_key: bytes, namespace: str, column: str) -> int:
        return int.from_bytes(derive(mask_key, namespace, column.encode("utf-8"))[:8], "big")

    def permutation(self, mask_key: bytes, namespace: str, column: str, n: int) -> np.ndarray:
        rng = np.random.default_rng(self.seed_int(mask_key, namespace, column))
        return rng.permutation(n)


class GroupedSeriesWalkProvider(DrawSiteProvider):
    """``mask.grouped_series_monotone_walk``: per-group sequential stream.

    Per group label ``g``: seed
    ``int.from_bytes(derive(mask_key, namespace, str(g).encode("utf-8", "replace"))[:8], "big")``
    seeds ``default_rng(g_seed)``, advanced per row within the group by
    ``integers(step, max_step + 1)`` and accumulated from ``start``. NON-partitionable:
    splitting a group across partitions loses the running sum.
    """

    draw_site_id = "mask.grouped_series_monotone_walk"

    def group_seed_int(self, mask_key: bytes, namespace: str, group_label: Any) -> int:
        source = str(group_label).encode("utf-8", errors="replace")
        return int.from_bytes(derive(mask_key, namespace, source)[:8], "big")

    def group_generator(
        self, mask_key: bytes, namespace: str, group_label: Any
    ) -> np.random.Generator:
        return np.random.default_rng(self.group_seed_int(mask_key, namespace, group_label))

    def walk(
        self,
        mask_key: bytes,
        namespace: str,
        group_label: Any,
        group_len: int,
        *,
        start: int,
        step: int,
        max_step: int,
    ) -> list[int]:
        """Reproduce one group's monotone walk: ``start`` then cumulative steps."""
        rng = self.group_generator(mask_key, namespace, group_label)
        out: list[int] = []
        cumulative = start
        for _ in range(group_len):
            out.append(cumulative)
            cumulative += int(rng.integers(step, max_step + 1))
        return out


class WindowedDateProvider(DrawSiteProvider):
    """``mask.windowed_date``: fresh per-row numpy Generator seeded by row index.

    Per row ``i``: seed
    ``int.from_bytes(derive(mask_key, namespace, i.to_bytes(8, "big"))[:8], "big")``,
    then ``default_rng(row_seed).integers(min_days, max_days + 1)`` (1 draw for
    ``uniform``, 2 for ``early``/``late``). Keys on the GLOBAL row index, so
    partitionable only when the native executor pins ``i`` to a global row number.
    """

    draw_site_id = "mask.windowed_date"

    def row_seed_int(self, mask_key: bytes, namespace: str, global_row_index: int) -> int:
        source = global_row_index.to_bytes(8, "big")
        return int.from_bytes(derive(mask_key, namespace, source)[:8], "big")

    def row_generator(
        self, mask_key: bytes, namespace: str, global_row_index: int
    ) -> np.random.Generator:
        return np.random.default_rng(self.row_seed_int(mask_key, namespace, global_row_index))

    def partitioned_draw(
        self, mask_key: bytes, namespace: str, global_row_index: int
    ) -> np.random.Generator:
        """A partition reproduces row ``i`` from its GLOBAL index, unchanged."""
        self.assert_partitionable()
        return self.row_generator(mask_key, namespace, global_row_index)


class MaskFormulaProvider(DrawSiteProvider):
    """``mask.formula``: one ``random.Random`` self-seeded from the formula text.

    ``formula_seed = int(sha256(f"{column}|{formula}").hexdigest()[:16], 16)``,
    then ``random.Random(formula_seed)`` shared across all rows via
    ``column.apply``. Order-dependent (a row draws only if the formula calls the
    RNG), so NON-partitionable, and independent of ``mask_key`` by design.
    """

    draw_site_id = "mask.formula"

    def formula_seed(self, column: str, formula: str) -> int:
        seed_material = f"{column}|{formula}".encode()
        return int(hashlib.sha256(seed_material).hexdigest()[:16], 16)

    def rng(self, column: str, formula: str) -> random.Random:
        return random.Random(self.formula_seed(column, formula))
