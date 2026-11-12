"""Frames for the full-frame profile equivalence golden (see test_full_frame_speed_equivalence)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from tests.integration.test_scan_profiler import _hipaa_like_dataframe


def mixed_frame(rows: int = 50_000) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    idx = np.arange(rows)
    emails = np.array([f"user{i % 9000}@example.com" for i in range(rows)], dtype=object)
    emails[rng.random(rows) < 0.2] = "not an email"
    ssn = np.array(
        [f"{100 + i % 800:03d}-{10 + i % 80:02d}-{1000 + i % 8000:04d}" for i in idx], dtype=object
    )
    ssn[rng.random(rows) < 0.3] = np.array(["123456789"], dtype=object)[0]
    pan_good = "4111111111111111"
    pan = np.where(rng.random(rows) < 0.6, pan_good, "4111111111111112").astype(object)
    ip = np.array([f"10.0.{i % 256}.{(i * 7) % 300}" for i in idx], dtype=object)
    dates = np.array(
        [f"20{10 + i % 14}-{1 + i % 12:02d}-{1 + i % 28:02d}" for i in idx], dtype=object
    )
    dates[rng.random(rows) < 0.05] = "garbage"
    padded = np.array([f"  user{i % 50}@corp.org " for i in idx], dtype=object)
    free = np.array([f"note {i % 3000} about the visit" for i in idx], dtype=object)
    phone = np.array([f"({200 + i % 700}) 555-{i % 10000:04d}" for i in idx], dtype=object)
    zips = np.array(
        [f"{10000 + i % 80000:05d}" if i % 3 else f"{10000 + i % 80000:05d}-1234" for i in idx],
        dtype=object,
    )
    mixed_types = np.array([i if i % 2 else str(i) for i in idx], dtype=object)
    status = rng.choice(["active", "N/A", " Unknown ", "closed", "TBA"], rows)
    dob = np.array([f"19{50 + i % 50}-0{1 + i % 9}-1{i % 10}" for i in idx], dtype=object)
    dob[rng.random(rows) < 0.02] = "0001-01-01"
    dob[rng.random(rows) < 0.01] = "9999-12-31"
    score = rng.choice([-999, 0, 1, 2, 9999, 7], rows)
    nulls = np.where(rng.random(rows) < 0.4, None, "a@b.co").astype(object)  # type: ignore[call-overload]
    return pd.DataFrame(
        {
            "email": emails,
            "ssn": ssn,
            "credit_card": pan,
            "ip_address": ip,
            "created": dates,
            "padded_mail": padded,
            "notes": free,
            "phone": phone,
            "zip": zips,
            "mixed_types": mixed_types,
            "sparse_email": nulls,
            "status": status,
            "dob": dob,
            "score": score,
            "amount": rng.integers(0, 10_000, rows),
            "first_name": rng.choice(["Alice", "Bob", "Carol", "zed1"], rows),
        }
    )


def small_frames() -> dict[str, pd.DataFrame]:
    return {
        "hipaa": _hipaa_like_dataframe(),
        "empty_strings": pd.DataFrame({"a": ["", " ", "x"], "email": ["", "", ""]}),
        "all_null": pd.DataFrame({"email": [None, None, None]}),
        "one_row": pd.DataFrame({"email": ["a@b.co"], "d": ["2020-01-01"]}),
        "dup_heavy": pd.DataFrame({"ssn": ["123-45-6789"] * 40 + ["bad", "x", "y", "z"] * 3}),
    }
