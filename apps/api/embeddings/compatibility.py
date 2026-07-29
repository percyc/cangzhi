"""Vector validation, cosine similarity and the compatibility judgement.

This module is the brain of ADR-015 phase 1. It takes the raw
vectors that the embedding provider returned for our three
versioned canary texts, compares them to the canary vectors stored
on the active profile (if any), and returns one of four
:enum:`CompatibilityDecision` values:

* ``same`` — the candidate's configuration fingerprint matches the
  active profile's, so we know without any cosine work that the
  candidate is in the same vector space. No rebuild required.
* ``compatible`` — the candidate is *not* the same configuration
  (the model, the URL, the key, or some combination) but its
  canary vectors are within ``COSINE_THRESHOLD`` of the active
  profile's. The operator is allowed to skip the rebuild in this
  case, but must do so explicitly.
* ``rebuild_required`` — the canary vectors disagree (cosine below
  threshold), the model or dimensionality changed, or the probe
  produced an unusable result. The active profile keeps serving
  until a new profile is fully built and activated.
* ``unknown`` — there is no active profile yet, so the test cannot
  make a relative comparison. The first profile is built from
  scratch; the verdict is "unknown" rather than "rebuild_required"
  so the UI can render a different hint.

The functions in this module are all pure. They never touch the
network, the database, or the filesystem. The service layer
(``service.py``) is the only place that performs real I/O.
"""

from __future__ import annotations

import math
from enum import Enum
from typing import Sequence


# Cosine threshold above which two canary vectors are considered
# "the same vector space". ADR-015 picked ``0.999``; we keep the
# value as a module constant so it can be tuned by tests without
# touching call sites.
COSINE_THRESHOLD: float = 0.999

# Number of canary vectors we expect to compare. The probe must
# always return this many vectors; anything less is a hard error.
EXPECTED_CANARY_COUNT: int = 3


class CompatibilityDecision(str, Enum):
    """Possible outcomes of the compatibility judgement.

    Inheriting from ``str`` lets the enum be serialised directly
    into JSON without an explicit ``.value`` lookup.
    """

    SAME = "same"
    COMPATIBLE = "compatible"
    REBUILD_REQUIRED = "rebuild_required"
    UNKNOWN = "unknown"


def validate_embedding_vector(value, *, dim: int | None = None) -> int:
    """Validate a single embedding vector and return its dimensionality.

    The function enforces ADR-015's "non-empty, finite, consistent"
    contract:

    * the value must be a non-empty list (or tuple) of numbers;
    * every element must be a real number (no booleans, no
      ``None``, no strings);
    * floats must be finite (no NaN, no ``+inf`` / ``-inf``);
    * if ``dim`` is supplied, the vector's length must match.

    Raises
    ------
    ValueError
        If the vector is empty, malformed, or has the wrong size.
    """

    if not isinstance(value, (list, tuple)):
        raise ValueError("返回的向量不是数组")
    if not value:
        raise ValueError("返回的向量为空")
    for index, item in enumerate(value):
        # ``bool`` is a subclass of ``int`` in Python; we reject it
        # explicitly because True/False are not real numbers in
        # this context.
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ValueError(f"向量第 {index} 项不是数值")
        if isinstance(item, float):
            if math.isnan(item) or math.isinf(item):
                raise ValueError(f"向量第 {index} 项包含非法数值（NaN/Inf）")
    if dim is not None and len(value) != dim:
        raise ValueError(f"向量维度 {len(value)} 与期望维度 {dim} 不一致")
    return len(value)


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Return the cosine similarity of two equally-sized vectors.

    Both inputs are expected to be validated by
    :func:`validate_embedding_vector` already. The function still
    re-validates its inputs in a single fast path so callers from
    outside the package cannot accidentally trigger a divide-by-zero
    on a zero vector.

    Raises
    ------
    ValueError
        If the inputs are not equally-sized numeric sequences.
    """

    if not isinstance(a, (list, tuple)) or not isinstance(b, (list, tuple)):
        raise ValueError("cosine 输入必须是数组")
    if len(a) != len(b):
        raise ValueError("cosine 输入维度不一致")
    if not a:
        raise ValueError("cosine 输入不能为空")
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        # Coerce through ``float`` so the integer path matches the
        # float path exactly; without this the cross-type
        # multiplication can produce ``int`` and then the result is
        # off by a tiny amount when summed.
        fx = float(x)
        fy = float(y)
        dot += fx * fy
        norm_a += fx * fx
        norm_b += fy * fy
    if norm_a <= 0.0 or norm_b <= 0.0:
        # A zero vector has no defined direction. Treat the
        # similarity as 0 (maximally different) so the caller
        # always gets a value in ``[-1, 1]``.
        return 0.0
    return dot / math.sqrt(norm_a * norm_b)


def _canary_metrics(
    candidate_canary: Sequence[Sequence[float]],
    active_canary: Sequence[Sequence[float]],
    threshold: float,
) -> tuple[list[float], float, bool]:
    """Compute per-cosine scores and an overall "all above threshold" flag.

    The threshold is taken as a parameter (not from the module
    constant) so tests can tighten or relax it without monkey
    patching globals.
    """

    scores: list[float] = []
    for c_vector, a_vector in zip(candidate_canary, active_canary):
        scores.append(cosine_similarity(c_vector, a_vector))
    all_above = all(score >= threshold for score in scores)
    return scores, (sum(scores) / len(scores) if scores else 0.0), all_above


def judge_compatibility(
    *,
    candidate_dim: int,
    candidate_model: str,
    candidate_fingerprint: str,
    candidate_canary_vectors: Sequence[Sequence[float]],
    active_dim: int | None,
    active_model: str | None,
    active_fingerprint: str | None,
    active_canary_vectors: Sequence[Sequence[float]] | None,
    threshold: float = COSINE_THRESHOLD,
) -> tuple[CompatibilityDecision, str, dict]:
    """Compare a candidate embedding configuration against the active one.

    The decision is purely a function of the inputs. No database
    lookups, no network calls, no logging. The third tuple element
    is a free-form diagnostics dict that the API layer can return
    to the operator; it never contains the canary vectors
    themselves (only their per-cosine scores).

    Parameters
    ----------
    candidate_dim, candidate_model, candidate_fingerprint, candidate_canary_vectors:
        The candidate's observed dimensionality, model name,
        configuration fingerprint and canary vectors. ``candidate_dim``
        is the dimensionality the embedding endpoint returned for
        the first canary text; the function assumes all three
        vectors are equally sized.
    active_dim, active_model, active_fingerprint, active_canary_vectors:
        The active profile's values, or ``None`` if no profile is
        active yet. ``active_canary_vectors`` is a list of three
        equally-sized vectors.
    threshold:
        Cosine threshold used for the ``compatible`` judgement.
        Tests pass a tighter value to exercise the boundary.
    """

    if candidate_dim <= 0:
        return (
            CompatibilityDecision.REBUILD_REQUIRED,
            "候选配置维度无效",
            {"scores": [], "average": 0.0, "threshold": threshold},
        )
    if len(candidate_canary_vectors) < EXPECTED_CANARY_COUNT:
        return (
            CompatibilityDecision.REBUILD_REQUIRED,
            f"候选探针数量不足（{len(candidate_canary_vectors)}/{EXPECTED_CANARY_COUNT}）",
            {"scores": [], "average": 0.0, "threshold": threshold},
        )

    # No active profile → the operator's first configuration.
    # We split "no active profile" from "active profile but with no
    # canary data": the former is ``unknown`` (first build), the
    # latter is ``rebuild_required`` (we cannot prove compatibility
    # so the operator must rebuild).
    if (
        active_dim is None
        or active_model is None
        or active_fingerprint is None
    ):
        return (
            CompatibilityDecision.UNKNOWN,
            "当前没有激活的向量索引",
            {"scores": [], "average": 0.0, "threshold": threshold},
        )

    if candidate_dim != active_dim:
        return (
            CompatibilityDecision.REBUILD_REQUIRED,
            (
                f"维度变化（候选 {candidate_dim}，当前 {active_dim}），"
                "向量空间不同"
            ),
            {"scores": [], "average": 0.0, "threshold": threshold},
        )

    if (candidate_model or "").strip() != (active_model or "").strip():
        return (
            CompatibilityDecision.REBUILD_REQUIRED,
            "模型名变化，向量空间不同",
            {"scores": [], "average": 0.0, "threshold": threshold},
        )

    if not active_canary_vectors or len(active_canary_vectors) < EXPECTED_CANARY_COUNT:
        return (
            CompatibilityDecision.REBUILD_REQUIRED,
            (
                f"当前激活 profile 探针数量不足"
                f"（{len(active_canary_vectors or [])}/{EXPECTED_CANARY_COUNT}），"
                "无法判断兼容性"
            ),
            {"scores": [], "average": 0.0, "threshold": threshold},
        )

    # All gates above passed: this is the "only URL / key / model
    # alias changed" path. The cosine is what makes the final
    # call.
    scores, average, all_above = _canary_metrics(
        candidate_canary_vectors, active_canary_vectors, threshold
    )
    if all_above:
        if candidate_fingerprint == active_fingerprint:
            return (
                CompatibilityDecision.SAME,
                "配置指纹相同且探针结果稳定，无需重建",
                {
                    "scores": scores,
                    "average": average,
                    "threshold": threshold,
                },
            )
        return (
            CompatibilityDecision.COMPATIBLE,
            (
                f"探针相似度均不低于 {threshold}，"
                "与当前索引高度兼容，可选择免重建"
            ),
            {
                "scores": scores,
                "average": average,
                "threshold": threshold,
            },
        )
    return (
        CompatibilityDecision.REBUILD_REQUIRED,
        (
            f"探针相似度低于阈值 {threshold}，"
            "向量空间可能漂移，必须重建"
        ),
        {
            "scores": scores,
            "average": average,
            "threshold": threshold,
        },
    )
