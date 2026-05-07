"""Tests for the n8n credentials tools (list / get / list_types).

Mocks N8NClient so no real n8n calls are made; verifies tool surface
only.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from openjarvis.integrations.n8n import N8NClient, N8NUnavailableError
from openjarvis.tools.n8n_tools import (
    N8NGetCredentialTool,
    N8NListCredentialTypesTool,
    N8NListCredentialsTool,
)


def test_list_credentials_returns_metadata():
    fake = MagicMock(spec=N8NClient)
    fake.list_credentials.return_value = {
        "data": [
            {"id": "cred-1", "name": "My Slack", "type": "slackApi"},
            {"id": "cred-2", "name": "Personal Gmail", "type": "gmailOAuth2"},
        ]
    }
    out = N8NListCredentialsTool(client=fake).execute(limit=50)
    assert out.success is True
    assert "slackApi" in out.content
    assert "gmailOAuth2" in out.content
    fake.list_credentials.assert_called_once_with(limit=50)


def test_get_credential_passes_id():
    fake = MagicMock(spec=N8NClient)
    fake.get_credential.return_value = {"id": "cred-1", "name": "My Slack"}
    out = N8NGetCredentialTool(client=fake).execute(credential_id="cred-1")
    assert out.success is True
    fake.get_credential.assert_called_once_with("cred-1")


def test_list_credential_types_returns_schema():
    fake = MagicMock(spec=N8NClient)
    fake.list_credential_types.return_value = ["slackApi", "stripeApi"]
    out = N8NListCredentialTypesTool(client=fake).execute()
    assert out.success is True
    assert "slackApi" in out.content


def test_credentials_tool_surfaces_unavailable_as_error():
    fake = MagicMock(spec=N8NClient)
    fake.list_credentials.side_effect = N8NUnavailableError("no key")
    out = N8NListCredentialsTool(client=fake).execute()
    assert out.success is False
    assert "n8n error" in out.content


def test_no_credential_tool_requires_confirmation():
    """Read-only credential lookups should NOT trigger confirmation —
    they don't expose secrets and don't change state."""
    fake = MagicMock(spec=N8NClient)
    for tool_cls in (
        N8NListCredentialsTool,
        N8NGetCredentialTool,
        N8NListCredentialTypesTool,
    ):
        spec = tool_cls(client=fake).spec
        assert spec.requires_confirmation is False, (
            f"{tool_cls.__name__} marked confirmation-gated; should be read-only"
        )
