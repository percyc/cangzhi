"""Unit tests for the scrypt password helpers."""

from __future__ import annotations

import pytest

from apps.api.security.passwords import (
    HashedPassword,
    hash_password,
    verify_password,
)


def test_hash_password_uses_scrypt_with_default_parameters():
    hashed = hash_password("correct horse battery staple")
    assert isinstance(hashed, HashedPassword)
    assert hashed.algo == "scrypt"
    from apps.api.security.passwords import SCRYPT_N
    assert hashed.n == SCRYPT_N
    assert hashed.r == 8
    assert hashed.p == 1
    assert len(hashed.salt) == 16
    assert len(hashed.hash) == 32
    # The serialized form embeds the parameters and the salt so the
    # verifier can recover them.
    serialized = hashed.serialized
    assert serialized.startswith("scrypt$N=")
    assert f"r={hashed.r},p={hashed.p}" in serialized


def test_hash_password_rejects_empty():
    with pytest.raises(ValueError):
        hash_password("")


def test_hash_password_produces_unique_salts():
    a = hash_password("hunter2")
    b = hash_password("hunter2")
    assert a.salt != b.salt
    assert a.hash != b.hash


def test_verify_password_accepts_correct_plaintext():
    hashed = hash_password("a-strong-passphrase")
    assert verify_password("a-strong-passphrase", hashed.serialized) is True


def test_verify_password_rejects_wrong_plaintext():
    hashed = hash_password("a-strong-passphrase")
    assert verify_password("a-wrong-passphrase", hashed.serialized) is False


def test_verify_password_rejects_garbled_serialized_form():
    assert verify_password("anything", "not-a-real-hash") is False
    assert verify_password("anything", "") is False
    assert verify_password("anything", "scrypt$oops") is False


def test_verify_password_rejects_unsupported_algorithm_token():
    forged = "argon2$N=2,r=8,p=1$" + "00" * 16 + "$" + "00" * 32
    assert verify_password("anything", forged) is False
