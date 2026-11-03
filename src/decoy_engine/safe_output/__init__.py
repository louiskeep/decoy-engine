"""Safe output (F31): closed, validated records that carry no exception text or cell value.

``SafeEvent`` holds only exact, bounded fields (a registry reason, a closed exception label, an
approved step name, closed counts and flags); ``safe_error_type`` labels an exception by exact
type. The wire functions arrive with the line serializer.
"""

from __future__ import annotations

from decoy_engine.safe_output._event import EventKind, Flag, SafeEvent, StepRef
from decoy_engine.safe_output._exception import SafeErrorType, safe_error_type

__all__ = ["EventKind", "Flag", "SafeErrorType", "SafeEvent", "StepRef", "safe_error_type"]
