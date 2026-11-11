"""Day 20 bearer authentication: token verification, trust configuration, get_principal.

Public smoke tests: no PostgreSQL, no dataset, no stored credentials. Every key pair is
generated per test session and lives only in a temporary directory. The database user
lookup is replaced by an in-memory fake here; test_day20_auth_database.py runs it
against the guarded PostgreSQL test database.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import SimpleNamespace

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from fastapi.testclient import TestClient

import rfq_copilot.repositories as repositories
from rfq_copilot.api import tokens
from rfq_copilot.api.app import create_app
from rfq_copilot.api.auth import Principal, get_principal
from rfq_copilot.api.config import ApiSettings
from rfq_copilot.api.tokens import (
    MAX_TOKEN_CHARS,
    InvalidToken,
    TokenConfigurationError,
    TokenVerifier,
    load_public_key,
)
from rfq_copilot.database import DatabaseError

ISSUER = "https://idp.test.invalid/"
AUDIENCE = "rfq-copilot-api"
SUBJECT = "auth|test-subject-1"

PROTECTED = [
    ("GET", "/api/v1/customers/me"),
    ("GET", "/api/v1/rfqs"),
    ("GET", "/api/v1/rfqs/RFQ-1"),
    ("GET", "/api/v1/materials"),
    ("GET", "/api/v1/quotes/Q-1"),
    ("GET", "/api/v1/rfqs/RFQ-1/attachments"),
    ("POST", "/api/v1/rfqs/RFQ-1/attachments"),
]


# --- Key material and tokens (generated, never stored) -----------------------------------


def pem_public(private_key):
    return private_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def write_key(directory, name, data, mode=0o600):
    path = directory / name
    path.write_bytes(data)
    path.chmod(mode)
    return path


@dataclass(frozen=True)
class Trust:
    private_key: object
    algorithm: str
    key_file: object
    directory: object

    def settings(self, **overrides):
        values = {
            "environment": "development",
            "jwt_issuer": ISSUER,
            "jwt_audience": AUDIENCE,
            "jwt_algorithm": self.algorithm,
            "jwt_public_key_file": self.key_file,
            **overrides,
        }
        return ApiSettings(**values)

    def verifier(self, **overrides):
        return TokenVerifier.from_settings(self.settings(**overrides))

    def mint(self, *, headers=None, key=None, algorithm=None, drop=(), **claims):
        now = int(time.time())
        payload = {"iss": ISSUER, "aud": AUDIENCE, "sub": SUBJECT, "iat": now, "exp": now + 300}
        payload.update(claims)
        for name in drop:
            payload.pop(name, None)
        return jwt.encode(
            payload,
            key or self.private_key,
            algorithm=algorithm or self.algorithm,
            headers=headers,
        )


@pytest.fixture(scope="session")
def trust(tmp_path_factory):
    directory = tmp_path_factory.mktemp("trust")
    private_key = ec.generate_private_key(ec.SECP256R1())
    return Trust(private_key, "ES256", write_key(directory, "es256.pem", pem_public(private_key)), directory)  # fmt: skip


@pytest.fixture(scope="session")
def rsa_trust(tmp_path_factory):
    directory = tmp_path_factory.mktemp("trust-rsa")
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return Trust(private_key, "RS256", write_key(directory, "rs256.pem", pem_public(private_key)), directory)  # fmt: skip


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def handmade(header, payload, signature=b""):
    head = b64(json.dumps(header).encode())
    body = b64(json.dumps(payload).encode())
    return f"{head}.{body}.{b64(signature)}"


def claims(**overrides):
    now = int(time.time())
    return {"iss": ISSUER, "aud": AUDIENCE, "sub": SUBJECT, "iat": now, "exp": now + 300, **overrides}  # fmt: skip


def rejects(verifier, token):
    with pytest.raises(InvalidToken):
        verifier.subject(token)


# --- Token verification ------------------------------------------------------------------


def test_valid_token_yields_only_its_subject(trust, rsa_trust):
    assert trust.verifier().subject(trust.mint()) == SUBJECT
    assert rsa_trust.verifier().subject(rsa_trust.mint()) == SUBJECT
    token = trust.mint(headers={"kid": "any", "typ": "at+jwt"}, nbf=int(time.time()) - 5)
    assert trust.verifier().subject(token) == SUBJECT


def test_eddsa_is_supported(tmp_path):
    private_key = ed25519.Ed25519PrivateKey.generate()
    t = Trust(
        private_key, "EdDSA", write_key(tmp_path, "ed.pem", pem_public(private_key)), tmp_path
    )
    assert t.verifier().subject(t.mint()) == SUBJECT


@pytest.mark.parametrize(
    "token",
    ["", "abc", "a.b", "a.b.c.d", "a.b.c", "é.é.é", "x" * (MAX_TOKEN_CHARS + 1), " . . "],
)
def test_malformed_and_oversized_tokens_are_rejected(trust, token):
    rejects(trust.verifier(), token)


def test_oversized_token_is_rejected_before_decoding(trust, monkeypatch):
    def tripwire(*args, **kwargs):
        raise AssertionError("must not decode")

    monkeypatch.setattr(jwt, "get_unverified_header", tripwire)
    rejects(trust.verifier(), trust.mint(padding="p" * MAX_TOKEN_CHARS))


def test_invalid_signature_is_rejected(trust):
    other = ec.generate_private_key(ec.SECP256R1())
    rejects(trust.verifier(), trust.mint(key=other))
    head, body, signature = trust.mint().split(".")
    tampered_body = b64(json.dumps(claims(sub="auth|admin")).encode())
    rejects(trust.verifier(), f"{head}.{tampered_body}.{signature}")
    flipped = signature[:-2] + ("AA" if signature[-2:] != "AA" else "BB")
    rejects(trust.verifier(), f"{head}.{body}.{flipped}")


@pytest.mark.parametrize(
    "overrides",
    [
        {"exp": int(time.time()) - 120},  # expired beyond leeway
        {"nbf": int(time.time()) + 600},  # not yet valid
        {"iat": int(time.time()) + 600, "exp": int(time.time()) + 800},  # issued in the future
        {"iss": "https://evil.invalid/"},
        {"iss": ISSUER.rstrip("/")},  # near-miss issuer
        {"aud": "another-api"},
        {"aud": [AUDIENCE, "another-api"]},  # strict audience: one exact string
        {"exp": int(time.time()) + 7200},  # lifetime beyond the configured maximum
        {"iat": True},
        {"exp": float(time.time() + 100)},
        {"sub": ""},
        {"sub": " padded"},
        {"sub": "line\nbreak"},
        {"sub": "bidi‮eman"},
        {"sub": 42},
        {"sub": "s" * 256},
    ],
)
def test_claim_violations_are_rejected(trust, overrides):
    rejects(trust.verifier(), trust.mint(**overrides))


@pytest.mark.parametrize("missing", ["exp", "iat", "iss", "aud", "sub"])
def test_required_claims_are_enforced(trust, missing):
    rejects(trust.verifier(), trust.mint(drop=(missing,)))


def test_lifetime_bound_is_configurable(trust):
    token = trust.mint(exp=int(time.time()) + 200)
    assert trust.verifier().subject(token) == SUBJECT
    rejects(trust.verifier(jwt_max_lifetime_seconds=120), token)


def test_unsigned_and_none_algorithm_tokens_are_rejected(trust):
    for alg in ("none", "None", "NONE"):
        rejects(trust.verifier(), handmade({"alg": alg, "typ": "JWT"}, claims()))
    rejects(trust.verifier(), handmade({"typ": "JWT"}, claims()))  # no alg at all


def test_hmac_algorithm_confusion_with_the_public_key_is_rejected(rsa_trust):
    # Classic confusion: sign HS256 with the server's public key bytes as the secret.
    secret = rsa_trust.key_file.read_bytes()
    header, payload = {"alg": "HS256", "typ": "JWT"}, claims()
    signing_input = f"{b64(json.dumps(header).encode())}.{b64(json.dumps(payload).encode())}"
    signature = hmac.new(secret, signing_input.encode(), hashlib.sha256).digest()
    rejects(rsa_trust.verifier(), f"{signing_input}.{b64(signature)}")


def test_other_asymmetric_algorithms_are_rejected(trust, rsa_trust):
    rejects(trust.verifier(), rsa_trust.mint())  # RS256 token, ES256 configured
    rejects(rsa_trust.verifier(), trust.mint())
    other = ec.generate_private_key(ec.SECP384R1())
    rejects(trust.verifier(), trust.mint(key=other, algorithm="ES384"))
    rejects(rsa_trust.verifier(), rsa_trust.mint(algorithm="PS256"))


@pytest.mark.parametrize(
    "header",
    [
        {"jku": "https://evil.invalid/jwks.json"},
        {"x5u": "https://evil.invalid/cert.pem"},
        {"crit": ["exp"]},
        {"typ": "JWS"},
    ],
)
def test_key_locating_and_unknown_headers_are_refused(trust, header):
    rejects(trust.verifier(), trust.mint(headers=header))


def test_embedded_jwk_is_never_trusted(trust):
    attacker = ec.generate_private_key(ec.SECP256R1())
    jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(attacker.public_key()))
    rejects(trust.verifier(), trust.mint(key=attacker, headers={"jwk": jwk}))


def test_privilege_claims_are_never_returned(trust):
    token = trust.mint(customer_id="CUST-OTHER", role="administrator", scope="admin")
    assert trust.verifier().subject(token) == SUBJECT  # only the subject leaves


# --- Trust configuration -----------------------------------------------------------------


def test_no_trust_configuration_means_no_verifier():
    assert TokenVerifier.from_settings(ApiSettings()) is None
    assert ApiSettings().authentication_configured is False


@pytest.mark.parametrize(
    "partial",
    [
        {"jwt_issuer": ISSUER},
        {"jwt_issuer": ISSUER, "jwt_audience": AUDIENCE, "jwt_algorithm": "ES256"},
        {"jwt_algorithm": "ES256", "jwt_public_key_file": "/keys/k.pem"},
    ],
)
def test_partial_trust_configuration_stops_startup(partial):
    with pytest.raises(ValueError):
        ApiSettings(**partial)


@pytest.mark.parametrize(
    "bad",
    [
        {"jwt_algorithm": "HS256"},
        {"jwt_algorithm": "none"},
        {"jwt_public_key_file": "relative/key.pem"},
        {"jwt_issuer": "has space"},
        {"jwt_max_lifetime_seconds": 86400},
        {"jwt_max_lifetime_seconds": 10},
    ],
)
def test_unsafe_trust_settings_are_rejected(trust, bad):
    with pytest.raises(ValueError):
        trust.settings(**bad)


def test_trust_settings_come_from_environment(trust):
    settings = ApiSettings.from_env(
        {
            "RFQ_API_JWT_ISSUER": ISSUER,
            "RFQ_API_JWT_AUDIENCE": AUDIENCE,
            "RFQ_API_JWT_ALGORITHM": "ES256",
            "RFQ_API_JWT_PUBLIC_KEY_FILE": str(trust.key_file),
            "RFQ_API_JWT_MAX_LIFETIME_SECONDS": "600",
        }
    )
    assert TokenVerifier.from_settings(settings).subject(trust.mint()) == SUBJECT


def test_weak_wrong_or_private_keys_stop_startup(tmp_path, trust):
    weak = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    cases = {
        "weak-rsa.pem": (pem_public(weak), "RS256"),
        "p384.pem": (pem_public(ec.generate_private_key(ec.SECP384R1())), "ES256"),
        "ec-as-rsa.pem": (trust.key_file.read_bytes(), "RS256"),
        "garbage.pem": (
            b"-----BEGIN PUBLIC KEY-----\nnot a key\n-----END PUBLIC KEY-----\n",
            "ES256",
        ),  # fmt: skip
        "private.pem": (
            trust.private_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
            "ES256",
        ),
    }
    for name, (data, algorithm) in cases.items():
        path = write_key(tmp_path, name, data)
        with pytest.raises(TokenConfigurationError) as caught:
            load_public_key(path, algorithm)
        assert str(tmp_path) not in str(caught.value)
        with pytest.raises(TokenConfigurationError):
            create_app(replace_settings(trust, path, algorithm))


def replace_settings(trust, path, algorithm):
    return trust.settings(jwt_public_key_file=path, jwt_algorithm=algorithm)


def test_key_file_must_not_be_writable_by_others_or_a_symlink(tmp_path, trust):
    data = trust.key_file.read_bytes()
    for mode in (0o666, 0o620, 0o602):
        path = write_key(tmp_path, f"k{mode:o}.pem", data, mode)
        with pytest.raises(TokenConfigurationError):
            load_public_key(path, "ES256")
    link = tmp_path / "link.pem"
    link.symlink_to(trust.key_file)
    with pytest.raises(TokenConfigurationError):
        load_public_key(link, "ES256")
    with pytest.raises(TokenConfigurationError):
        load_public_key(tmp_path / "missing.pem", "ES256")
    assert load_public_key(write_key(tmp_path, "ok.pem", data, 0o644), "ES256") is not None


def test_key_path_substituted_after_a_check_never_loads_the_attackers_key(
    tmp_path, trust, monkeypatch
):
    """Reproduces the lstat()/open() race deterministically.

    The first filesystem check the loader makes (path or descriptor based) triggers the
    attack: the key path is atomically replaced by a symlink to the attacker's public
    key. Whatever the loader returns must never accept a token signed by the attacker.
    The pre-correction loader (lstat, then open) loaded the attacker's key here.
    """
    legitimate = write_key(tmp_path, "jwt.pem", trust.key_file.read_bytes(), 0o644)
    attacker = ec.generate_private_key(ec.SECP256R1())
    planted = write_key(tmp_path, "attacker.pem", pem_public(attacker), 0o644)
    swapped = []

    def substitute():
        if not swapped:
            staged = tmp_path / "staged-link"
            staged.symlink_to(planted)
            os.replace(staged, legitimate)  # atomic rename over the checked name
            swapped.append(True)

    for name in ("lstat", "stat", "fstat"):
        real = getattr(os, name)

        def checked(*args, _real=real, **kwargs):
            result = _real(*args, **kwargs)
            substitute()
            return result

        monkeypatch.setattr(tokens.os, name, checked)
    try:
        key = tokens.load_public_key(legitimate, "ES256")
    except TokenConfigurationError as error:
        key = None
        assert str(tmp_path) not in str(error)
    monkeypatch.undo()
    assert swapped, "the substitution must have happened during loading"
    if key is not None:
        verifier = TokenVerifier(ISSUER, AUDIENCE, "ES256", key, 900)
        assert verifier.subject(trust.mint()) == SUBJECT  # the validated file was read
        with pytest.raises(InvalidToken):
            verifier.subject(trust.mint(key=attacker))


def test_key_file_owner_directory_type_and_size_are_checked_on_the_opened_file(
    tmp_path, trust, monkeypatch
):
    data = trust.key_file.read_bytes()
    ok = write_key(tmp_path, "ok.pem", data, 0o600)
    assert load_public_key(ok, "ES256") is not None
    assert tokens._trusted(os.stat("/"))  # root-owned, not group/world-writable: allowed
    # Owned by neither root nor this service's user.
    monkeypatch.setattr(tokens.os, "geteuid", lambda: os.getuid() + 1)
    with pytest.raises(TokenConfigurationError):
        load_public_key(ok, "ES256")
    monkeypatch.undo()
    # A directory others can write to (or rename entries in) is refused.
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o700)
    inside = write_key(shared, "k.pem", data, 0o644)
    for mode in (0o770, 0o707, 0o1777):
        shared.chmod(mode)
        with pytest.raises(TokenConfigurationError):
            load_public_key(inside, "ES256")
    shared.chmod(0o755)
    assert load_public_key(inside, "ES256") is not None
    # A FIFO fails fast instead of blocking startup; a directory is not a key file.
    fifo = tmp_path / "fifo.pem"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(TokenConfigurationError):
        load_public_key(fifo, "ES256")
    with pytest.raises(TokenConfigurationError):
        load_public_key(shared, "ES256")
    # Size limit, enforced on the bytes read: exactly 16 KiB is allowed, one more is not.
    padded = data + b"\n" * (tokens.MAX_KEY_FILE_BYTES - len(data))
    assert load_public_key(write_key(tmp_path, "max.pem", padded), "ES256") is not None
    with pytest.raises(TokenConfigurationError) as caught:
        load_public_key(write_key(tmp_path, "big.pem", padded + b"\n"), "ES256")
    assert str(tmp_path) not in str(caught.value)


@pytest.mark.parametrize("algorithm", ["RS256", "ES256", "EdDSA"])
def test_each_supported_key_type_loads_and_verifies(tmp_path, algorithm):
    private_key = {
        "RS256": lambda: rsa.generate_private_key(public_exponent=65537, key_size=2048),
        "ES256": lambda: ec.generate_private_key(ec.SECP256R1()),
        "EdDSA": ed25519.Ed25519PrivateKey.generate,
    }[algorithm]()
    path = write_key(tmp_path, "key.pem", pem_public(private_key), 0o644)
    key = load_public_key(path, algorithm)
    now = int(time.time())
    token = jwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "sub": SUBJECT, "iat": now, "exp": now + 300},
        private_key,
        algorithm=algorithm,
    )
    assert TokenVerifier(ISSUER, AUDIENCE, algorithm, key, 900).subject(token) == SUBJECT


# --- get_principal and the protected routes ------------------------------------------------


class FakeDatabase:
    """Counts sessions; mirrors Database.session's open/close contract."""

    def __init__(self, fail=False):
        self.opened = self.closed = 0
        self.fail = fail

    @asynccontextmanager
    async def session(self):
        if self.fail:
            raise DatabaseError("Database operation failed.")
        self.opened += 1
        try:
            yield SimpleNamespace()
        finally:
            self.closed += 1


USERS = {SUBJECT: SimpleNamespace(user_id="U-1", active=True)}


@pytest.fixture
def identity(monkeypatch):
    """The user lookup, binding and grants, faked; records every lookup."""
    calls = SimpleNamespace(lookups=[], bindings={}, grants={})

    async def find(session, subject):
        calls.lookups.append(subject)
        return USERS.get(subject)

    async def bind(session, user):
        return calls.bindings.get(user.user_id)

    monkeypatch.setattr(repositories, "find_active_user", find)

    async def granted(session, user):
        return calls.grants.get(user.user_id, frozenset())

    monkeypatch.setattr(repositories, "customer_binding", bind)
    monkeypatch.setattr(repositories, "granted_permissions", granted)
    return calls


def api(trust, database=None, **settings):
    return create_app(trust.settings(**settings), database=database or FakeDatabase())


def call(app, method, path, **kwargs):
    with TestClient(app, raise_server_exceptions=False) as client:
        return client.request(method, path, **kwargs)


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": ""},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer "},
        {"Authorization": "Basic dXNlcjpwYXNz"},
        {"Authorization": "Token abc"},
        {"X-Customer-Id": "CUST-A", "X-Forwarded-User": SUBJECT, "X-User-Id": "U-1"},
    ],
)
def test_missing_or_non_bearer_credentials_get_401_without_lookup(trust, identity, headers):
    database = FakeDatabase()
    app = api(trust, database)
    for method, path in PROTECTED:
        response = call(app, method, path, headers=headers)
        assert response.status_code == 401, path
        assert response.json() == {"detail": "Authentication required."}
        assert response.headers["www-authenticate"] == "Bearer"
    assert identity.lookups == [] and database.opened == 0


def test_cookies_and_query_identity_are_ignored(trust, identity):
    app = api(trust)
    with TestClient(app, cookies={"session": SUBJECT, "access_token": trust.mint()}) as client:
        response = client.get("/api/v1/customers/me", params={"customer_id": "CUST-A", "access_token": trust.mint()})  # fmt: skip
    assert response.status_code == 401 and identity.lookups == []


@pytest.mark.parametrize(
    "token_factory",
    [
        lambda t: t.mint(key=ec.generate_private_key(ec.SECP256R1())),
        lambda t: t.mint(exp=int(time.time()) - 300),
        lambda t: t.mint(aud="other"),
        lambda t: handmade({"alg": "none"}, claims()),
        lambda t: "not-a-jwt",
    ],
)
def test_invalid_tokens_get_401_before_any_database_access(trust, identity, token_factory):
    database = FakeDatabase()
    app = api(trust, database)
    token = token_factory(trust)
    for method, path in PROTECTED:
        response = call(app, method, path, headers=bearer(token))
        assert response.status_code == 401 and response.headers["www-authenticate"] == "Bearer"
        assert response.json() == {"detail": "Authentication required."}
        assert token not in response.text
    assert identity.lookups == [] and database.opened == 0


def test_unknown_and_inactive_users_look_like_bad_tokens(trust, identity):
    app = api(trust)
    for subject in ("auth|nobody", "auth|disabled"):  # the fake returns None for both
        response = call(app, "GET", "/api/v1/customers/me", headers=bearer(trust.mint(sub=subject)))
        assert response.status_code == 401
        assert response.json() == {"detail": "Authentication required."}
    assert identity.lookups == ["auth|nobody", "auth|disabled"]


def test_verified_user_without_a_customer_binding_is_forbidden(trust, identity):
    database = FakeDatabase()
    app = api(trust, database)
    token = trust.mint(customer_id="CUST-A", role="administrator")  # claims are ignored
    for method, path in PROTECTED:
        response = call(app, method, path, headers=bearer(token))
        assert response.status_code == 403 and response.json() == {"detail": "Forbidden."}
    assert database.opened == database.closed == len(PROTECTED)  # one short session each


def test_principal_customer_comes_only_from_the_binding(trust, identity):
    identity.bindings["U-1"] = "CUST-BOUND"
    app = api(trust)
    seen = []

    @app.get("/probe")
    async def probe(principal: Principal = get_principal_dependency()):
        seen.append(principal.customer_id)
        return {}

    token = trust.mint(customer_id="CUST-FORGED", sub=SUBJECT)
    assert call(app, "GET", "/probe", headers=bearer(token)).status_code == 200
    assert seen == ["CUST-BOUND"]


def get_principal_dependency():
    from fastapi import Depends

    return Depends(get_principal)


def test_unconfigured_trust_answers_503_and_authorizes_nothing(identity):
    app = create_app(ApiSettings(environment="development"), database=FakeDatabase())
    for method, path in PROTECTED:
        response = call(app, method, path, headers=bearer("eyJhbGciOiJFUzI1NiJ9.e30.sig"))
        assert response.status_code == 503 and response.json() == {"detail": "Service unavailable."}
        assert call(app, method, path).status_code == 401  # no credentials: still 401
    assert identity.lookups == []


def test_identity_database_failure_is_an_opaque_503(trust, identity):
    app = api(trust, FakeDatabase(fail=True))
    response = call(app, "GET", "/api/v1/customers/me", headers=bearer(trust.mint()))
    assert response.status_code == 503 and response.json() == {"detail": "Service unavailable."}


def test_public_endpoints_need_no_token(trust):
    app = api(trust)
    assert call(app, "GET", "/health/live").status_code == 200
    assert call(app, "GET", "/api/v1/system/info").status_code == 200
    bad = call(app, "GET", "/health/live", headers=bearer("garbage"))
    assert bad.status_code == 200


def test_anonymous_upload_is_rejected_before_any_processing(trust, identity, tmp_path):
    app = api(trust, attachment_dir=tmp_path / "store")
    response = call(
        app, "POST", "/api/v1/rfqs/RFQ-1/attachments",
        files={"file": ("a.pdf", b"%PDF-1.4", "application/pdf")},
        headers=bearer(trust.mint(key=ec.generate_private_key(ec.SECP256R1()))),
    )  # fmt: skip
    assert response.status_code == 401
    assert not (tmp_path / "store").exists() and app.state.write_database is None


def test_validation_422_still_needs_identity_first(trust, identity):
    app = api(trust)
    assert call(app, "GET", "/api/v1/rfqs", params={"limit": "x"}).status_code == 401


@pytest.mark.asyncio
async def test_concurrent_requests_resolve_their_own_identities(trust, identity, monkeypatch):
    identity.bindings.update({"U-1": "CUST-1", "U-2": "CUST-2"})
    USERS_EXTRA = {"auth|s2": SimpleNamespace(user_id="U-2", active=True)}
    original = repositories.find_active_user

    async def slow_find(session, subject):
        await asyncio.sleep(0.01)  # interleave the requests
        return USERS_EXTRA.get(subject) or await original(session, subject)

    monkeypatch.setattr(repositories, "find_active_user", slow_find)
    database = FakeDatabase()
    app = api(trust, database)
    seen = []

    @app.get("/probe")
    async def probe(principal: Principal = get_principal_dependency()):
        seen.append(principal.customer_id)
        return {"customer": principal.customer_id}

    tokens = [trust.mint(), trust.mint(sub="auth|s2"), trust.mint(aud="x")] * 10
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://t") as http:
        responses = await asyncio.gather(*(http.get("/probe", headers=bearer(t)) for t in tokens))
    expected = [(200, "CUST-1"), (200, "CUST-2"), (401, None)] * 10
    got = [(r.status_code, r.json().get("customer")) for r in responses]
    assert got == expected
    assert database.opened == database.closed == 20  # invalid tokens opened none


def test_openapi_declares_the_bearer_scheme(trust):
    schema = create_app(trust.settings()).openapi()
    scheme = schema["components"]["securitySchemes"]["BearerToken"]
    assert scheme == {
        "type": "http",
        "scheme": "bearer",
        "description": "JWT from the configured issuer; its subject must be an active user.",
    }
    assert ISSUER not in json.dumps(schema) and str(trust.key_file) not in json.dumps(schema)


def test_verifier_repr_hides_the_key(trust):
    assert "key=" not in repr(trust.verifier())


def test_verifier_is_immutable(trust):
    with pytest.raises(AttributeError):
        trust.verifier().issuer = "https://evil.invalid/"


def test_eligible_account_statuses_are_real_database_values():
    import re

    from rfq_copilot.models import Customer
    from rfq_copilot.repositories.identity import ELIGIBLE_ACCOUNT_STATUSES

    check = next(c for c in Customer.__table__.constraints if c.name == "ck_customers_rule_01")
    allowed = set(re.findall(r"'([a-z_]+)'", str(check.sqltext)))
    assert ELIGIBLE_ACCOUNT_STATUSES < allowed
    assert allowed - ELIGIBLE_ACCOUNT_STATUSES == {"inactive"}
