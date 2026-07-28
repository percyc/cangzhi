"""Prompt version registry.

Internal prompt versioning. Only v1 is supported currently.
Unknown versions safely fall back to v1.
"""
from __future__ import annotations

SUPPORTED_VERSIONS = {"v1"}
DEFAULT_VERSION = "v1"


def get_allowed_version(requested: str | None) -> str:
    """Return a supported prompt version, default to v1 if unknown."""
    if not requested:
        return DEFAULT_VERSION
    cleaned = requested.strip().lower()
    if cleaned in SUPPORTED_VERSIONS:
        return cleaned
    return DEFAULT_VERSION
