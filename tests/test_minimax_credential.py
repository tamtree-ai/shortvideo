"""The `minimax_api` credential — the one in this plugin that can be probed.

V2.1's requirements are small but each exists for a reason the code cannot
state on its own: a named type so the picker is unambiguous on a canvas
holding three vendors, a field name the shipped header builder actually reads,
and a probe URL that answers without generating anything.
"""

from __future__ import annotations

from tamtree_plugin_sdk import CredentialTypeSpec
from tamtree_sdk.http_client import credential_auth_headers

from tamtree_shortvideo.credentials import (
    GOOGLE_SERVICE_ACCOUNT_CREDENTIAL,
    MINIMAX_API_CREDENTIAL,
    MINIMAX_CREDENTIAL_TYPE,
    MINIMAX_DEFAULT_TEST_URL,
    MINIMAX_PRICE_FIELD,
    MINIMAX_TOKEN_FIELD,
)
from tamtree_shortvideo.minimax import API_HOST

TOKEN = "eyJ-a-real-looking-minimax-key"


def test_it_is_its_own_type_not_a_generic_api_key() -> None:
    """V2.1: "Do not reuse a generic API-key field that is ambiguous in the
    credential picker." On a canvas with Google and MiniMax slots, "API key"
    names neither."""
    assert MINIMAX_API_CREDENTIAL.type == MINIMAX_CREDENTIAL_TYPE == "minimax_api"
    assert MINIMAX_API_CREDENTIAL.type != GOOGLE_SERVICE_ACCOUNT_CREDENTIAL.type
    assert isinstance(MINIMAX_API_CREDENTIAL, CredentialTypeSpec)


def test_the_shipped_header_builder_can_actually_authenticate_it() -> None:
    """The load-bearing test. `credential_auth_headers` reads the field named
    `token` for `auth_kind="bearer"` and no other name — a field called
    `api_key` would store fine, probe as connected never, and authenticate
    nothing."""
    headers = credential_auth_headers(
        MINIMAX_CREDENTIAL_TYPE,
        {MINIMAX_TOKEN_FIELD: TOKEN},
        auth_kind=MINIMAX_API_CREDENTIAL.auth_kind,
    )

    assert headers == {"Authorization": f"Bearer {TOKEN}"}


def test_the_key_is_stored_as_a_secret_and_required() -> None:
    (field,) = [f for f in MINIMAX_API_CREDENTIAL.fields if f.name == MINIMAX_TOKEN_FIELD]

    assert field.secret is True
    assert field.required is True


def test_the_rate_is_required_and_has_no_default() -> None:
    """The install-time gate. `credentials_api.py:168 @ d73c2d3e` refuses a
    create with any required field blank — secret or not — so this is what
    makes an operator answer the rate question before a key can be stored.

    `default` must stay `None`: a default is filled in on create for a blank
    field (`packages/sdk/tamtree_sdk/credential_types.py:33-38`), which would
    hand back exactly the silent zero this field exists to remove."""
    (field,) = [f for f in MINIMAX_API_CREDENTIAL.fields if f.name == MINIMAX_PRICE_FIELD]

    assert field.required is True
    assert field.default is None
    # Not a secret: it is a commercial fact, and a password input would hide
    # the operator's own typo from them.
    assert field.secret is False
    # The form renders `label` and `placeholder` and nothing else, so the
    # label has to carry the one thing an operator cannot guess — that 0 is a
    # permitted answer with a consequence.
    assert "0" in field.label


def test_the_rate_is_not_a_field_the_header_builder_can_confuse_for_the_key() -> None:
    """A second non-secret field on a bearer credential must not change what
    authenticates it."""
    headers = credential_auth_headers(
        MINIMAX_CREDENTIAL_TYPE,
        {MINIMAX_TOKEN_FIELD: TOKEN, MINIMAX_PRICE_FIELD: "0.13"},
        auth_kind=MINIMAX_API_CREDENTIAL.auth_kind,
    )

    assert headers == {"Authorization": f"Bearer {TOKEN}"}


def test_it_ships_a_probe_url_so_test_connection_says_something_real() -> None:
    """Unlike the Google credential — which can only ever answer "stored",
    because no probe can mint a service-account token — this one can be tested
    for real. The default means it works without the author finding a URL."""
    assert MINIMAX_API_CREDENTIAL.test_url_field == "test_url"
    (field,) = [f for f in MINIMAX_API_CREDENTIAL.fields if f.name == "test_url"]
    assert field.default == MINIMAX_DEFAULT_TEST_URL
    assert field.required is False
    assert field.secret is False
    assert GOOGLE_SERVICE_ACCOUNT_CREDENTIAL.test_url_field is None


def test_the_probe_generates_nothing() -> None:
    """A model listing is metadata, not inference, so it cannot bill for a
    generation — which is the honest version of "non-billable", since MiniMax
    states no price for it either way."""
    assert MINIMAX_DEFAULT_TEST_URL.startswith(API_HOST)
    assert MINIMAX_DEFAULT_TEST_URL.endswith("/v1/models")
    assert "video_generation" not in MINIMAX_DEFAULT_TEST_URL


def test_the_probe_stays_on_the_host_the_manifest_declares() -> None:
    """An egress allowlist is only worth something while it is complete, and a
    default probe pointing somewhere undeclared would make it incomplete on a
    fresh install."""
    assert MINIMAX_DEFAULT_TEST_URL.split("/")[2] == API_HOST.split("/")[2]
