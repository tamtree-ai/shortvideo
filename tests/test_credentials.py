"""`google_service_account` — core's type now, named here and no longer contributed.

The type moved into Tamtree core (sheet-records, contracts 2.10.0) with this
plugin's shape unchanged. Contributing it as well would be a boot refusal (a
plugin type that clashes with a built-in), so these tests pin the two halves
of the move: the plugin stops contributing it, and the field the token mint
reads is still the one core's form asks for.
"""

from importlib.metadata import entry_points

from tamtree_plugin_sdk import GROUP_CREDENTIAL_TYPES

from tamtree_shortvideo.credentials import CREDENTIAL_TYPE, KEY_FIELD


def test_the_plugin_no_longer_contributes_the_type() -> None:
    contributed = {
        ep.name
        for ep in entry_points(group=GROUP_CREDENTIAL_TYPES)
        if ep.value.startswith("tamtree_shortvideo:")
    }
    assert CREDENTIAL_TYPE not in contributed


def test_the_field_is_the_one_core_asks_for() -> None:
    """Core's `google_service_account` has one secret field with this name; a
    rename on either side would leave every saved key unreadable here."""
    assert CREDENTIAL_TYPE == "google_service_account"
    assert KEY_FIELD == "service_account_json"
