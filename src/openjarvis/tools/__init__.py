"""Tools primitive — tool system with ABC interface and built-in tools."""

from __future__ import annotations

from openjarvis.tools._stubs import BaseTool, ToolExecutor, ToolSpec

# Import built-in tools to trigger @ToolRegistry.register() decorators.
# Each is wrapped in try/except so the package loads even before the
# individual tool modules are created.
try:
    import openjarvis.tools.calculator  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.think  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.retrieval  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.llm_tool  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.file_read  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.web_search  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.code_interpreter  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.code_interpreter_docker  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.repl  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.storage_tools  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.mcp_adapter  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.channel_tools  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.http_request  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.shell_exec  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.memory_manage  # noqa: F401
except ImportError:
    pass
try:
    import openjarvis.tools.user_profile_manage  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.skill_manage  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.file_write  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.apply_patch  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.git_tool  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.db_query  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.pdf_tool  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.image_tool  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.audio_tool  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.knowledge_tools  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.text_to_speech  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.digest_collect  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.integrations_check  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.self_introspect  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.weather_tools  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.geocode_tools  # noqa: F401
except ImportError:
    pass

# NOTE: web_search_tools.py was a duplicate of the existing web_search.py
# (which already registers 'web_search' with Tavily+DDG fallback). The
# duplicate registration crashed startup with "ToolRegistry already has
# an entry for 'web_search'". Existing tool stays canonical; the
# free-only DDG variant is shelved for now.

# Integration tool surfaces (Obsidian vault, n8n, Railway, GitHub,
# Cloudinary, V0, SMTP). Each registers its BaseTool subclasses via
# @ToolRegistry.register and is gated on the relevant env-vars +
# optional dependencies — soft-skip if anything is missing.
try:
    import openjarvis.tools.obsidian_vault_tools  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.n8n_tools  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.email_tools  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.railway_tools  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.github_tools  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.cloudinary_tools  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.v0_tools  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.stripe_tools  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.paypal_tools  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.calendar_tools  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.gmail_tools  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.outlook_tools  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.browser_tools  # noqa: F401
except ImportError:
    pass

# Desktop control — operate the user's Windows machines (laptop + ROG) over
# a LiveKit data-channel bridge. Soft-skips if the `livekit` realtime SDK is
# not installed (it lives in the pyproject `server` extra).
try:
    import openjarvis.tools.desktop_bridge  # noqa: F401
except ImportError:
    pass

# OpenCTI — Jarvis's intelligence / investigation layer (search the graph,
# log observables, open incidents, link entities, summarise). Soft-skips
# if httpx is not installed.
try:
    import openjarvis.tools.opencti  # noqa: F401
except ImportError:
    pass

__all__ = ["BaseTool", "ToolExecutor", "ToolSpec"]
