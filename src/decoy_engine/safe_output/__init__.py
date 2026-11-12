"""Safe output (F31): closed, validated records that carry no exception text or cell value.

``SafeEvent`` holds only exact, bounded fields (a registry reason, a closed exception label, an
approved step name, closed counts and flags); ``safe_error_type`` labels an exception by exact
type; ``validation_problems`` turns a Pydantic ``ValidationError`` into value-free
``ConfigProblem`` records. The wire functions arrive with the line serializer.
"""

from __future__ import annotations

from decoy_engine.safe_output._event import EventKind, Flag, SafeEvent, StepRef
from decoy_engine.safe_output._exception import SafeErrorType, safe_error_type
from decoy_engine.safe_output._validation import ConfigProblem, validation_problems, yaml_error_text

__all__ = [
    "ConfigProblem",
    "EventKind",
    "Flag",
    "SafeErrorType",
    "SafeEvent",
    "StepRef",
    "safe_error_type",
    "validation_problems",
    "yaml_error_text",
]
