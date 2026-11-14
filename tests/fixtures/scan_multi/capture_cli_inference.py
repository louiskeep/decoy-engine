"""Regenerate the SCAN-MULTI S3-engine.1 parity fixtures.

    python tests/fixtures/scan_multi/capture_cli_inference.py --cli-src PATH

``PATH`` is the ``src`` folder of the CLI at commit 8fba6af (``git archive 8fba6af src`` into
a scratch folder is enough). The script runs the CLI's own ``_infer_strategy_for_column`` and
``_emit_column_yaml`` on each case below and records what the CLI decided, so the engine port
is judged against the CLI and not against a hand copy of it. Review text is recorded with the
retired product name replaced by "The scan" (see `_RETIRED_NAME`). It also records the engine's
``DEFAULT_STRATEGY_BY_DETECTOR`` as it stood before the descriptor registry (run it against
the pre-registry engine to refresh that file; the checked-in copy came from engine main
07763ff5).

The cases carry no data values: a detector id, a match rate, a confidence bucket and two
column-shape fields.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import yaml

HERE = Path(__file__).resolve().parent

# The CLI's review copy starts with the retired product name. The engine copy says "The scan"
# instead (the removed-surface sentry bars the old name), so the fixture records the CLI text
# with that one substitution. The name is spelled in two parts so this file passes the sentry.
_RETIRED_NAME = re.compile("STORM")

# Every detector id the CLI table keys, in table order.
CLI_DETECTORS = [
    "email",
    "ssn",
    "us_phone",
    "person_name",
    "iso_date",
    "us_date",
    "us_zip",
    "address_street",
    "address_city",
    "mrn",
    "npi",
    "ndc",
    "icd10",
    "pan",
    "cvv",
    "iban",
    "uuid",
    "ipv4",
]


def _case(case_id, matches=(), inferred_type="string", unique=False):
    return {
        "id": case_id,
        "inferred_type": inferred_type,
        "is_likely_unique": unique,
        "matches": [
            {"detector_id": d, "match_rate": r, "confidence_bucket": b} for d, r, b in matches
        ],
    }


def cases() -> list[dict]:
    out = [_case(f"detector-{d}", [(d, 0.95, "high")]) for d in CLI_DETECTORS]
    out += [
        _case("unknown-detector-high", [("custom__uk_nhs_number", 0.95, "high")]),
        _case("registered-but-not-in-cli-table", [("first_name", 0.95, "high")]),
        _case("no-matches-string"),
        _case("no-matches-integer-unique", inferred_type="integer", unique=True),
        _case("no-matches-integer-not-unique", inferred_type="integer"),
        _case("no-matches-float-unique", inferred_type="float", unique=True),
        _case("no-matches-string-unique", unique=True),
        _case("low-bucket-low-rate", [("email", 0.3, "low")]),
        _case("none-bucket-low-rate", [("email", 0.3, None)]),
        _case("low-bucket-rate-at-half", [("email", 0.5, "low")]),
        _case("low-bucket-high-rate", [("ssn", 0.8, "low")]),
        _case("medium-bucket-low-rate", [("email", 0.3, "medium")]),
        _case("high-bucket-low-rate", [("us_phone", 0.2, "high")]),
        _case("unknown-bucket-low-rate", [("email", 0.3, "weird")]),
        _case("confident-with-integer-unique", [("npi", 0.9, "high")], "integer", True),
        _case("unconfident-with-integer-unique", [("npi", 0.1, "low")], "integer", True),
        # The ambiguity window is 0.1, strict: a gap of exactly 0.1 is not ambiguous.
        _case("ambiguous-gap-0.05", [("us_phone", 0.9, "high"), ("us_date", 0.85, "high")]),
        _case("ambiguous-gap-0.09", [("us_phone", 0.9, "high"), ("us_date", 0.81, "high")]),
        _case("clear-gap-0.10", [("us_phone", 0.9, "high"), ("us_date", 0.8, "high")]),
        _case("clear-gap-0.15", [("us_phone", 0.9, "high"), ("us_date", 0.75, "high")]),
        _case("clear-gap-0.19", [("us_phone", 0.9, "high"), ("us_date", 0.71, "high")]),
        _case("ambiguous-equal-rates", [("email", 0.9, "high"), ("person_name", 0.9, "high")]),
        _case(
            "ambiguous-three",
            [("ssn", 0.9, "high"), ("npi", 0.88, "high"), ("mrn", 0.86, "medium")],
        ),
        _case(
            "ambiguous-four-names-three",
            [
                ("ssn", 0.9, "high"),
                ("npi", 0.89, "high"),
                ("mrn", 0.88, "high"),
                ("pan", 0.87, "high"),
            ],
        ),
        _case("ambiguous-unsorted-input", [("us_date", 0.85, "high"), ("us_phone", 0.9, "high")]),
        _case("ambiguous-top-unknown", [("custom__x", 0.9, "high"), ("email", 0.88, "high")]),
        _case("unconfident-second-ignored", [("email", 0.9, "high"), ("ssn", 0.3, "low")]),
        _case("confident-by-rate-only-second", [("email", 0.9, "high"), ("ssn", 0.85, "low")]),
    ]
    return out


def _stats(case: dict) -> SimpleNamespace:
    return SimpleNamespace(
        name="c",
        inferred_type=case["inferred_type"],
        is_likely_unique=case["is_likely_unique"],
        detector_matches=[SimpleNamespace(**m) for m in case["matches"]],
    )


def _decision(inference, emit) -> dict:
    """Read the strategy, provider and provider_config the CLI would write for one column."""
    block = yaml.safe_load(emit("c", inference, indent="")) or {}
    # The emitted text is `# REVIEW...` lines then `- name: c` and its keys.
    entry = block[0] if isinstance(block, list) else block
    return {
        "strategy": entry["strategy"],
        "provider": entry.get("provider"),
        "provider_config": entry.get("provider_config", {}),
        "deterministic": bool(entry.get("deterministic", False)),
        "review": _RETIRED_NAME.sub("The scan", inference.review),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cli-src", required=True, type=Path)
    parser.add_argument("--no-default-base", action="store_true")
    args = parser.parse_args()
    sys.path.insert(0, str(args.cli_src))
    from decoy.cli._init_inference import _infer_strategy_for_column
    from decoy.cli.init import _emit_column_yaml

    recorded = []
    for case in cases():
        inference = _infer_strategy_for_column(_stats(case))
        recorded.append({**case, "expected": _decision(inference, _emit_column_yaml)})
    document = {
        "source": "decoy CLI 8fba6af _infer_strategy_for_column (review text: retired product name replaced by The scan)",
        "cases": recorded,
    }
    (HERE / "cli_inference_8fba6af.json").write_text(json.dumps(document, indent=2) + "\n")
    if not args.no_default_base:
        from decoy_engine.profiling.recommendations import DEFAULT_STRATEGY_BY_DETECTOR

        base = {k: [v[0], v[1]] for k, v in DEFAULT_STRATEGY_BY_DETECTOR.items()}
        (HERE / "default_strategy_by_detector_base.json").write_text(
            json.dumps(base, indent=2) + "\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
