"""Deterministic embedding selection for large structured documents."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, TypeVar


class OrderedChunk(Protocol):
    id: int | None
    order_index: int


ChunkT = TypeVar("ChunkT", bound=OrderedChunk)


def evenly_sample_chunks(chunks: Sequence[ChunkT], sample_size: int) -> list[ChunkT]:
    """Return an order-preserving sample which always includes both edges."""

    if sample_size <= 0 or not chunks:
        return []
    ordered = sorted(chunks, key=lambda chunk: (chunk.order_index, chunk.id or 0))
    if len(ordered) <= sample_size:
        return ordered
    if sample_size == 1:
        return [ordered[0]]
    last_index = len(ordered) - 1
    indexes = {
        round(position * last_index / (sample_size - 1))
        for position in range(sample_size)
    }
    return [ordered[index] for index in sorted(indexes)]
