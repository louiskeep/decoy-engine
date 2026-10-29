"""Mutation check for the F26 retention contract (decoy_engine.retention).

Each mutant changes one decision in a COPY of the source tree (the working tree is never touched)
and runs the contract tests, which must fail. A surviving mutant means a guarantee of the shared
contract is not pinned by a test. Run from the repo root:  python scripts/retention_mutation_check.py [filter]
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ("tests/unit/retention", "tests/sentry/test_retention_contract.py")
PKG = "src/decoy_engine/retention"


@dataclass(frozen=True)
class M:
    name: str
    file: str
    old: str
    new: str


# fmt: off
MUTANTS = [
    # ---- confirmation digest binds everything it claims to ---------------------------------
    M("digest: scope not bound", "_report.py", '"scope": sorted(c.value for c in scope),\n        "limits"', '"limits"'),
    M("digest: policy id not bound", "_report.py", '"policy_id": check_policy_id(policy_id),', ""),
    M("digest: limits not bound", "_report.py", '"limits": limits.to_dict(),\n        "expires_at"', '"expires_at"'),
    M("digest: run expiry not bound", "_report.py", '"expires_at": check_count("expires_at", expires_at),', ""),
    M("digest: target ids not bound", "_report.py", '"eligible": sorted([aid, ident] for aid, ident in eligible),', '"eligible": [],'),
    M("digest: identity digests not bound", "_report.py", '"eligible": sorted([aid, ident] for aid, ident in eligible),', '"eligible": sorted([aid] for aid, ident in eligible),'),
    M("digest: order dependent", "_report.py", '"eligible": sorted([aid, ident] for aid, ident in eligible),', '"eligible": [[aid, ident] for aid, ident in eligible],'),
    M("digest: no domain separation", "_report.py", '"domain": DIGEST_DOMAIN,', ""),
    # ---- outcome derivation is the one shared mapping -------------------------------------
    M("outcome: refusal ignored", "_report.py", "    if refusal is not None:\n        return RunOutcome.REFUSED", "    if False:\n        return RunOutcome.REFUSED"),
    M("outcome: incomplete preview is success", "_report.py", "        if not complete:\n            return RunOutcome.PARTIAL", "        if False:\n            return RunOutcome.PARTIAL"),
    M("outcome: empty preview is not a no-op", "_report.py", "        if counts.total == 0:\n            return RunOutcome.NOOP", "        if False:\n            return RunOutcome.NOOP"),
    M("outcome: all-retained preview reads as success", "_report.py", "return RunOutcome.SUCCESS if counts.eligible else RunOutcome.RETAINED", "return RunOutcome.SUCCESS"),
    M("outcome: partial delete reads as success", "_report.py", "    if counts.deleted == counts.approved:\n        return RunOutcome.SUCCESS", "    if counts.deleted > 0:\n        return RunOutcome.SUCCESS"),
    M("outcome: failures with no deletes read as retained", "_report.py", "    if counts.deleted == 0 and counts.failed == 0:\n        return RunOutcome.RETAINED", "    if counts.deleted == 0:\n        return RunOutcome.RETAINED"),
    # ---- exit codes ------------------------------------------------------------------------
    M("exit: unknown outcome maps to success", "_vocab.py", "return _OUTCOME_EXIT.get(outcome, ExitCode.INDETERMINATE)", "return _OUTCOME_EXIT.get(outcome, ExitCode.SUCCESS)"),
    M("exit: partial collides with retained", "_vocab.py", "PARTIAL = 12", "PARTIAL = 11"),
    # ---- unknown future values can only retain --------------------------------------------
    M("reason: unknown parses to eligible", "_vocab.py", "        return cls.RETAIN_UNKNOWN\n", "        return cls.ELIGIBLE\n"),
    M("report item: contradictory decision stays eligible", "_report.py", "            decision, reason = (\n                Decision.RETAINED,", "            decision, reason = (\n                decision,"),
    M("report item: decision and reason may disagree", "_report.py", "        if (self.decision is Decision.ELIGIBLE) != (self.reason is ReasonCode.ELIGIBLE):\n            raise ValueError(\"decision and reason disagree\")", "        pass"),
    # ---- value-free serialization ---------------------------------------------------------
    M("report: unknown fields accepted on parse", "_report.py", "        if set(data) - ALLOWED_REPORT_KEYS:\n            raise ValueError(\"unknown report field\")", "        pass"),
    M("item: unknown fields accepted on parse", "_report.py", "        if set(data) - ALLOWED_ITEM_KEYS:\n            raise ValueError(\"unknown item field\")", "        pass"),
    M("opaque id: any string accepted", "_canonical.py", 'return isinstance(value, str) and _OPAQUE_ID.fullmatch(value) is not None', "return isinstance(value, str)"),
    M("error type: free text accepted", "_canonical.py", 'if not isinstance(value, str) or _ERROR_TYPE.fullmatch(value) is None:', "if not isinstance(value, str):"),
    M("canonical json: key order not fixed", "_canonical.py", "sort_keys=True, separators", "sort_keys=False, separators"),
    # ---- policy strictness ------------------------------------------------------------------
    M("policy: window upper bound removed", "_policy.py", "MIN_WINDOW_HOURS <= h <= MAX_WINDOW_HOURS", "MIN_WINDOW_HOURS <= h"),
    M("policy: window lower bound removed", "_policy.py", "MIN_WINDOW_HOURS <= h <= MAX_WINDOW_HOURS", "h <= MAX_WINDOW_HOURS"),
    M("policy: bool accepted as a window", "_policy.py", "                isinstance(h, bool)\n                or not isinstance(h, int)", "                not isinstance(h, int)"),
    M("policy: keep_all may carry a window", "_policy.py", "            if self.window_hours is not None:\n                raise ValueError(\"keep_all takes no window\")", "            pass"),
    M("policy: missing class tolerated", "_policy.py", "        if missing:\n            raise ValueError(\"policy must state every class\")", "        pass"),
    M("policy: unknown mode tolerated", "_policy.py", "        else:\n            raise ValueError(\"unsupported policy mode\")", "        else:\n            pass"),
    M("policy: extra class field tolerated", "_policy.py", "            if set(body) - {\"mode\", \"window_hours\"}:\n                raise ValueError(\"unknown class policy field\")", "            pass"),
    M("policy: schema version not checked", "_policy.py", "if not isinstance(data, dict) or data.get(\"schema\") != POLICY_SCHEMA:", "if not isinstance(data, dict):"),
    M("policy: defaults expire saved uploads", "_policy.py", "    PolicyKey.RAW_UPLOAD_SAVED: _KEEP,", "    PolicyKey.RAW_UPLOAD_SAVED: _DAY,"),
    M("policy: defaults expire outputs", "_policy.py", "    PolicyKey.PUBLISHED_OUTPUT: _KEEP,", "    PolicyKey.PUBLISHED_OUTPUT: _DAY,"),
    M("policy: id not content-addressed", "_policy.py", "        return sha256_hex(canonical_json(self.to_dict()))[:16]", "        return \"0\" * 16"),
]
# fmt: on


def run(tree: Path) -> bool:
    """True when the tests PASSED against ``tree`` (the mutant survived)."""
    proc = subprocess.run(  # noqa: S603 - fixed interpreter and pytest args
        [
            sys.executable,
            "-m",
            "pytest",
            *TESTS,
            "-x",
            "-q",
            "-p",
            "no:cacheprovider",
            "--no-header",
            # the repo's pytest config puts ./src first; point it at the mutated COPY instead
            "-o",
            f"pythonpath={tree}",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )
    return proc.returncode == 0


def main(argv: list[str]) -> int:
    flt = argv[0] if argv else ""
    todo = [m for m in MUTANTS if flt in m.name]
    survived: list[str] = []
    bad: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        tree = Path(tmp) / "src"
        shutil.copytree(ROOT / "src", tree)
        for i, m in enumerate(todo, 1):
            path = tree / "decoy_engine" / "retention" / m.file
            original = path.read_text(encoding="utf-8")
            if original.count(m.old) != 1:
                bad.append(f"{m.name}: pattern matched {original.count(m.old)} times")
                print(f"[{i}/{len(todo)}] BAD PATTERN  {m.name}")
                continue
            path.write_text(original.replace(m.old, m.new), encoding="utf-8")
            try:
                alive = run(tree)
            finally:
                path.write_text(original, encoding="utf-8")
            print(f"[{i}/{len(todo)}] {'SURVIVED' if alive else 'killed  '}  {m.name}")
            if alive:
                survived.append(m.name)
    print(
        f"\n{len(todo) - len(survived) - len(bad)} killed, {len(survived)} survived, {len(bad)} bad patterns"
    )
    for s in survived + bad:
        print("  -", s)
    return 1 if survived or bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
