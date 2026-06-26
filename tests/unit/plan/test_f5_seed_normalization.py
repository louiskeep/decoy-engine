"""F5 (2026-06-26): one shared seed validator + profile-path bool rejection.

Pre-fix the pipeline profile path used ``isinstance(job_seed_raw, int)``, which
is ``True`` for ``bool`` (``isinstance(True, int)``), so ``seed: true`` seeded
``random.Random(True) == random.Random(1)`` and produced byte-identical
profiles to ``seed: 1`` -- an observable coercion -- before ``compile_plan``
later rejected the bool. The fix routes the profile path (and generation)
through the single shared ``_normalize_job_seed_int`` validator and tightens the
``profile_source`` config-seed fallback so a bool is never used as a seed.
"""

from __future__ import annotations

import pytest

from decoy_engine.plan._seed import _normalize_job_seed, _normalize_job_seed_int
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.profile import profile_source


class TestSharedSeedValidator:
    """`_normalize_job_seed_int` is the single canonical validator used by the
    pipeline profile path, the plan compiler, and generation."""

    def test_accepts_int(self) -> None:
        assert _normalize_job_seed_int({"global_settings": {"seed": 7}}) == 7

    def test_absent_defaults_to_zero(self) -> None:
        assert _normalize_job_seed_int({}) == 0
        assert _normalize_job_seed_int({"global_settings": {}}) == 0
        assert _normalize_job_seed_int({"global_settings": {"seed": None}}) == 0

    def test_rejects_bool(self) -> None:
        # isinstance(True, int) is True; the validator must still reject it so
        # `seed: true` cannot masquerade as `seed: 1`.
        with pytest.raises(PlanCompileError) as exc:
            _normalize_job_seed_int({"global_settings": {"seed": True}})
        assert exc.value.code == "seed_not_numeric"

    def test_rejects_float(self) -> None:
        with pytest.raises(PlanCompileError) as exc:
            _normalize_job_seed_int({"global_settings": {"seed": 1.5}})
        assert exc.value.code == "seed_not_numeric"

    def test_bytes_wrapper_matches_int(self) -> None:
        # The bytes form derive(...) consumes is just the int, big-endian.
        assert _normalize_job_seed({"global_settings": {"seed": 7}}) == (7).to_bytes(8, "big")


class TestProfileBoolFallbackGuard:
    """`profile_source` must not treat a config-side bool seed as an int via its
    defensive config-seed fallback."""

    def test_bool_config_seed_is_not_used_as_seed(self) -> None:
        # seed kwarg omitted -> the fallback reads global_settings.seed. A bool
        # must be rejected, leaving the seed None, which fires the
        # non-deterministic-sampling warning. Pre-fix, True satisfied the
        # isinstance(int) check and silenced the warning (== seed 1).
        cfg = {"sources": {}, "global_settings": {"seed": True}}
        with pytest.warns(UserWarning, match="without a seed"):
            profile_source(cfg)

    def test_int_config_seed_is_used_no_warning(self) -> None:
        # A real int seed in config satisfies the fallback -> no warning.
        import warnings

        cfg = {"sources": {}, "global_settings": {"seed": 1}}
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            profile_source(cfg)  # must not raise: no "without a seed" warning
