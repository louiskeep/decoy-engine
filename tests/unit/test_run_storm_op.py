"""Unit tests for the run_storm graph op."""

import warnings

import pandas as pd
import pytest

from decoy_engine.context import ExecutionContext
from decoy_engine.graph.ops import OPS, run_storm
from decoy_engine.graph.ops._base import OpError
from decoy_engine.internal.validator import ValidationError


@pytest.fixture
def df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "patient_id": [1, 2, 3, 4, 5],
            "ssn": [
                "111-22-3333", "222-33-4444", "333-44-5555",
                "444-55-6666", "555-66-7777",
            ],
            "email": [
                "a@x.com", "b@x.com", "c@x.com", "d@x.com", "e@x.com",
            ],
            "state": ["CA", "NY", "CA", "TX", "CA"],
        }
    )


class TestRegistration:
    def test_run_storm_registered(self):
        assert "run_storm" in OPS


class TestValidation:
    @pytest.mark.parametrize(
        "cfg",
        [
            {},
            {"source_label": "patients"},
            {"source_label": "patients", "sample_strategy": "head"},
            {"sample_strategy": "random", "sample_row_cap": 1000},
        ],
    )
    def test_valid_configs(self, cfg):
        run_storm.validate_config(cfg)

    @pytest.mark.parametrize(
        "cfg,path_substr",
        [
            ({"source_label": ""}, "config.source_label"),
            ({"source_label": "  "}, "config.source_label"),
            ({"source_label": 5}, "config.source_label"),
            ({"sample_strategy": "weird"}, "config.sample_strategy"),
            ({"sample_strategy": 5}, "config.sample_strategy"),
            ({"sample_row_cap": 0}, "config.sample_row_cap"),
            ({"sample_row_cap": -1}, "config.sample_row_cap"),
            ({"sample_row_cap": "100"}, "config.sample_row_cap"),
            ({"sample_row_cap": True}, "config.sample_row_cap"),
        ],
    )
    def test_invalid_configs(self, cfg, path_substr):
        with pytest.raises(ValidationError) as exc:
            run_storm.validate_config(cfg)
        assert path_substr in (exc.value.path or "")


class TestApply:
    def test_passes_dataframe_through_unchanged(self, df):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            ctx = ExecutionContext()
            out = run_storm.apply([df], {"source_label": "patients"}, ctx)
        # Same shape, same content — run_storm is a side-channel observer.
        assert out is df or out.equals(df)
        assert list(out.columns) == ["patient_id", "ssn", "email", "state"]
        assert len(out) == 5

    def test_captures_profile_to_ctx(self, df):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            ctx = ExecutionContext()
            run_storm.apply([df], {"source_label": "patients"}, ctx)

        assert len(ctx.captured_outputs) == 1
        entry = ctx.captured_outputs[0]
        assert entry["kind"] == "storm_profile"
        assert entry["source_label"] == "patients"
        # No parent declared -> hint absent from the captured entry.
        assert "parent_source_label" not in entry
        # profile.to_dict() round-trip — fields should be present
        profile = entry["profile"]
        assert profile["source_label"] == "patients"
        assert profile["row_count"] == 5
        assert isinstance(profile["fields"], list)
        assert len(profile["fields"]) == 4  # one per column

    def test_parent_source_label_flows_into_captured_entry(self, df):
        """Sprint G follow-on: declare a parent label and verify the
        captured-output entry carries the hint for the platform runner
        to resolve into a source_scan_id."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            ctx = ExecutionContext()
            run_storm.apply(
                [df],
                {
                    "source_label": "patients_masked",
                    "parent_source_label": "patients",
                },
                ctx,
            )

        assert len(ctx.captured_outputs) == 1
        entry = ctx.captured_outputs[0]
        assert entry["source_label"] == "patients_masked"
        assert entry["parent_source_label"] == "patients"

    def test_parent_source_label_empty_or_whitespace_rejected(self):
        from decoy_engine.internal.validator import ValidationError

        for bad in ("", "  ", 42):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                try:
                    run_storm.validate_config(
                        {
                            "source_label": "patients",
                            "parent_source_label": bad,
                        }
                    )
                except ValidationError as exc:
                    assert "parent_source_label" in str(exc)
                else:
                    raise AssertionError(
                        f"Expected ValidationError for parent_source_label={bad!r}"
                    )

    def test_default_source_label_when_omitted(self, df):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            ctx = ExecutionContext()
            run_storm.apply([df], {}, ctx)

        # Falls back to a sensible default; platform overrides this with
        # the pipeline name before calling the engine.
        assert ctx.captured_outputs[0]["source_label"] == "graph_run"

    def test_no_ctx_does_not_crash(self, df):
        # Engine is permissive — None ctx must work, just no capture.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            out = run_storm.apply([df], {"source_label": "patients"}, None)
        assert len(out) == 5

    def test_ctx_without_captured_outputs_attr_does_not_crash(self, df):
        # Older callers may pass an object that doesn't have the field.
        class StubCtx:
            logger = None

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            run_storm.apply([df], {"source_label": "patients"}, StubCtx())

    def test_multiple_run_storm_nodes_all_captured(self, df):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            ctx = ExecutionContext()
            run_storm.apply([df], {"source_label": "first"}, ctx)
            run_storm.apply([df], {"source_label": "second"}, ctx)

        labels = [e["source_label"] for e in ctx.captured_outputs]
        assert labels == ["first", "second"]


class TestEndToEndGraph:
    """run_storm wired into a real graph run via run_graph."""

    def test_graph_run_captures_profile(self, tmp_path):
        import yaml as _yaml

        from decoy_engine import run_graph

        # Build a tiny pipeline: source.file → run_storm → target.file
        src = tmp_path / "in.csv"
        out = tmp_path / "out.csv"
        pd.DataFrame({"x": [1, 2, 3], "y": ["a", "b", "c"]}).to_csv(src, index=False)

        cfg = {
            "mode": "graph",
            "nodes": [
                {"id": "s", "kind": "source.file", "config": {"path": str(src)}},
                {"id": "rs", "kind": "run_storm",
                 "config": {"source_label": "tiny"}},
                {"id": "t", "kind": "target.file",
                 "config": {"output_filename": str(out)}},
            ],
            "edges": [
                {"from": "s", "to": "rs"},
                {"from": "rs", "to": "t"},
            ],
        }
        ctx = ExecutionContext()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            result = run_graph(_yaml.safe_dump(cfg), ctx=ctx)

        assert result["success"] is True
        # Output file matches input — run_storm passes data through.
        assert out.exists()
        written = pd.read_csv(out)
        assert len(written) == 3
        assert list(written.columns) == ["x", "y"]

        # And the profile was captured to ctx.
        assert len(ctx.captured_outputs) == 1
        entry = ctx.captured_outputs[0]
        assert entry["kind"] == "storm_profile"
        assert entry["source_label"] == "tiny"
        assert entry["profile"]["row_count"] == 3
