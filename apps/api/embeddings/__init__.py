"""Vector configuration and versioned embedding index support (ADR-015).

The package hosts the data plane introduced by M3-6b phase 1:

* :mod:`apps.api.embeddings.canary` defines the fixed probe texts
  used to detect vector-space drift between two configurations.
* :mod:`apps.api.embeddings.fingerprint` defines a deterministic,
  non-secret fingerprint over a configuration.
* :mod:`apps.api.embeddings.compatibility` provides cosine and the
  ``same / compatible / rebuild_required / unknown`` judgement that
  ADR-015 requires.
* :mod:`apps.api.embeddings.service` orchestrates the probe
  requests and writes the resulting :class:`EmbeddingProfile` row.
* :mod:`apps.api.api.embeddings` exposes the public
  ``/api/embeddings/test`` and ``/api/embeddings/status`` routes.

The package deliberately keeps no I/O of its own at the module
level: every function takes the database session / HTTP client it
needs as an argument. That makes the pure parts easy to unit-test
without spinning up FastAPI.
"""

from .canary import CANARY_TEXTS, CANARY_VERSION, get_canary_texts
from .compatibility import (
    CompatibilityDecision,
    cosine_similarity,
    judge_compatibility,
    validate_embedding_vector,
)
from .fingerprint import compute_config_fingerprint


__all__ = [
    "CANARY_TEXTS",
    "CANARY_VERSION",
    "CompatibilityDecision",
    "compute_config_fingerprint",
    "cosine_similarity",
    "get_canary_texts",
    "judge_compatibility",
    "validate_embedding_vector",
]
