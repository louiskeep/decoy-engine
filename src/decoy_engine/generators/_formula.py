"""Formula column evaluation, split out of columns.py (F11a).

``_formula_hash_keyed`` is the formula sandbox's keyed hash primitive; the
mixin holds the per-row formula evaluation paths. Folded into
``ColumnGenerator`` so behavior is byte-identical to the in-class versions;
extracted only to keep the generator module under the size cap.
"""

from __future__ import annotations

import random
from typing import Any

import pandas as pd

from decoy_engine.expressions import BASE_GLOBALS, safe_eval
from decoy_engine.internal.crypto import hmac_hex


def _formula_hash_keyed(value: str, local_seed: int) -> str:
    """Formula-sandbox keyed hash. Replaces the legacy SHA256(value + seed)
    path with HMAC-SHA256(key, value) where the key is derived from
    `local_seed`.

    Carry from QA-internal-synth-providers F12 (PO 2026-06-01 follow-on
    to the M1 shim): the legacy `deterministic_hash` was reversible given
    a known (value, hash) pair plus the seed. New code paths must use
    keyed primitives (HMAC-SHA256 via `internal.crypto.hmac_hex`).

    SEED_PROTOCOL_VERSION 3 -> 4 in the same change because the per-
    row byte output of `hash(col)` formulas changes (HMAC vs raw
    SHA256). Pre-GA, no manifests to break (PO confirmed).

    `local_seed` is the int the existing formula sandbox already builds
    as `column_seed + row_index` so this swap is wire-compatible with
    the prior interface; only the output bytes change.
    """
    # 8-byte key derived from the local_seed via modular wrap. Stable
    # across runs (same int -> same key). Repeated 4x to fill a 32-byte
    # buffer (HMAC-SHA256's natural block size); RFC 2104 accepts any
    # key length but >= block-size is preferred + collision-free for
    # the per-row keying purpose.
    seed_bytes = (local_seed & ((1 << 64) - 1)).to_bytes(8, "big")
    return hmac_hex(seed_bytes * 4, value)[:8]


class _FormulaMixin:
    """Per-row formula evaluation for :class:`ColumnGenerator`."""

    def _generate_formula_column(
        self,
        num_rows: int,
        column_config: dict[str, Any],
        table_name: str,
        reference_data: dict[str, pd.DataFrame],
    ) -> pd.Series:
        """
        Generate data based on a formula.

        Single inline path: every formula is a Python expression (write
        ``f"..."`` yourself if you want template-like substitution). Drops
        the previous three-way dispatch (basic / template / composite).

        When ``references: [...]`` is set on the column config, this method
        emits a None-filled placeholder series -- the column's actual values
        are filled by the in-memory post-pass
        (``fill_referenced_formula_column``, driven by the v2
        ``generation._referenced_formula.fill_referenced_formula_columns``) AFTER
        every other column has been generated, so the formula can read its
        siblings. When ``references`` is empty/missing, the formula is
        evaluated inline per row with deterministic seeding.

        Args:
            num_rows: Number of rows to generate
            column_config: Configuration for this column
            table_name: Name of the table this column belongs to
            reference_data: Dictionary of previously generated tables

        Returns:
            pandas.Series with generated data (or None placeholders when
            the column has cross-column references -- filled in post-pass).
        """
        formula = column_config.get("formula", "")
        column_name = column_config.get("name", "unnamed_column")
        references = column_config.get("references", []) or []

        if not formula:
            self.logger.warning("No formula provided in configuration")
            return pd.Series([None] * num_rows)

        if references:
            # Defer to the post-pass: this column reads sibling columns,
            # which haven't been generated yet during the per-column loop.
            self.logger.debug(
                f"Formula column '{column_name}' references {references} -- deferring to post-pass."
            )
            return pd.Series([None] * num_rows, dtype=object)

        return self._eval_formula_inline(
            num_rows,
            formula,
            column_name,
            column_config,
        )

    def fill_referenced_formula_column(
        self,
        col_name: str,
        formula: str,
        references: list[str],
        out: pd.DataFrame,
    ) -> pd.Series:
        """Evaluate a formula column whose expression reads sibling columns.

        Called by the v2 generation post-pass
        (``generation._referenced_formula.fill_referenced_formula_columns``) after
        the per-column loop has produced every other column, so ``out``
        contains finalized values for each referenced column. Uses the same
        per-row deterministic seeding and safe_eval scope as
        ``_eval_formula_inline``, operating on the in-memory DataFrame.
        """
        missing = [r for r in references if r not in out.columns]
        if missing:
            self.logger.warning(
                f"Formula column {col_name!r} references missing columns "
                f"{missing!r} -- emitting None"
            )
            return pd.Series([None] * len(out), dtype=object)

        gen_ctx = self._column_ctx(col_name)
        values: list = []
        # Track null sibling cells coerced to '' so the substitution is
        # diagnosable: a null in a referenced column silently blanks the formula
        # input otherwise (e.g. "first last" -> "first " on a null last name).
        null_subs: dict[str, int] = {}
        # F2/F3 (2026-06-26): per-row seeds are full-width family derivations
        # (py for the formula RNG + keyed hash, faker for Faker) instead of
        # column_seed + i, so adjacent columns no longer correlate. faker
        # .seed_instance still serializes module-level state internally (Faker
        # library limitation; see synthesize.py _FAKER_CALL_LOCK).
        row_rng = random.Random()
        for i in range(len(out)):
            local_seed = gen_ctx.row_int("py", i)
            row_rng.seed(local_seed)
            self.faker.seed_instance(gen_ctx.row_int("faker", i))

            scope = self._formula_scope(local_seed, rng=row_rng)
            scope["i"] = i
            scope["index"] = i
            for ref in references:
                val = out.at[i, ref]
                if val is None or (isinstance(val, float) and pd.isna(val)):
                    null_subs[ref] = null_subs.get(ref, 0) + 1
                    val = ""
                scope[ref] = val

            try:
                result = safe_eval(formula, BASE_GLOBALS, scope)
                values.append(result)
            except Exception as exc:
                self.logger.warning(f"Formula column {col_name!r} row {i} eval error: {exc}")
                values.append(None)

        if null_subs:
            self.logger.warning(
                f"Formula column {col_name!r}: substituted '' for null cells in "
                f"referenced column(s) {null_subs!r}; blanks may appear in the output."
            )
        return pd.Series(values, dtype=object)

    def _eval_formula_inline(
        self,
        num_rows: int,
        formula: str,
        column_name: str = "unnamed_column",
        column_config: dict[str, Any] | None = None,
    ) -> pd.Series:
        """Per-row eval of a Python expression. Same deterministic seeding
        as the legacy ``basic`` path: ``column_seed + row_index`` reseeds
        ``random`` and the Faker instance per row. When the column's
        config has ``determinism: fresh``, the column-seed comes from
        os.urandom -- internal consistency holds within a run, but the
        column rolls per run.

        Scope per row (via :func:`decoy_engine.expressions.safe_eval` +
        :data:`decoy_engine.expressions.BASE_GLOBALS`):
          - ``i`` / ``index`` -- row number
          - ``random`` / ``randint`` / ``choice`` -- RNG (deterministic per row)
          - ``hash`` -- short deterministic hash
          - ``str`` / ``int`` / ``float`` / ``round`` / ``min`` / ``max`` / ``len``
          - Faker date helpers + arithmetic (``today``, ``days_from_now``, ...)

        Cross-column refs aren't reachable here -- that's the post-pass."""
        gen_ctx = self._column_ctx(column_name, column_config)
        values = []
        # F2/F3 (2026-06-26): per-row family derivations (py for the formula
        # RNG + keyed hash, faker for Faker) replace column_seed + i. See
        # fill_referenced_formula_column for the rationale.
        row_rng = random.Random()
        for i in range(num_rows):
            local_seed = gen_ctx.row_int("py", i)
            row_rng.seed(local_seed)
            self.faker.seed_instance(gen_ctx.row_int("faker", i))

            scope = self._formula_scope(local_seed, rng=row_rng)
            scope["i"] = i
            scope["index"] = i

            try:
                result = safe_eval(formula, BASE_GLOBALS, scope)
                values.append(result)
            except Exception as e:
                error_msg = str(e)
                if "not defined" in error_msg:
                    self.logger.warning(f"Name not available in formula for row {i}: {error_msg}")
                    self.logger.info(f"Available names: {sorted(list(scope.keys()))}")
                else:
                    self.logger.warning(f"Error evaluating formula for row {i}: {error_msg}")
                self.logger.debug(f"Formula: {formula}")
                values.append(None)

        return pd.Series(values)

    def _formula_scope(self, local_seed: int, rng: random.Random | None = None) -> dict[str, Any]:
        """Build the names available inside a formula eval. Shared between
        the inline path (``_eval_formula_inline``) and the cross-column
        post-pass (``fill_referenced_formula_column``) so users get the
        same vocabulary regardless of whether their formula reads other
        columns. Per-row seed is captured into the closure so RNG calls
        within the eval stay deterministic.

        QA-1 M21 (2026-06-01): ``random``/``randint``/``choice`` bind to
        the passed-in ``rng`` instance instead of module-level
        ``random``. Pre-fix two formula columns in the same job shared
        module-global random state and column B's output depended on
        column A's execution order. With a per-row rng, column B's
        sequence is a pure function of (column_seed, row_index).
        Backwards-compatible: when ``rng`` is None, falls back to the
        module-global pattern for any caller that hasn't migrated yet.

        QA-1 H7 (2026-06-01): ``now``/``today``/``days_from_now``/
        ``months_from_now``/``years_from_now`` now read
        ``self._reference_date`` (snapshotted at construction time)
        instead of ``pd.Timestamp.now()`` per call. The same formula
        run on two different calendar days against the same generator
        returns identical output.
        """
        _rng = rng if rng is not None else random
        ref_date = self._reference_date
        return {
            # RNG bound to the per-row instance (M21).
            "random": _rng.random,
            "randint": lambda a, b: _rng.randint(a, b),
            "choice": lambda lst: _rng.choice(lst),
            # Normal-distribution draw from the per-row RNG instance.
            # Enables formulas like gauss(mu, sigma) for numeric generation.
            "gauss": lambda m, s: _rng.gauss(m, s),
            # Numeric / string utilities
            "round": round,
            "min": min,
            "max": max,
            "len": len,
            "str": str,
            "int": int,
            "float": float,
            # Formula-hash migration (PO Q-formula-hash 2026-06-01):
            # hash() in the formula sandbox now uses HMAC-SHA256 keyed
            # by the per-row local_seed (see _formula_hash_keyed at
            # module top). Replaces the M1 shim around the legacy
            # deterministic_hash. SEED_PROTOCOL_VERSION 3 -> 4 in the
            # same change because per-row output bytes differ.
            "hash": lambda x: _formula_hash_keyed(str(x), local_seed),
            # Faker date helpers
            "date_between": self.faker.date_between,
            "date_this_decade": self.faker.date_this_decade,
            "date_this_year": self.faker.date_this_year,
            "date_this_month": self.faker.date_this_month,
            "future_date": self.faker.future_date,
            "past_date": self.faker.past_date,
            "date_of_birth": self.faker.date_of_birth,
            "time": lambda: self.faker.time(),
            # Wall-clock helpers bound to reference_date (H7).
            "now": lambda fmt="%Y-%m-%d": ref_date.strftime(fmt),
            "today": lambda fmt="%Y-%m-%d": ref_date.strftime(fmt),
            "days_from_now": lambda days: (ref_date + pd.Timedelta(days=days)).strftime("%Y-%m-%d"),
            "months_from_now": lambda months: (ref_date + pd.DateOffset(months=months)).strftime(
                "%Y-%m-%d"
            ),
            "years_from_now": lambda years: (ref_date + pd.DateOffset(years=years)).strftime(
                "%Y-%m-%d"
            ),
            "format_date": lambda date_obj, fmt="%Y-%m-%d": (
                date_obj.strftime(fmt) if hasattr(date_obj, "strftime") else str(date_obj)
            ),
        }
