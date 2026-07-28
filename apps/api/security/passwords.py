"""Password hashing for the single-admin login.

The algorithm is scrypt (RFC 7914), called with conservative
parameters. The salt is a 16-byte random value per password and is
stored alongside the hash so the verifier is self-contained.

The output is a single string of the form::

    scrypt$N=2^15,r=8,p=1$<salt-hex>$<hash-hex>

so the parameters and the salt travel together with the hash. A
future migration to argon2 or bcrypt can be implemented by
accepting the leading ``algo`` token and dispatching on it.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass


SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
HASH_BYTES = 32
ALGO_NAME = "scrypt"


@dataclass
class HashedPassword:
    """A self-describing scrypt hash.

    The ``serialized`` form is what we put in the database. The
    individual fields are exposed for tests and migrations.
    """

    salt: bytes
    hash: bytes
    n: int
    r: int
    p: int
    algo: str = ALGO_NAME

    @property
    def serialized(self) -> str:
        return (
            f"{self.algo}$N={self.n},r={self.r},p={self.p}"
            f"${self.salt.hex()}${self.hash.hex()}"
        )


def hash_password(password: str) -> HashedPassword:
    """Return a scrypt hash of ``password`` with a fresh random salt."""

    if not isinstance(password, str) or password == "":
        raise ValueError("密码不能为空")
    salt = secrets.token_bytes(SALT_BYTES)
    digest = _scrypt(password.encode("utf-8"), salt)
    return HashedPassword(salt=salt, hash=digest, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)


def verify_password(password: str, serialized: str) -> bool:
    """Constant-time check of ``password`` against ``serialized``."""

    parsed = _parse(serialized)
    if parsed is None:
        return False
    candidate = _scrypt(password.encode("utf-8"), parsed.salt, n=parsed.n, r=parsed.r, p=parsed.p)
    return hmac.compare_digest(candidate, parsed.hash)


def _scrypt(
    password: bytes,
    salt: bytes,
    *,
    n: int = SCRYPT_N,
    r: int = SCRYPT_R,
    p: int = SCRYPT_P,
) -> bytes:
    return hashlib.scrypt(password, salt=salt, n=n, r=r, p=p, dklen=HASH_BYTES)


def _parse(value: str) -> HashedPassword | None:
    if not value:
        return None
    parts = value.split("$")
    if len(parts) != 4:
        return None
    algo, params, salt_hex, hash_hex = parts
    if algo != ALGO_NAME:
        return None
    parsed_params: dict[str, int] = {}
    for chunk in params.split(","):
        key, _, raw = chunk.partition("=")
        if not key or not raw:
            return None
        try:
            parsed_params[key] = int(raw)
        except ValueError:
            return None
    try:
        n = parsed_params["N"]
        r = parsed_params["r"]
        p = parsed_params["p"]
    except KeyError:
        return None
    try:
        salt = bytes.fromhex(salt_hex)
        digest = bytes.fromhex(hash_hex)
    except ValueError:
        return None
    if len(digest) != HASH_BYTES:
        return None
    return HashedPassword(salt=salt, hash=digest, n=n, r=r, p=p, algo=algo)
