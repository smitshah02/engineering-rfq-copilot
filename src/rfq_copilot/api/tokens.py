"""Bearer-token verification against one configured issuer (PyJWT).

The trust boundary is fixed by configuration: issuer, audience, one asymmetric algorithm
and one public key, or a configured key set for rotation (TD-013). Nothing in a token can
change it: the header's ``alg`` must equal the configured algorithm, and key-locating
headers (``jku``, ``jwk``, ``x5u``, ``x5c``) and ``crit`` are refused. With one key,
``kid`` is ignored; with a key set it must name a configured key exactly and selects
only that key (no fallback to the others, nothing fetched).

A verified token yields only its subject. Every other claim (customer, role, scope,
email) is ignored; identity and customer come from the database (``api/auth.py``).
Errors are a single ``InvalidToken`` with no detail, so nothing about the token reaches
the client or the logs.
"""

import os
import stat
import time
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from cryptography.hazmat.primitives.serialization import load_pem_public_key

from rfq_copilot.api.config import ApiSettings
from rfq_copilot.paths import UntrustedPathError, open_trusted_parent

MAX_TOKEN_CHARS = 4096
MAX_KEY_FILE_BYTES = 16 * 1024
MAX_SUBJECT_CHARS = 255
LEEWAY_SECONDS = 30  # clock skew tolerated for exp, nbf and iat
MIN_RSA_BITS = 2048
_ALLOWED_HEADERS = frozenset({"alg", "typ", "kid"})
_ALLOWED_TYPES = frozenset({"jwt", "at+jwt"})


class InvalidToken(Exception):
    """The token is not acceptable. Deliberately carries no reason."""


class TokenConfigurationError(ValueError):
    """The configured verification key is missing, unsafe or of the wrong kind."""


def _key_matches(algorithm: str, key: Any) -> bool:
    if algorithm == "RS256":
        return isinstance(key, rsa.RSAPublicKey) and key.key_size >= MIN_RSA_BITS
    if algorithm == "ES256":
        return isinstance(key, ec.EllipticCurvePublicKey) and isinstance(key.curve, ec.SECP256R1)
    return isinstance(key, ed25519.Ed25519PublicKey)  # EdDSA


def _trusted(info: os.stat_result) -> bool:
    """Owned by root or by this service's user, and not writable by group or others."""
    return info.st_uid in (0, os.geteuid()) and not info.st_mode & 0o022


def _read_key_file(path: os.PathLike) -> bytes:
    """Read the key file through descriptors, so what is checked is what is read.

    The parent directory is opened and checked first (``fstat``): only its owner (root
    or this service's user) can rename entries in it. The file is then opened relative
    to that descriptor with ``O_NOFOLLOW`` (a symlink as the name fails) and
    ``O_NONBLOCK`` (a FIFO cannot hang startup), and ``fstat`` on the opened descriptor
    checks type, owner and mode. Reading stops after ``MAX_KEY_FILE_BYTES + 1`` bytes.
    Every directory above it is checked too (``rfq_copilot.paths``): owned by root or
    this service's user, and not writable by others unless sticky. POSIX only.
    """
    unsafe = TokenConfigurationError(
        "JWT public key file must be a regular file owned by root or the service user, "
        "not a symlink, and not group/world-writable, in a directory with the same rules."
    )
    try:
        directory, name = open_trusted_parent(path)
    except UntrustedPathError:
        raise unsafe from None
    try:
        if not _trusted(os.fstat(directory)):
            raise unsafe
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        try:
            descriptor = os.open(name, flags, dir_fd=directory)
        except OSError:
            if stat.S_ISLNK(os.lstat(name, dir_fd=directory).st_mode):
                raise unsafe from None
            raise
    finally:
        os.close(directory)
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or not _trusted(info):
            raise unsafe
        data = handle.read(MAX_KEY_FILE_BYTES + 1)
    if len(data) > MAX_KEY_FILE_BYTES:
        raise TokenConfigurationError("JWT public key file is too large.")
    return data


def load_public_key(path: os.PathLike, algorithm: str) -> Any:
    """Load a PEM public key that only root or this server's user can modify.

    A private key, a symlink, an unsafe file or directory (``_read_key_file``), an RSA
    key below 2048 bits, or a key of another type than ``algorithm`` is rejected.
    Messages name no path or key material.
    """
    try:
        key = load_pem_public_key(_read_key_file(path))
    except TokenConfigurationError:
        raise
    except (OSError, ValueError, TypeError):
        raise TokenConfigurationError("JWT public key could not be loaded as a PEM public key.") from None  # fmt: skip
    if not _key_matches(algorithm, key):
        raise TokenConfigurationError("JWT public key does not match the algorithm or is too weak.")
    return key


@dataclass(frozen=True)
class TokenVerifier:
    issuer: str
    audience: str
    algorithm: str
    key: Any = field(repr=False)  # the single configured key; None with a key set
    max_lifetime_seconds: int
    # Rotation (TD-013): configured key id -> key. The header's kid selects among these
    # only; an absent or unknown kid is rejected, never tried against every key.
    key_set: Mapping[str, Any] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if (self.key is None) == (self.key_set is None):
            raise TokenConfigurationError("Configure exactly one key or one key set.")
        if self.key_set is not None:
            object.__setattr__(self, "key_set", MappingProxyType(dict(self.key_set)))

    @classmethod
    def from_settings(cls, settings: ApiSettings) -> "TokenVerifier | None":
        if not settings.authentication_configured:
            return None
        algorithm = settings.jwt_algorithm
        if settings.jwt_public_keys is not None:
            key, key_set = (
                None,
                {kid: load_public_key(path, algorithm) for kid, path in settings.jwt_public_keys},
            )
        else:
            key, key_set = load_public_key(settings.jwt_public_key_file, algorithm), None
        return cls(
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            algorithm=algorithm,
            key=key,
            max_lifetime_seconds=settings.jwt_max_lifetime_seconds,
            key_set=key_set,
        )

    def subject(self, token: str) -> str:
        """Verify ``token`` and return its subject, or raise ``InvalidToken``."""
        return self.verify(token).subject

    def verify(self, token: str) -> "VerifiedToken":
        """Verify ``token``; return its subject and integer ``iat``, or raise ``InvalidToken``."""
        if not isinstance(token, str) or not 0 < len(token) <= MAX_TOKEN_CHARS:
            raise InvalidToken
        if not token.isascii() or token.count(".") != 2:
            raise InvalidToken
        try:
            header = jwt.get_unverified_header(token)
            if (
                not set(header) <= _ALLOWED_HEADERS
                or header.get("alg") != self.algorithm
                or str(header.get("typ", "JWT")).lower() not in _ALLOWED_TYPES
            ):
                raise InvalidToken
            key = self.key
            if self.key_set is not None:
                kid = header.get("kid")
                if not isinstance(kid, str) or kid not in self.key_set:
                    raise InvalidToken
                key = self.key_set[kid]
            claims = jwt.decode(
                token,
                key,
                algorithms=[self.algorithm],  # never the token's own choice
                audience=self.audience,
                issuer=self.issuer,
                leeway=LEEWAY_SECONDS,
                options={
                    "require": ["exp", "iat", "iss", "aud", "sub"],
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_nbf": True,
                    "verify_iat": True,
                    "verify_aud": True,
                    "verify_iss": True,
                    "strict_aud": True,  # aud must be exactly the configured string
                },
            )
        except InvalidToken:
            raise
        except (jwt.PyJWTError, ValueError, TypeError, KeyError):
            raise InvalidToken from None
        current = time.time()
        issued, expires = claims["iat"], claims["exp"]
        if any(type(value) is not int for value in (issued, expires)):
            raise InvalidToken  # bools and floats are not NumericDate seconds here
        if issued > current + LEEWAY_SECONDS or expires - issued > self.max_lifetime_seconds:
            raise InvalidToken
        if expires <= issued:
            raise InvalidToken
        return VerifiedToken(_subject(claims["sub"]), issued)


@dataclass(frozen=True)
class VerifiedToken:
    subject: str
    issued_at: int  # seconds since the epoch, as the verified ``iat`` claim


def _subject(value: object) -> str:
    # Same shape as users.authentication_subject (nonblank text); kept verbatim.
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= MAX_SUBJECT_CHARS
        or value != value.strip()
        or any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in value)
    ):
        raise InvalidToken
    return value
