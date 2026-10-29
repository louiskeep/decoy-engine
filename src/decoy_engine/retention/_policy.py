"""``retention-policy/v1``: bounded per-class retention windows (F26).

A policy only states when an artifact *may* become eligible; it never authorizes deletion and a
change to it never purges as a side effect. Defaults are conservative: durable classes keep
everything; transient uploads and registered staging expire 24 hours after quiescence.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from decoy_engine.retention._canonical import canonical_json, sha256_hex
from decoy_engine.retention._vocab import _Closed

POLICY_SCHEMA = "retention-policy/v1"
MIN_WINDOW_HOURS = 1
MAX_WINDOW_HOURS = 24 * 3650


class PolicyKey(_Closed):
    """Policy lookup key. ``raw_upload`` is split because FRAME treats transient and saved
    uploads differently; the ledger records which one an upload is at registration."""

    RAW_UPLOAD_TRANSIENT = "raw_upload_transient"
    RAW_UPLOAD_SAVED = "raw_upload_saved"
    SNAPSHOT = "snapshot"
    PUBLISHED_OUTPUT = "published_output"
    STAGING = "staging"
    CHECKPOINT = "checkpoint"
    UNKNOWN = "unknown"


class PolicyMode(_Closed):
    KEEP_ALL = "keep_all"
    EXPIRE_AFTER_QUIESCENCE = "expire_after_quiescence"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ClassPolicy:
    mode: PolicyMode
    window_hours: int | None = None

    def __post_init__(self) -> None:
        if self.mode is PolicyMode.KEEP_ALL:
            if self.window_hours is not None:
                raise ValueError("keep_all takes no window")
        elif self.mode is PolicyMode.EXPIRE_AFTER_QUIESCENCE:
            h = self.window_hours
            if (
                isinstance(h, bool)
                or not isinstance(h, int)
                or not (MIN_WINDOW_HOURS <= h <= MAX_WINDOW_HOURS)
            ):
                raise ValueError(
                    f"window_hours must be an integer in [{MIN_WINDOW_HOURS}, {MAX_WINDOW_HOURS}]"
                )
        else:
            raise ValueError("unsupported policy mode")


_KEEP = ClassPolicy(PolicyMode.KEEP_ALL)
_DAY = ClassPolicy(PolicyMode.EXPIRE_AFTER_QUIESCENCE, 24)

DEFAULT_CLASSES: Mapping[PolicyKey, ClassPolicy] = {
    PolicyKey.RAW_UPLOAD_TRANSIENT: _DAY,
    PolicyKey.RAW_UPLOAD_SAVED: _KEEP,
    PolicyKey.SNAPSHOT: _KEEP,
    PolicyKey.PUBLISHED_OUTPUT: _KEEP,
    PolicyKey.STAGING: _DAY,
    PolicyKey.CHECKPOINT: _KEEP,
}


@dataclass(frozen=True)
class RetentionPolicy:
    classes: Mapping[PolicyKey, ClassPolicy]

    def __post_init__(self) -> None:
        for key, cp in self.classes.items():
            if not isinstance(key, PolicyKey) or key is PolicyKey.UNKNOWN:
                raise ValueError("unknown policy key")
            if not isinstance(cp, ClassPolicy):
                raise ValueError("class policy must be a ClassPolicy")
        missing = set(DEFAULT_CLASSES) - set(self.classes)
        if missing:
            raise ValueError("policy must state every class")

    def for_key(self, key: PolicyKey) -> ClassPolicy:
        return self.classes[key]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": POLICY_SCHEMA,
            "classes": {
                k.value: {"mode": cp.mode.value, "window_hours": cp.window_hours}
                for k, cp in sorted(self.classes.items(), key=lambda kv: kv[0].value)
            },
        }

    @property
    def policy_id(self) -> str:
        """Content-addressed id: the same classes always hash to the same id, so a run's policy
        snapshot cannot be swapped without changing the id the digest binds."""
        return sha256_hex(canonical_json(self.to_dict()))[:16]

    @classmethod
    def from_dict(cls, data: object) -> RetentionPolicy:
        """Strict parse: any malformed, unknown or missing entry raises ``ValueError``.

        Callers treat a raise as ``retain_policy_invalid``; a lenient parse would let a typo
        shorten a window."""
        if not isinstance(data, dict) or data.get("schema") != POLICY_SCHEMA:
            raise ValueError("unsupported policy schema")
        raw = data.get("classes")
        if not isinstance(raw, dict):
            raise ValueError("classes must be an object")
        out: dict[PolicyKey, ClassPolicy] = {}
        for name, body in raw.items():
            key = PolicyKey.parse(name)
            if key is PolicyKey.UNKNOWN or not isinstance(body, dict):
                raise ValueError("unknown policy key")
            if set(body) - {"mode", "window_hours"}:
                raise ValueError("unknown class policy field")
            mode = PolicyMode.parse(body.get("mode"))
            out[key] = ClassPolicy(mode, body.get("window_hours"))
        return cls(out)


DEFAULT_POLICY = RetentionPolicy(dict(DEFAULT_CLASSES))
