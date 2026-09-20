"""The `google_service_account` credential type — data, not code (D-G6).

**Why a new type rather than reusing `google_api`.** The shipped `google_api`
credential is an OAuth *authorization-code* credential: it carries a client id,
a client secret and a user's refresh token, and it exists so a workspace can act
on behalf of a person's Gmail or Sheets. A service account is the opposite
shape — no user, no consent screen, a private key the machine signs with. Piling
a PEM into `google_api` would make the connect flow nonsense and would hand
every existing Google node a payload it cannot interpret.

**Why one blob field rather than three.** The credential form is a plain loop
over this spec rendering `<Input type={field.secret ? 'password' : 'text'}>`
(`packages/frontend/src/pages/CredentialFields.tsx @ 90e82780`) — there is no
textarea, and an `<input>` strips newlines on paste. A separate `private_key`
field would therefore silently destroy the PEM the user pasted. The downloaded
key *file* survives that same paste intact, because its PEM newlines are already
`\\n`-escaped inside a JSON string. So the field that looks lazier is the only
one that works.

**Why `auth_kind="api_key"`.** `credential_auth_headers`
(`packages/sdk/tamtree_sdk/http_client.py:34-77 @ 90e82780`) returns `{}` for an
`api_key` credential that carries no `api_key` field, precisely because the kind
is broader than the field — `aws_s3` and `aws_sigv4` share it and sign
elsewhere. A service account signs elsewhere too. Only `oauth2` is ever branched
on anywhere, so inventing a `service_account` kind would add vocabulary no
consumer reads (§5.1).

**What this type gives up, deliberately.** With no `test_url_field`, *Test
connection* can only ever answer `"stored — add a test URL to probe live
connectivity"` (`packages/server/tamtree_server/credentials.py:222-229`), and a
real probe is impossible anyway because the probe builds its headers through
`credential_auth_headers`, which cannot mint a Google token. Liveness is
therefore proven in exactly one place: a loud, named error from the node — see
`google_auth.ServiceAccountKeyError` and `TokenMintError`.
"""

from typing import Final

from tamtree_plugin_sdk import CredentialFieldSpec, CredentialTypeSpec

__all__ = [
    "CREDENTIAL_TYPE",
    "GOOGLE_SERVICE_ACCOUNT_CREDENTIAL",
    "KEY_FIELD",
    "MINIMAX_API_CREDENTIAL",
    "MINIMAX_CREDENTIAL_TYPE",
    "MINIMAX_DEFAULT_TEST_URL",
    "MINIMAX_TOKEN_FIELD",
    "OPENROUTER_API_CREDENTIAL",
    "OPENROUTER_CREDENTIAL_TYPE",
    "OPENROUTER_DEFAULT_TEST_URL",
    "OPENROUTER_TOKEN_FIELD",
]

#: The registered type name. Must equal the entry-point name or the registry
#: refuses the boot — one constant so the spec, the manifest, the node
#: requirements and the tests cannot drift.
CREDENTIAL_TYPE: Final = "google_service_account"

#: The single field, named once for the same reason.
KEY_FIELD: Final = "service_account_json"


GOOGLE_SERVICE_ACCOUNT_CREDENTIAL: Final = CredentialTypeSpec(
    type=CREDENTIAL_TYPE,
    display_name="Google service account",
    description=(
        "The whole JSON key file downloaded from a Google Cloud service "
        "account. Used to mint access tokens for Google Cloud APIs such as "
        "Text-to-Speech. Grant the account the narrowest role that works, in a "
        "project used for nothing else — the OAuth scope cannot narrow it."
    ),
    auth_kind="api_key",
    fields=[
        CredentialFieldSpec(
            name=KEY_FIELD,
            label="Service account key (JSON)",
            secret=True,
            required=True,
            placeholder='{"type": "service_account", "project_id": "…", …}',
        )
    ],
)


# -- MiniMax (V2.1) ----------------------------------------------------------
#
# **Why its own type rather than the generic `api_key`.** The credential picker
# on a node shows type names, and "API key" on a canvas holding three vendors
# tells the author nothing about which of their keys belongs in which slot —
# the plan's V2.1 says so in as many words. A named type also lets both MiniMax
# nodes *require* it, so the engine refuses to dispatch a step with no binding
# instead of failing inside the node against a header that was never set.
#
# **Why `auth_kind="bearer"` and a field literally called `token`.** Unlike the
# Google credential, this one carries a plain bearer token, so the shipped
# `credential_auth_headers` can build its header — and that function reads the
# field named `token` for this kind and no other name
# (`packages/sdk/tamtree_sdk/http_client.py:55-58 @ 90e82780`). Calling the
# field `api_key` would have produced a credential that stores fine, tests as
# "connected" never, and authenticates nothing.
#
# **Why it *can* be probed when the Google one cannot.** `test_connection`
# builds its headers through that same function, so a bearer credential is
# testable for real. What it needs is a URL that answers 200 for a valid key
# and costs nothing — see `MINIMAX_DEFAULT_TEST_URL`.

MINIMAX_CREDENTIAL_TYPE: Final = "minimax_api"

MINIMAX_TOKEN_FIELD: Final = "token"

#: The OpenAI-compatible model listing. Chosen as the probe because it is
#: metadata rather than inference: it generates nothing, so it cannot bill for
#: a generation. MiniMax's docs do not state a price for it either way, so that
#: is a reasoned choice and not a quoted guarantee — which is the honest
#: version of the plan's "non-billable probe". The field carries a default so
#: the credential tests out of the box, and stays editable for a deployment on
#: a different MiniMax region.
MINIMAX_DEFAULT_TEST_URL: Final = "https://api.minimax.io/v1/models"


MINIMAX_API_CREDENTIAL: Final = CredentialTypeSpec(
    type=MINIMAX_CREDENTIAL_TYPE,
    display_name="MiniMax API",
    description=(
        "An API key from the MiniMax platform (Account Management → API Keys), "
        "used to generate video clips. Video generation is billed per second of "
        "output — give this key to a project whose spend you are watching."
    ),
    auth_kind="bearer",
    fields=[
        CredentialFieldSpec(
            name=MINIMAX_TOKEN_FIELD,
            label="API key",
            secret=True,
            required=True,
            placeholder="eyJhbGciOi…",
        ),
        CredentialFieldSpec(
            name="test_url",
            label="Test URL",
            secret=False,
            required=False,
            default=MINIMAX_DEFAULT_TEST_URL,
            placeholder=MINIMAX_DEFAULT_TEST_URL,
        ),
    ],
    test_url_field="test_url",
)


# -- OpenRouter (V0.2b: `openrouter_tts`) ------------------------------------
#
# **Why its own type rather than reusing `minimax_api`'s shape.** Same reasoning
# as MiniMax's own type: a bearer token on a canvas holding three vendors is
# unidentifiable by type name alone. `auth_kind="bearer"` and a field named
# `token` are copied from that precedent for the same reason — the shipped
# `credential_auth_headers` reads that exact field name for that exact kind
# (`packages/sdk/tamtree_sdk/http_client.py:55-58`), and no other name
# authenticates anything.
#
# **Why it can be probed.** `GET /api/v1/models` is metadata — it lists what
# OpenRouter serves, generates nothing, and OpenRouter's own docs say a failed
# or non-generating call is not billed. Same reasoning as MiniMax's model
# listing, applied to a different vendor.

OPENROUTER_CREDENTIAL_TYPE: Final = "openrouter_api"

OPENROUTER_TOKEN_FIELD: Final = "token"

OPENROUTER_DEFAULT_TEST_URL: Final = "https://openrouter.ai/api/v1/models"


OPENROUTER_API_CREDENTIAL: Final = CredentialTypeSpec(
    type=OPENROUTER_CREDENTIAL_TYPE,
    display_name="OpenRouter API",
    description=(
        "An API key from openrouter.ai (Settings → Keys), used to synthesize "
        "narration through Gemini 3.1 Flash TTS. Billed per token by OpenRouter's "
        "own ledger — give this key to a project whose spend you are watching."
    ),
    auth_kind="bearer",
    fields=[
        CredentialFieldSpec(
            name=OPENROUTER_TOKEN_FIELD,
            label="API key",
            secret=True,
            required=True,
            placeholder="sk-or-v1-…",
        ),
        CredentialFieldSpec(
            name="test_url",
            label="Test URL",
            secret=False,
            required=False,
            default=OPENROUTER_DEFAULT_TEST_URL,
            placeholder=OPENROUTER_DEFAULT_TEST_URL,
        ),
    ],
    test_url_field="test_url",
)
