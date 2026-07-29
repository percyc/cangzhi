"""Fixed canary texts used to detect embedding vector-space drift.

ADR-015 requires that the compatibility test between two embedding
configurations is grounded in actual vector outputs, not in the
model name. We send a small set of fixed, versioned short texts to
both the active and the candidate embedding endpoint and compare the
returned vectors. If the cosine similarity per probe is high enough
we treat the two configurations as the same vector space.

The canary texts are versioned so a future operator can rotate
them by changing :data:`CANARY_VERSION` together with the list.
Versioning is important: if we ever change the wording, a profile
that was tested against ``v1`` cannot be reasonably compared with a
profile tested against ``v2``, so the compatibility check must
demand that both sides agree on the canary version.

Three short, fixed texts are used:

* One Chinese sentence that exercises the multilingual nature of
  most modern embedding models. Mixing scripts also makes it less
  likely that a model silently drops non-ASCII inputs.
* One English sentence that focuses on the retrieval / recall
  vocabulary we use throughout the product, so the test has
  semantic overlap with the eventual query workload.
* One Latin / mixed-sentence "tag" that catches a model which
  happens to return identical-looking vectors for two distinct
  English strings (the third text is shorter and more obviously a
  probe).

All three are deliberately short. The compatibility test should
not consume a meaningful amount of the operator's embedding quota
and must never read user documents.
"""

from __future__ import annotations

from dataclasses import dataclass


CANARY_VERSION: str = "1"

# Three short, fixed, versioned canary strings. The wording is part
# of the public contract: if you change it you must also bump
# :data:`CANARY_VERSION` so old profiles are no longer considered
# comparable to new ones.
CANARY_TEXTS: tuple[str, ...] = (
    "知识库检索与向量召回兼容性测试。",
    "Knowledge base retrieval vector recall compatibility test.",
    "Cangzhi embedding canary v1.",
)


@dataclass(frozen=True)
class CanarySet:
    """A versioned bundle of canary probe texts.

    Storing the version alongside the texts means downstream code
    never has to guess which list the operator is using: the
    fingerprint that goes onto the profile row always includes the
    version, and the compatibility check refuses to compare two
    profiles recorded under different versions.
    """

    version: str
    texts: tuple[str, ...]

    def __iter__(self):  # pragma: no cover - trivial
        return iter(self.texts)

    def __len__(self) -> int:  # pragma: no cover - trivial
        return len(self.texts)


def get_canary_texts() -> CanarySet:
    """Return the active canary set.

    The result is a :class:`CanarySet` so callers always have
    access to the version without having to import the constant
    separately.
    """

    return CanarySet(version=CANARY_VERSION, texts=CANARY_TEXTS)
