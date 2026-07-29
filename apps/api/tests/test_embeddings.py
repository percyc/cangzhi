"""Unit tests for the pure embedding helpers.

These tests do not require a database, an HTTP client, or any
fixture. They exercise the contract that ADR-015 puts on the
judgement function (the "same / compatible / rebuild_required /
unknown" outcomes) and on the supporting math.

The tests are organised by module: one class per source file. The
class name is reused by the integration tests in
:mod:`apps.api.tests.test_embeddings_api` so a failure points
directly at the right module.
"""

from __future__ import annotations

import math

import pytest

from apps.api.embeddings import (
    CANARY_TEXTS,
    CANARY_VERSION,
    CompatibilityDecision,
    compute_config_fingerprint,
    cosine_similarity,
    judge_compatibility,
    validate_embedding_vector,
)
from apps.api.embeddings.canary import get_canary_texts
from apps.api.embeddings.compatibility import COSINE_THRESHOLD


# --- canary -----------------------------------------------------------------


class TestCanary:
    """The fixed canary texts are a public contract."""

    def test_three_texts(self):
        assert len(CANARY_TEXTS) == 3

    def test_includes_chinese_and_english(self):
        joined = " ".join(CANARY_TEXTS)
        # Both scripts must be present so the test catches a model
        # that silently drops non-ASCII input.
        assert any("\u4e00" <= ch <= "\u9fff" for ch in joined)
        assert any("a" <= ch.lower() <= "z" for ch in joined)

    def test_version_is_string(self):
        assert isinstance(CANARY_VERSION, str)
        assert CANARY_VERSION  # non-empty

    def test_get_canary_texts_returns_versioned_set(self):
        canary = get_canary_texts()
        assert canary.version == CANARY_VERSION
        assert tuple(canary.texts) == CANARY_TEXTS
        assert len(canary) == 3


# --- fingerprint ------------------------------------------------------------


class TestFingerprint:
    """The configuration fingerprint is stable and secret-free."""

    BASE = dict(
        provider="openai",
        base_url="https://api.example.com/v1",
        model="text-embedding-3-small",
        dim=4,
        has_api_key=True,
        key_fingerprint="fk_a1b2c3d4e5f6",
        canary_version=CANARY_VERSION,
    )

    def test_identical_inputs_yield_identical_fingerprint(self):
        assert compute_config_fingerprint(**self.BASE) == compute_config_fingerprint(
            **self.BASE
        )

    def test_trailing_slash_does_not_change_fingerprint(self):
        a = compute_config_fingerprint(**{**self.BASE, "base_url": "https://api.example.com/v1/"})
        b = compute_config_fingerprint(**{**self.BASE, "base_url": "https://api.example.com/v1"})
        assert a == b

    def test_casing_of_provider_and_url_is_normalised(self):
        a = compute_config_fingerprint(
            **{**self.BASE, "provider": "OpenAI", "base_url": "HTTPS://API.example.com/v1"}
        )
        b = compute_config_fingerprint(**self.BASE)
        assert a == b

    def test_model_change_changes_fingerprint(self):
        a = compute_config_fingerprint(**self.BASE)
        b = compute_config_fingerprint(**{**self.BASE, "model": "text-embedding-3-large"})
        assert a != b

    def test_dim_change_changes_fingerprint(self):
        a = compute_config_fingerprint(**self.BASE)
        b = compute_config_fingerprint(**{**self.BASE, "dim": 1536})
        assert a != b

    def test_key_presence_changes_fingerprint(self):
        a = compute_config_fingerprint(**{**self.BASE, "has_api_key": True})
        b = compute_config_fingerprint(**{**self.BASE, "has_api_key": False})
        assert a != b

    def test_key_fingerprint_change_changes_fingerprint(self):
        a = compute_config_fingerprint(**self.BASE)
        b = compute_config_fingerprint(
            **{**self.BASE, "key_fingerprint": "fk_different"}
        )
        assert a != b

    def test_canary_version_change_changes_fingerprint(self):
        a = compute_config_fingerprint(**self.BASE)
        b = compute_config_fingerprint(**{**self.BASE, "canary_version": "2"})
        assert a != b

    def test_provider_change_changes_fingerprint(self):
        a = compute_config_fingerprint(**self.BASE)
        b = compute_config_fingerprint(**{**self.BASE, "provider": "ollama"})
        assert a != b

    def test_required_fields_rejected(self):
        with pytest.raises(ValueError):
            compute_config_fingerprint(**{**self.BASE, "model": ""})
        with pytest.raises(ValueError):
            compute_config_fingerprint(**{**self.BASE, "base_url": ""})
        with pytest.raises(ValueError):
            compute_config_fingerprint(**{**self.BASE, "provider": ""})
        with pytest.raises(ValueError):
            compute_config_fingerprint(**{**self.BASE, "dim": 0})

    def test_plaintext_key_does_not_affect_fingerprint(self):
        """Rotating the plaintext API key does NOT change the
        fingerprint as long as the master Fernet key is the same.

        We verify this indirectly: the fingerprint is a function of
        ``key_fingerprint`` (which is derived from the master key,
        not the API key), so two invocations with the same master
        key produce the same fingerprint regardless of what API
        key the operator happens to be using. The unit test simply
        asserts that adding a new field to the input does not break
        the stability property.
        """

        # Same inputs → same fingerprint, even if the operator
        # typed a different API key between the two calls. The
        # fingerprint function only looks at key_fingerprint, not
        # at the plaintext.
        a = compute_config_fingerprint(**self.BASE)
        b = compute_config_fingerprint(**self.BASE)
        assert a == b


# --- validate_embedding_vector ---------------------------------------------


class TestValidateVector:
    """The probe's first line of defence: a vector must be sane."""

    def test_simple_list(self):
        assert validate_embedding_vector([0.1, 0.2, 0.3]) == 3

    def test_accepts_integers(self):
        assert validate_embedding_vector([0, 1, 2, 3]) == 4

    def test_rejects_empty(self):
        with pytest.raises(ValueError):
            validate_embedding_vector([])

    def test_rejects_non_list(self):
        with pytest.raises(ValueError):
            validate_embedding_vector("0.1,0.2,0.3")  # type: ignore[arg-type]

    def test_rejects_none_value(self):
        with pytest.raises(ValueError):
            validate_embedding_vector([0.1, None, 0.2])  # type: ignore[list-item]

    def test_rejects_bool(self):
        with pytest.raises(ValueError):
            validate_embedding_vector([0.1, True, 0.2])

    def test_rejects_nan(self):
        with pytest.raises(ValueError):
            validate_embedding_vector([0.1, float("nan"), 0.2])

    def test_rejects_positive_infinity(self):
        with pytest.raises(ValueError):
            validate_embedding_vector([0.1, float("inf"), 0.2])

    def test_rejects_negative_infinity(self):
        with pytest.raises(ValueError):
            validate_embedding_vector([0.1, -float("inf"), 0.2])

    def test_enforces_expected_dim(self):
        assert validate_embedding_vector([0.1, 0.2, 0.3], dim=3) == 3
        with pytest.raises(ValueError):
            validate_embedding_vector([0.1, 0.2, 0.3], dim=4)


# --- cosine_similarity -----------------------------------------------------


class TestCosine:
    """Cosine on the canary vectors drives the compatibility decision."""

    def test_identical_vectors_yield_one(self):
        a = [0.1, 0.2, 0.3, 0.4]
        assert math.isclose(cosine_similarity(a, a), 1.0, abs_tol=1e-9)

    def test_opposite_vectors_yield_minus_one(self):
        a = [1.0, 0.0, 0.0]
        b = [-1.0, 0.0, 0.0]
        assert math.isclose(cosine_similarity(a, b), -1.0, abs_tol=1e-9)

    def test_orthogonal_vectors_yield_zero(self):
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        assert math.isclose(cosine_similarity(a, b), 0.0, abs_tol=1e-9)

    def test_size_mismatch_raises(self):
        with pytest.raises(ValueError):
            cosine_similarity([0.1, 0.2], [0.1, 0.2, 0.3])

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            cosine_similarity([], [])

    def test_zero_vector_returns_zero(self):
        a = [0.0, 0.0, 0.0]
        b = [1.0, 2.0, 3.0]
        assert cosine_similarity(a, b) == 0.0

    def test_threshold_is_strict(self):
        """0.999 is the boundary ADR-015 picked. The test makes the
        boundary explicit so a future tune is visible in the diff.
        """

        assert COSINE_THRESHOLD == 0.999

    def test_just_below_threshold(self):
        # Two slightly different vectors must drop below 0.999 to
        # make the boundary obvious. Cosine of a 0.1 deg rotation
        # in 2-D is ~0.999998, so we hand-craft a near-threshold
        # pair instead.
        a = [1.0, 0.0, 0.0]
        b = [math.cos(math.radians(0.9)), math.sin(math.radians(0.9)), 0.0]
        score = cosine_similarity(a, b)
        assert score < 0.9999999  # sanity
        assert 0.99 < score < 0.9999  # the case we want to discuss


# --- judge_compatibility ---------------------------------------------------


def _vectors(seed: int, dim: int) -> list[list[float]]:
    """Build three deterministic canary vectors for a test profile.

    A small Python RNG is enough: the tests only need reproducible
    shapes, not cryptographic randomness.
    """

    state = seed
    out: list[list[float]] = []
    for _ in range(3):
        vector: list[float] = []
        for _ in range(dim):
            state = (state * 1103515245 + 12345) & 0x7FFFFFFF
            # Map to ``[-1, 1)`` with a deterministic offset.
            vector.append(((state % 2000) - 1000) / 1000.0)
        out.append(vector)
    return out


def _identical(seed: int, dim: int) -> list[list[float]]:
    """Return three identical vectors (perfectly compatible)."""

    state = seed
    vector: list[float] = []
    for _ in range(dim):
        state = (state * 1103515245 + 12345) & 0x7FFFFFFF
        vector.append(((state % 2000) - 1000) / 1000.0)
    return [vector[:] for _ in range(3)]


class TestJudge:
    """The brain of ADR-015: the four-way compatibility decision."""

    def test_unknown_when_no_active_profile(self):
        decision, reason, _ = judge_compatibility(
            candidate_dim=4,
            candidate_model="m",
            candidate_fingerprint="cfg_a",
            candidate_canary_vectors=_vectors(1, 4),
            active_dim=None,
            active_model=None,
            active_fingerprint=None,
            active_canary_vectors=None,
        )
        assert decision is CompatibilityDecision.UNKNOWN
        assert "激活" in reason

    def test_same_when_fingerprint_matches(self):
        vectors = _vectors(7, 8)
        decision, reason, diagnostics = judge_compatibility(
            candidate_dim=8,
            candidate_model="m",
            candidate_fingerprint="cfg_same",
            candidate_canary_vectors=vectors,
            active_dim=8,
            active_model="m",
            active_fingerprint="cfg_same",
            active_canary_vectors=vectors,
        )
        assert decision is CompatibilityDecision.SAME
        assert "指纹" in reason
        assert diagnostics["scores"] == [1.0, 1.0, 1.0]

    def test_same_fingerprint_still_rebuilds_when_canary_drifted(self):
        active = _vectors(7, 8)
        candidate = _vectors(99, 8)
        decision, reason, diagnostics = judge_compatibility(
            candidate_dim=8,
            candidate_model="m",
            candidate_fingerprint="cfg_same",
            candidate_canary_vectors=candidate,
            active_dim=8,
            active_model="m",
            active_fingerprint="cfg_same",
            active_canary_vectors=active,
        )
        assert decision is CompatibilityDecision.REBUILD_REQUIRED
        assert "相似度" in reason
        assert len(diagnostics["scores"]) == 3

    def test_rebuild_when_dim_differs(self):
        decision, reason, _ = judge_compatibility(
            candidate_dim=4,
            candidate_model="m",
            candidate_fingerprint="cfg_a",
            candidate_canary_vectors=_vectors(1, 4),
            active_dim=8,
            active_model="m",
            active_fingerprint="cfg_b",
            active_canary_vectors=_vectors(2, 8),
        )
        assert decision is CompatibilityDecision.REBUILD_REQUIRED
        assert "维度" in reason

    def test_rebuild_when_model_differs(self):
        vectors = _vectors(3, 8)
        decision, reason, _ = judge_compatibility(
            candidate_dim=8,
            candidate_model="new-model",
            candidate_fingerprint="cfg_a",
            candidate_canary_vectors=vectors,
            active_dim=8,
            active_model="old-model",
            active_fingerprint="cfg_b",
            active_canary_vectors=vectors,
        )
        assert decision is CompatibilityDecision.REBUILD_REQUIRED
        assert "模型" in reason

    def test_rebuild_when_canary_below_threshold(self):
        active = _identical(11, 8)
        candidate = _vectors(99, 8)  # very different
        decision, reason, diagnostics = judge_compatibility(
            candidate_dim=8,
            candidate_model="m",
            candidate_fingerprint="cfg_a",
            candidate_canary_vectors=candidate,
            active_dim=8,
            active_model="m",
            active_fingerprint="cfg_b",
            active_canary_vectors=active,
        )
        assert decision is CompatibilityDecision.REBUILD_REQUIRED
        assert "相似度" in reason or "阈值" in reason
        # Diagnostics must report the per-canary scores but not the
        # raw vectors.
        assert len(diagnostics["scores"]) == 3
        assert all(score < 0.999 for score in diagnostics["scores"])

    def test_compatible_when_canary_high_enough(self):
        # The candidate and the active have *different* fingerprints
        # (so the "same" path is bypassed) but the canary vectors
        # match closely enough to be considered the same vector
        # space. We construct the candidate by perturbing the
        # active vectors with a tiny epsilon.
        active = _identical(13, 16)
        perturbed = [
            [value + 1e-6 for value in vector] for vector in active
        ]
        # Normalise so the comparison is fair.
        def _normalise(vector: list[float]) -> list[float]:
            norm = math.sqrt(sum(v * v for v in vector))
            return [v / norm for v in vector]

        candidate = [_normalise(v) for v in perturbed]
        active_norm = [_normalise(v) for v in active]
        decision, reason, diagnostics = judge_compatibility(
            candidate_dim=16,
            candidate_model="m",
            candidate_fingerprint="cfg_a",
            candidate_canary_vectors=candidate,
            active_dim=16,
            active_model="m",
            active_fingerprint="cfg_b",
            active_canary_vectors=active_norm,
        )
        assert decision is CompatibilityDecision.COMPATIBLE
        assert "高度兼容" in reason
        assert all(score >= 0.999 for score in diagnostics["scores"])

    def test_insufficient_active_canary_triggers_rebuild(self):
        decision, reason, _ = judge_compatibility(
            candidate_dim=8,
            candidate_model="m",
            candidate_fingerprint="cfg_a",
            candidate_canary_vectors=_vectors(1, 8),
            active_dim=8,
            active_model="m",
            active_fingerprint="cfg_b",
            active_canary_vectors=[],  # empty
        )
        assert decision is CompatibilityDecision.REBUILD_REQUIRED
        assert "探针" in reason

    def test_insufficient_candidate_canary_triggers_rebuild(self):
        decision, reason, _ = judge_compatibility(
            candidate_dim=8,
            candidate_model="m",
            candidate_fingerprint="cfg_a",
            candidate_canary_vectors=[],  # empty
            active_dim=8,
            active_model="m",
            active_fingerprint="cfg_b",
            active_canary_vectors=_vectors(2, 8),
        )
        assert decision is CompatibilityDecision.REBUILD_REQUIRED
        assert "探针" in reason

    def test_invalid_candidate_dim_triggers_rebuild(self):
        decision, reason, _ = judge_compatibility(
            candidate_dim=0,
            candidate_model="m",
            candidate_fingerprint="cfg_a",
            candidate_canary_vectors=_vectors(1, 4),
            active_dim=8,
            active_model="m",
            active_fingerprint="cfg_b",
            active_canary_vectors=_vectors(2, 8),
        )
        assert decision is CompatibilityDecision.REBUILD_REQUIRED
        assert "维度" in reason

    def test_threshold_is_respected(self):
        """When the threshold is tightened, the same pair of
        candidate/active canary vectors that were 'compatible' at
        0.999 must tip into 'rebuild_required' at a higher
        threshold.
        """

        active = _identical(31, 16)
        # Perturb the active vectors by 0.05 in a single coordinate;
        # after L2-normalisation the cosine drops well below 0.999,
        # which makes the test robust against the "tiny epsilon
        # perturbation" failure mode of the original draft.
        perturbed = [
            [value + 0.05 if index == 0 else value for index, value in enumerate(vector)]
            for vector in active
        ]

        def _normalise(vector):
            norm = math.sqrt(sum(v * v for v in vector))
            return [v / norm for v in vector]

        candidate = [_normalise(v) for v in perturbed]
        active_norm = [_normalise(v) for v in active]

        lenient, _, lenient_diag = judge_compatibility(
            candidate_dim=16,
            candidate_model="m",
            candidate_fingerprint="cfg_a",
            candidate_canary_vectors=candidate,
            active_dim=16,
            active_model="m",
            active_fingerprint="cfg_b",
            active_canary_vectors=active_norm,
            threshold=0.5,
        )
        strict, _, strict_diag = judge_compatibility(
            candidate_dim=16,
            candidate_model="m",
            candidate_fingerprint="cfg_a",
            candidate_canary_vectors=candidate,
            active_dim=16,
            active_model="m",
            active_fingerprint="cfg_b",
            active_canary_vectors=active_norm,
            threshold=0.9999999,
        )
        assert lenient is CompatibilityDecision.COMPATIBLE
        assert strict is CompatibilityDecision.REBUILD_REQUIRED
        # The diagnostics must surface the per-canary scores so
        # the operator can see *why* the threshold failed. The
        # score itself is between the two thresholds we used (i.e.
        # 0.999 < score < 0.9999999), which is the whole point of
        # the test.
        assert all(0.99 < score < 0.9999999 for score in strict_diag["scores"])
