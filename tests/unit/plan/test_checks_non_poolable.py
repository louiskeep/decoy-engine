"""Row 11: non_poolable_provider_with_pool_backend (audit H5, 2026-06-12).

Pre-fix, `strategy: faker` on a provider with poolable=False (uuid,
lorem_text-style) passed every compile check and crashed at runtime with
PoolCapacityError[provider_not_poolable] -- which is exactly how 3 of
the 5 shipped CLI templates (hipaa/pci/gdpr) were dead-on-arrival while
`decoy validate` exited 0. These cells pin the compile-time rejection
and the config-only check API that lets `validate` catch it.
"""

from __future__ import annotations

import pytest

from decoy_engine.plan import PlanCompileError, run_config_only_checks
from decoy_engine.plan._checks import check_non_poolable_provider_with_pool_backend


def _cfg(columns: list[dict]) -> dict:
    return {"version": 1, "tables": [{"name": "t", "columns": columns}]}


class TestNonPoolableProviderWithPoolBackend:
    def test_faker_uuid_rejected(self):
        cfg = _cfg([{"name": "device_id", "strategy": "faker", "provider": "uuid"}])
        with pytest.raises(PlanCompileError) as exc:
            check_non_poolable_provider_with_pool_backend(cfg)
        assert exc.value.code == "non_poolable_provider_with_pool_backend"
        assert "device_id" in exc.value.message

    def test_faker_poolable_provider_passes(self):
        cfg = _cfg([{"name": "email", "strategy": "faker", "provider": "person_email"}])
        check_non_poolable_provider_with_pool_backend(cfg)  # no raise

    def test_non_faker_strategy_with_non_poolable_provider_passes(self):
        # hash does not route through the pool; uuid as a provider hint
        # on a keyed strategy is not this check's concern.
        cfg = _cfg([{"name": "id", "strategy": "hash", "provider": "uuid"}])
        check_non_poolable_provider_with_pool_backend(cfg)  # no raise

    def test_unknown_provider_is_not_this_checks_concern(self):
        cfg = _cfg([{"name": "x", "strategy": "faker", "provider": "no_such_provider"}])
        check_non_poolable_provider_with_pool_backend(cfg)  # row 2 owns it

    def test_malformed_entries_skipped(self):
        cfg = {"version": 1, "tables": [{"name": "t", "columns": ["junk", {"name": "a"}]}, "junk"]}
        check_non_poolable_provider_with_pool_backend(cfg)  # no raise


class TestBrokenTemplateShapesRejected:
    """The three shipped-template column shapes the audit found DOA."""

    @pytest.mark.parametrize(
        "column",
        [
            {"name": "vehicle_id", "strategy": "faker", "provider": "uuid"},  # hipaa
            {  # pci (also declared deterministic on a non-deterministic provider)
                "name": "transaction_id",
                "strategy": "faker",
                "provider": "uuid",
                "deterministic": True,
                "namespace": "transaction_identity",
            },
            {"name": "device_id", "strategy": "faker", "provider": "uuid"},  # gdpr
        ],
    )
    def test_template_shape_rejected_at_compile(self, column):
        with pytest.raises(PlanCompileError) as exc:
            check_non_poolable_provider_with_pool_backend(_cfg([column]))
        assert exc.value.code == "non_poolable_provider_with_pool_backend"


class TestRunConfigOnlyChecks:
    def test_clean_config_returns_check_names(self):
        cfg = _cfg([{"name": "email", "strategy": "faker", "provider": "person_email"}])
        names = run_config_only_checks(cfg)
        assert names == (
            "unknown_provider",
            "when_with_coherent_with",
            "deterministic_namespace_completeness",
            "non_poolable_provider_with_pool_backend",
            "statistical_columns",
            "text_redact_ner_available",
            "vault_columns",
        )

    def test_raises_on_unknown_provider(self):
        cfg = _cfg([{"name": "x", "strategy": "faker", "provider": "no_such_provider"}])
        with pytest.raises(PlanCompileError) as exc:
            run_config_only_checks(cfg)
        assert exc.value.code == "unknown_provider"

    def test_raises_on_non_poolable_pool_backend(self):
        cfg = _cfg([{"name": "device_id", "strategy": "faker", "provider": "uuid"}])
        with pytest.raises(PlanCompileError) as exc:
            run_config_only_checks(cfg)
        assert exc.value.code == "non_poolable_provider_with_pool_backend"

    def test_raises_on_when_with_coherent_with(self):
        cfg = _cfg(
            [
                {
                    "name": "a",
                    "strategy": "faker",
                    "provider": "person_email",
                    "when": "b > 1",
                    "coherent_with": ["c"],
                }
            ]
        )
        with pytest.raises(PlanCompileError) as exc:
            run_config_only_checks(cfg)
        assert exc.value.code == "when_with_coherent_with_unsupported"

    def test_check_set_is_subset_of_compile_plan(self):
        # Drift guard between the two surfaces: every config-only check
        # must exist in compile_plan's full checks_passed contract.
        from tests.unit.plan.test_compile_s2_refactor import EXPECTED_S2_CHECKS_PASSED

        cfg = _cfg([{"name": "email", "strategy": "faker", "provider": "person_email"}])
        names = set(run_config_only_checks(cfg))
        full = set(EXPECTED_S2_CHECKS_PASSED) | {"when_with_coherent_with"}
        assert names <= full
