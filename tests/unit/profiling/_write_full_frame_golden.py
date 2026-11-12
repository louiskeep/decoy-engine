"""Regenerate tests/fixtures/profile_full_frame/profiles.json (run on a trusted baseline).

python -m tests.unit.profiling._write_full_frame_golden
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.unit.profiling._full_frame_frames import mixed_frame, small_frames
from tests.unit.profiling.test_full_frame_speed_equivalence import GOLDEN, dump_profile


def main() -> None:
    frames = {**small_frames(), "mixed_50k": mixed_frame()}
    out = {k: dump_profile(v) for k, v in frames.items()}
    Path(GOLDEN).parent.mkdir(parents=True, exist_ok=True)
    Path(GOLDEN).write_text(json.dumps(out, indent=1, sort_keys=True, default=str) + "\n")


if __name__ == "__main__":
    main()
