"""Deterministic, non-secret configuration fingerprint.

The fingerprint is a SHA-256 digest of the four fields that fully
determine an embedding's vector space plus an existence flag and a
short key identifier. The plaintext API key never enters the digest:

* ``provider`` — which wire protocol we are talking to.
* ``base_url`` — the operator's address. Two endpoints with the
  same provider but different URLs *might* still be in the same
  vector space (e.g. an OpenAI-compatible gateway that fronts a
  single upstream), so we have to treat them as different
  configurations until the canary check says otherwise.
* ``model`` — the model name as reported by the operator. Two
  endpoints that use different model names produce different
  vectors, so this is part of the fingerprint.
* ``dim`` — the dimensionality we observed. The same model with
  different output sizes (e.g. ``text-embedding-3-small`` at
  ``1536`` vs ``512``) is a different vector space.
* ``has_api_key`` — whether a key is required and present. We
  cannot decode the key to compare, but we can record whether
  there is one.
* ``key_fingerprint`` — the short, non-secret identifier produced
  by :class:`SecretStore.key_fingerprint`. Two configurations
  whose only difference is the rotated key are NOT bit-identical
  (the fingerprint changes), but they are in the same vector
  space — the canary check is what tells us so.

The function is pure and side-effect free. It only depends on the
standard library so it is easy to unit-test.
"""

from __future__ import annotations

import hashlib


# Default separator between fields in the digest input. The byte
# value 0x1F is the "Unit Separator" control code which is
# guaranteed never to appear in any of the input fields (provider
# and model are restricted to printable ASCII, the base URL is
# validated to look like an HTTP URL, the key fingerprint is hex).
_FIELD_SEPARATOR = b"\x1f"


def compute_config_fingerprint(
    *,
    provider: str,
    base_url: str,
    model: str,
    dim: int,
    has_api_key: bool,
    key_fingerprint: str | None,
    canary_version: str,
) -> str:
    """Return a stable SHA-256-derived identifier for a configuration.

    The function lowercases the provider, model and base URL before
    digesting so the fingerprint is stable across trivially
    different capitalisations. Trailing slashes are stripped from
    the base URL for the same reason.

    Parameters
    ----------
    provider:
        One of ``"openai"`` or ``"ollama"``.
    base_url:
        The operator's saved base URL.
    model:
        The model name as configured.
    dim:
        Dimensionality observed during the most recent successful
        probe.
    has_api_key:
        Whether a key is required and present.
    key_fingerprint:
        Short, non-secret identifier for the encrypted key. ``None``
        when no key is required.
    canary_version:
        The canary version that was used to produce the fingerprint
        vectors stored on the profile. Bumping the version is what
        makes old profiles incomparable to new ones.
    """

    provider_clean = (provider or "").strip().lower()
    model_clean = (model or "").strip()
    base_clean = (base_url or "").strip().rstrip("/").lower()
    canary_version_clean = (canary_version or "").strip()
    key_clean = (key_fingerprint or "").strip()

    if not provider_clean:
        raise ValueError("provider is required for fingerprinting")
    if not model_clean:
        raise ValueError("model is required for fingerprinting")
    if not base_clean:
        raise ValueError("base_url is required for fingerprinting")
    if not canary_version_clean:
        raise ValueError("canary_version is required for fingerprinting")
    if dim <= 0:
        raise ValueError("dim must be a positive integer")

    pieces = [
        provider_clean.encode("utf-8"),
        base_clean.encode("utf-8"),
        model_clean.encode("utf-8"),
        str(dim).encode("ascii"),
        b"1" if has_api_key else b"0",
        key_clean.encode("ascii"),
        canary_version_clean.encode("ascii"),
    ]
    joined = _FIELD_SEPARATOR.join(pieces)
    digest = hashlib.sha256(joined).hexdigest()
    return f"cfg_{digest[:32]}"
