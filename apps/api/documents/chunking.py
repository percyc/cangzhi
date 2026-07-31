"""Document-profile-specific child chunk sizes."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .detection import DocumentProfile

CHUNK_PROFILE_VERSION = "chunk-profile:v4"


@dataclass(frozen=True)
class ChunkingConfig:
    child_target_max_chars: int
    child_hard_max_chars: int
    child_target_min_chars: int
    child_overlap_chars: int
    profile_version: str = CHUNK_PROFILE_VERSION

    def to_dict(self) -> dict:
        return asdict(self)


def get_chunking_config(profile: DocumentProfile) -> ChunkingConfig:
    """Code and table documents keep larger retrieval units; others use defaults."""

    if profile.detected_type == "table":
        return ChunkingConfig(
            child_target_max_chars=1800,
            child_hard_max_chars=2400,
            child_target_min_chars=100,
            child_overlap_chars=0,
        )
    if profile.detected_type == "code":
        return ChunkingConfig(
            child_target_max_chars=1200,
            child_hard_max_chars=1800,
            child_target_min_chars=100,
            child_overlap_chars=200,
        )
    return ChunkingConfig(
        child_target_max_chars=600,
        child_hard_max_chars=900,
        child_target_min_chars=80,
        child_overlap_chars=120,
    )
