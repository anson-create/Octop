"""Where the Jira credential lives, and what it looks like once stored.

The split matters: an integration row (``mapping_json``) is configuration and
lands in every database dump; the credential belongs in the secret store. The
account name is part of the credential, not part of the configuration — an
instance either needs both or neither.
"""

from __future__ import annotations

import json

from octop.infra.pm.ports.jira import JiraClient


def test_username_is_stored_beside_the_password_and_not_in_the_mapping(service, workspace) -> None:
    workspace_id = workspace["id"]

    status = service.save_jira_config(
        workspace_id,
        base_url="http://jira.example.com/",
        token="pw-secret",
        username="anson",
    )

    assert status["configured"] is True
    assert status["base_url"] == "http://jira.example.com"
    assert status["auth"] == "basic"
    assert status["token_set"] is True

    mapping = service._jira_mapping(workspace_id)  # noqa: SLF001 - asserting storage shape
    assert "username" not in mapping
    assert "pw-secret" not in json.dumps(mapping)


def test_a_password_change_keeps_the_stored_username(service, workspace) -> None:
    workspace_id = workspace["id"]
    service.save_jira_config(
        workspace_id, base_url="http://jira.example.com", token="old", username="anson"
    )

    # Rotating without repeating the account name must not silently drop it.
    service.save_jira_config(workspace_id, base_url="http://jira.example.com", token="new")

    assert service._jira_credential(workspace_id) == ("anson", "new")  # noqa: SLF001


def test_the_client_built_from_config_uses_the_stored_scheme(service, workspace) -> None:
    workspace_id = workspace["id"]
    service.save_jira_config(
        workspace_id,
        base_url="http://jira.example.com",
        token="pw-secret",
        username="anson",
    )

    client = service.jira_client(workspace_id)

    assert isinstance(client, JiraClient)
    assert client.username == "anson"
    assert client.base_url == "http://jira.example.com"


def test_a_pat_only_install_still_reads_as_bearer(service, workspace) -> None:
    workspace_id = workspace["id"]

    status = service.save_jira_config(
        workspace_id, base_url="http://jira.example.com", token="pat-token"
    )

    assert status["auth"] == "bearer"
    assert service._jira_credential(workspace_id) == ("", "pat-token")  # noqa: SLF001


def test_an_instance_configured_before_this_change_keeps_working(service, workspace) -> None:
    """The legacy key held a bare token. Reading it back must not need a re-entry."""

    workspace_id = workspace["id"]
    service.save_jira_config(workspace_id, base_url="http://jira.example.com", token="pat-token")

    secrets = service._jira_secrets()  # noqa: SLF001
    # Old layout: bare token under the token key, and nothing readable under the
    # credential key (a pre-migration row that is not the new JSON shape).
    secrets.get_or_create(service._jira_token_key(workspace_id), lambda: b"legacy-token")  # noqa: SLF001
    secrets.rotate(service._jira_credential_key(workspace_id), b"legacy-not-json")  # noqa: SLF001

    assert service._jira_credential(workspace_id) == ("", "legacy-token")  # noqa: SLF001
    assert service.jira_status(workspace_id)["configured"] is True
