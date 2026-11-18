"""Development-only bearer tokens for the local demo. Never an HTTP endpoint.

    python -m rfq_copilot.dev_token keygen [--dir ~/.config/rfq_copilot]
    python -m rfq_copilot.dev_token issue --subject SUBJECT [--ttl 900] [--key PATH] [--kid KID]

``keygen`` writes a new Ed25519 pair outside the repository: the private key with mode 0600
in a 0700 directory (never overwritten), the public key with mode 0644. Point the API at
the public key (``RFQ_API_JWT_ALGORITHM=EdDSA``, ``RFQ_API_JWT_PUBLIC_KEY_FILE=...``).

``issue`` reads the same ``RFQ_API_*`` settings the API reads (process environment only),
refuses unless ``RFQ_API_ENVIRONMENT=development`` and ``EdDSA`` are configured, signs a
short-lived token (``iss``/``aud``/``sub``/``iat``/``exp``, lifetime at most the configured
maximum) and verifies it with the API's own ``TokenVerifier`` before printing it. The token
grants nothing by itself: the subject must be an active user with an eligible binding and
granted permissions, all re-read by the API per request.

``DevMcpCredentialProvider`` (Checkpoint A2) is the workflow worker's in-process MCP
credential source on a development machine; see its docstring.
"""

import argparse
import json
import os
import secrets
import stat
import sys
import time
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from rfq_copilot.api.config import ApiSettings
from rfq_copilot.api.tokens import InvalidToken, TokenVerifier

DEFAULT_DIR = Path.home() / ".config" / "rfq_copilot"
PRIVATE_NAME = "dev_jwt_ed25519.pem"
PUBLIC_NAME = "dev_jwt_ed25519.pub.pem"
ALGORITHM = "EdDSA"


class DevTokenError(Exception):
    """A fixed, non-sensitive refusal."""


def keygen(directory: Path) -> tuple[Path, Path]:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.stat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise DevTokenError("key directory must be owned by you with mode 0700")
    private_path, public_path = directory / PRIVATE_NAME, directory / PUBLIC_NAME
    key = ed25519.Ed25519PrivateKey.generate()
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    for path, data, mode in ((private_path, private, 0o600), (public_path, public, 0o644)):
        try:  # O_EXCL: an existing key is never replaced
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
        except FileExistsError:
            raise DevTokenError(f"{path.name} already exists; remove it to rotate") from None
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        os.chmod(path, mode)
    return private_path, public_path


def _private_key(path: Path) -> ed25519.Ed25519PrivateKey:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise DevTokenError("private key must be a regular file you own with mode 0600")
        key = serialization.load_pem_private_key(handle.read(), password=None)
    if not isinstance(key, ed25519.Ed25519PrivateKey):
        raise DevTokenError("private key is not Ed25519")
    return key


def issue(
    settings: ApiSettings, key_path: Path, subject: str, ttl: int, kid: str | None = None, now=None
) -> str:
    if settings.environment != "development":
        raise DevTokenError("refused: RFQ_API_ENVIRONMENT is not development")
    if settings.jwt_algorithm != ALGORITHM or settings.jwt_issuer is None:
        raise DevTokenError("refused: configure RFQ_API_JWT_* with algorithm EdDSA")
    if not subject or subject != subject.strip():
        raise DevTokenError("invalid subject")
    if not 60 <= ttl <= settings.jwt_max_lifetime_seconds:
        raise DevTokenError("ttl must be between 60 and RFQ_API_JWT_MAX_LIFETIME_SECONDS")
    issued = int(time.time() if now is None else now)
    claims = {
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "sub": subject,
        "iat": issued,
        "exp": issued + ttl,
    }
    headers = {"kid": kid} if kid else None
    token = jwt.encode(claims, _private_key(key_path), algorithm=ALGORITHM, headers=headers)
    if now is None:  # self-check against the API's own verifier (wrong pair, kid, settings)
        try:
            TokenVerifier.from_settings(settings).verify(token)
        except InvalidToken:
            raise DevTokenError("issued token fails the configured verifier") from None
    return token


SERVICE_PREFIX = "svc|"  # operator_admin.SERVICE_PREFIX: the only subjects minted below
MAX_SERVICE_TTL = 300


class DevMcpCredentialProvider:
    """Checkpoint A2, development only: ``api.mcp_integration.McpCredentialProvider`` for the
    workflow worker. Signs a short-lived token for the **MCP audience** and the verified
    service principal's own subject, then self-verifies it with the MCP host's verifier
    settings. It never mints for a human or customer subject (only ``svc|``), never reads a
    REST token, and holds the key only in memory (never logged, never in a repr).

    It grants nothing by itself: the MCP host re-verifies signature, issuer, audience,
    lifetime and revocation, then re-reads the active user, its single binding and the
    role's v2 tool permissions from the database on every call.
    """

    def __init__(self, settings, key_path: Path, *, environment: str, ttl: int = MAX_SERVICE_TTL):
        from rfq_copilot.api.tokens import load_public_key

        if environment != "development":
            raise DevTokenError("refused: the workflow environment is not development")
        if settings.jwt_algorithm != ALGORITHM:
            raise DevTokenError("refused: configure RFQ_MCP_JWT_* with algorithm EdDSA")
        if not 60 <= ttl <= min(MAX_SERVICE_TTL, settings.jwt_max_lifetime_seconds):
            raise DevTokenError("ttl must be between 60 and the MCP maximum (at most 300)")
        self.audience, self.issuer, self._ttl = settings.jwt_audience, settings.jwt_issuer, ttl
        self._verifier = TokenVerifier(
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            algorithm=ALGORITHM,
            key=load_public_key(settings.jwt_public_key_file, ALGORITHM),
            max_lifetime_seconds=settings.jwt_max_lifetime_seconds,
        )
        self.__key = _private_key(key_path)

    def __repr__(self) -> str:
        return f"DevMcpCredentialProvider(audience={self.audience!r})"

    async def credential_for(self, principal) -> str:
        subject = getattr(principal, "subject", None)
        if not isinstance(subject, str) or not subject.startswith(SERVICE_PREFIX) or len(subject) > 255:  # fmt: skip
            raise DevTokenError("refused: only a service subject is minted")
        issued = int(time.time())
        # jti: EdDSA is deterministic, so without it two tokens of one second are identical
        # and revoking one (by its sha256) would revoke the other too.
        claims = {"iss": self.issuer, "aud": self.audience, "sub": subject, "iat": issued,
                  "exp": issued + self._ttl, "jti": secrets.token_hex(16)}  # fmt: skip
        token = jwt.encode(claims, self.__key, algorithm=ALGORITHM)
        try:
            if self._verifier.verify(token).subject != subject:
                raise InvalidToken("subject")
        except InvalidToken:
            raise DevTokenError("issued token fails the MCP verifier") from None
        return token


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("keygen", help="create a new Ed25519 key pair")
    gen.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    out = sub.add_parser("issue", help="print one short-lived token for SUBJECT")
    out.add_argument("--subject", required=True)
    out.add_argument("--ttl", type=int, default=900)
    out.add_argument("--key", type=Path, default=DEFAULT_DIR / PRIVATE_NAME)
    out.add_argument("--kid")
    args = parser.parse_args(argv)
    try:
        if args.command == "keygen":
            private_path, public_path = keygen(args.dir.expanduser())
            print(json.dumps({"private_key": str(private_path), "public_key": str(public_path),
                              "set": {"RFQ_API_JWT_ALGORITHM": ALGORITHM,
                                      "RFQ_API_JWT_PUBLIC_KEY_FILE": str(public_path)}}))  # fmt: skip
        else:
            settings = ApiSettings.from_env()
            print(issue(settings, args.key.expanduser(), args.subject, args.ttl, args.kid))
    except (DevTokenError, OSError, ValueError) as error:
        message = str(error) if isinstance(error, DevTokenError) else type(error).__name__
        print(json.dumps({"status": "refused", "error": message}), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
