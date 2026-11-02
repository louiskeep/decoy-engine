"""Filesystem + path + formatting helpers used internally (V2.0-C).

Split out of the bundled internal/helpers.py. All functions are thin
wrappers around stdlib; none touches the file system.
"""

from __future__ import annotations

import os


def convert_quoting_mode(quoting_mode: str) -> int:
    """Convert a quoting mode string to the corresponding csv module constant."""
    quoting_map = {
        "minimal": 0,  # csv.QUOTE_MINIMAL
        "all": 1,  # csv.QUOTE_ALL
        "nonnumeric": 2,  # csv.QUOTE_NONNUMERIC
        "none": 3,  # csv.QUOTE_NONE
    }
    return quoting_map.get(quoting_mode.lower(), 0)


def get_filename_without_extension(file_path: str) -> str:
    """Filename component of `file_path` with its extension stripped."""
    base_name = os.path.basename(file_path)
    return os.path.splitext(base_name)[0]


def convert_file_size(size_bytes: int) -> str:
    """Human-readable file size (B / KB / MB / GB / TB) for byte counts."""
    units = ["B", "KB", "MB", "GB", "TB"]
    if size_bytes == 0:
        return "0 B"
    size: float = float(size_bytes)
    i = 0
    while size >= 1024 and i < len(units) - 1:
        size /= 1024
        i += 1
    if i == 0:
        return f"{size:.0f} {units[i]}"
    return f"{size:.2f} {units[i]}"


def format_elapsed_time(seconds: float) -> str:
    """Format elapsed time in seconds to a human-readable string."""
    if seconds < 60:
        return f"{seconds:.1f} seconds"
    if seconds < 3600:
        minutes = seconds / 60
        return f"{minutes:.1f} minutes"
    hours = seconds / 3600
    return f"{hours:.1f} hours"
