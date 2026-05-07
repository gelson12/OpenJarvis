"""Regression test: when config.tools.enabled is empty, the agent must
still get the full integration tool catalog so it can actually CALL
n8n / github / stripe / etc. — not just describe them.

Before the fix at builder.py:_resolve_tools, an empty tools.enabled
defaulted to tools=[]. The agent had nothing to call; user prompts
like "create a workflow" produced curl examples instead of
n8n_create_workflow tool calls.
"""

from __future__ import annotations

from openjarvis.mcp.server import MCPServer


def test_mcp_server_auto_discovers_integration_tools():
    """MCPServer().get_tools() must include every registered integration
    tool. The builder relies on this list as its default-on tool set."""
    tools = MCPServer().get_tools()
    names = {t.spec.name for t in tools}

    integration_prefixes = (
        "n8n_",
        "gh_",
        "vault_",
        "railway_",
        "cloudinary_",
        "v0_",
    )
    found = {n for n in names if n.startswith(integration_prefixes)}
    # We expect at least one tool per integration prefix to exist.
    for prefix in integration_prefixes:
        assert any(n.startswith(prefix) for n in found), (
            f"no tools found with prefix {prefix!r}; "
            f"agent will not be able to call those integrations. "
            f"All discovered names: {sorted(names)}"
        )


def test_mcp_server_includes_email_send():
    """email_send is a critical exact-name tool (not prefixed) — must
    survive auto-discovery."""
    tools = MCPServer().get_tools()
    names = {t.spec.name for t in tools}
    assert "email_send" in names, (
        f"email_send missing; got {sorted(names)}"
    )


def test_mcp_server_has_at_least_25_tools():
    """Sanity check: a fully-loaded MCPServer should expose 25+ tools
    (built-ins + storage + channel + every integration). If this drops
    to single digits, an integration import is probably failing
    silently in tools/__init__.py."""
    tools = MCPServer().get_tools()
    assert len(tools) >= 25, (
        f"only {len(tools)} tools registered — expected 25+. "
        f"Likely cause: an integration tool module is failing to "
        f"import. Discovered: {sorted(t.spec.name for t in tools)}"
    )
