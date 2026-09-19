"""Shared fixtures — chiefly a real RSA key, generated once.

The token mint signs for real in these tests rather than against a stubbed
signer: an RS256 path that is never exercised with a genuine PEM would let a
key-handling bug live behind green tests, and key *handling* is the whole
review surface of V1.1.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

CLIENT_EMAIL = "shorts@example-project.iam.gserviceaccount.com"
PROJECT_ID = "example-project"


@pytest.fixture(scope="session")
def private_key_pem() -> str:
    """A throwaway 2048-bit RSA key in the PEM form a key file carries.

    Session-scoped because generating one costs more than every test that uses
    it put together.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")


@pytest.fixture(scope="session")
def key_file(private_key_pem: str) -> dict[str, Any]:
    """The JSON key file Google hands out, field for field."""
    return {
        "type": "service_account",
        "project_id": PROJECT_ID,
        "private_key_id": "0123456789abcdef0123456789abcdef01234567",
        "private_key": private_key_pem,
        "client_email": CLIENT_EMAIL,
        "client_id": "123456789012345678901",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_x509_cert_url": (
            "https://www.googleapis.com/robot/v1/metadata/x509/"
            "shorts%40example-project.iam.gserviceaccount.com"
        ),
        "universe_domain": "googleapis.com",
    }


@pytest.fixture
def credential_payload(key_file: dict[str, Any]) -> dict[str, str]:
    """What `ctx.credential("google_service_account")` returns: one blob field,
    pretty-printed exactly as the console downloads it."""
    return {"service_account_json": json.dumps(key_file, indent=2)}


@pytest.fixture(autouse=True)
def _clean_token_cache() -> Any:
    """The mint cache is module state and outlives a test; a leaked token would
    make the next test's cold-cache assertion pass for the wrong reason."""
    from tamtree_shortvideo.google_auth import clear_token_cache

    clear_token_cache()
    yield
    clear_token_cache()
