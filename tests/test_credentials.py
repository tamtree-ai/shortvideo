"""The `google_service_account` credential type — shape, and what it promises.

These are declaration tests. They exist because the credential *form* is
generated from this spec and nothing else, so a field renamed here silently
changes what the user is asked for and what `google_auth` then fails to find.
"""

from importlib.metadata import EntryPoint

import pytest
from tamtree_plugin_sdk import (
    GROUP_CREDENTIAL_TYPES,
    GROUP_NODES,
    CredentialTypeSpec,
    PluginRegistry,
)
from tamtree_sdk import PluginRefusedError

from tamtree_shortvideo import GOOGLE_SERVICE_ACCOUNT_CREDENTIAL
from tamtree_shortvideo.credentials import CREDENTIAL_TYPE, KEY_FIELD

PLUGIN_NAME = "shortvideo"
CREDENTIAL_ENTRY_POINT = EntryPoint(
    name=CREDENTIAL_TYPE,
    value="tamtree_shortvideo:GOOGLE_SERVICE_ACCOUNT_CREDENTIAL",
    group=GROUP_CREDENTIAL_TYPES,
)


def test_the_spec_is_data_not_an_implementation() -> None:
    """`tamtree.credential_types` is the one family carrying a value rather than
    a class — the registry refuses anything else by name (D-G6)."""
    assert isinstance(GOOGLE_SERVICE_ACCOUNT_CREDENTIAL, CredentialTypeSpec)


def test_one_secret_blob_field_and_no_other() -> None:
    """§5.1's central finding: the form is a loop of single-line `<Input>`s, so
    a split `private_key` field would lose the PEM's newlines on paste. The
    whole file in one field survives that, because its newlines are already
    `\\n`-escaped inside the JSON string.
    """
    fields = GOOGLE_SERVICE_ACCOUNT_CREDENTIAL.fields
    assert [field.name for field in fields] == [KEY_FIELD]
    assert fields[0].secret is True
    assert fields[0].required is True


def test_auth_kind_follows_the_signs_elsewhere_precedent() -> None:
    """`credential_auth_headers` returns `{}` for an `api_key` credential with
    no `api_key` field — the shape `aws_s3` and `aws_sigv4` already use. Only
    `oauth2` is ever branched on, so a bespoke kind would add vocabulary no
    consumer reads."""
    assert GOOGLE_SERVICE_ACCOUNT_CREDENTIAL.auth_kind == "api_key"


def test_it_promises_no_liveness_probe() -> None:
    """Deliberate, and documented as an accepted cost: with no `test_url_field`
    *Test connection* can only say "stored". The node's named errors are where
    a bad key is actually found, so declaring a probe URL here would be a
    promise nothing keeps."""
    assert GOOGLE_SERVICE_ACCOUNT_CREDENTIAL.test_url_field is None


def test_the_type_name_stays_out_of_the_first_party_namespace() -> None:
    """A clash with a shipped credential type is a boot refusal, not a
    precedence — a shadowed type would store a user's secret in a differently
    shaped record than the form they filled in. `google_api` is the shipped
    OAuth credential; this one must not collide with it."""
    assert CREDENTIAL_TYPE == "google_service_account"
    assert CREDENTIAL_TYPE != "google_api"


def test_the_plugin_contributes_the_credential_type_at_boot() -> None:
    registry = PluginRegistry()
    registry.discover(lambda: [CREDENTIAL_ENTRY_POINT])

    assert registry.credential_types()[CREDENTIAL_TYPE] is GOOGLE_SERVICE_ACCOUNT_CREDENTIAL
    assert "credential_type" in registry.plugins()[PLUGIN_NAME].manifest.kinds


def test_an_entry_point_named_anything_else_is_refused() -> None:
    """This group has no `.name` to read off its value, so the registry falls
    back to the entry-point key and refuses a disagreement outright — the form
    would otherwise be unresolvable."""
    registry = PluginRegistry()
    mislabelled = EntryPoint(
        name="google_sa",
        value="tamtree_shortvideo:GOOGLE_SERVICE_ACCOUNT_CREDENTIAL",
        group=GROUP_CREDENTIAL_TYPES,
    )
    with pytest.raises(PluginRefusedError, match="unresolvable"):
        registry.discover(lambda: [mislabelled])


def test_one_distribution_carries_both_contributions() -> None:
    """Nodes and the credential type ship together, as one install: a user who
    has the node but not the type could never fill the form it requires."""
    registry = PluginRegistry()
    registry.discover(
        lambda: [
            EntryPoint(name=PLUGIN_NAME, value="tamtree_shortvideo:NODES", group=GROUP_NODES),
            CREDENTIAL_ENTRY_POINT,
        ]
    )

    plugin = registry.plugins()[PLUGIN_NAME]
    assert set(plugin.manifest.kinds) == {"node", "credential_type"}
    assert CREDENTIAL_TYPE in registry.credential_types()
