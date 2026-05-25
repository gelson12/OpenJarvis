"""Route handlers for the OpenAI-compatible API server."""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from openjarvis.core.types import Message, Role
from openjarvis.server.models import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatCompletionResponse,
    Choice,
    ChoiceMessage,
    ComplexityInfo,
    DeltaMessage,
    ModelListResponse,
    ModelObject,
    StreamChoice,
    UsageInfo,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# Auto-inject integration tools when the frontend doesn't send any
# ---------------------------------------------------------------------------

# Tools whose names start with these prefixes (or match these exact names)
# get auto-injected into chat completion requests when req.tools is empty.
# This covers the user-facing integrations (n8n, Obsidian vault, GitHub,
# Railway, Cloudinary, V0, SMTP) — the ones a model would actually want
# to call from a "list my workflows / make me a thing" prompt. Internal
# tools (calculator, file_read, retrieval) are left out to keep the
# system prompt small and the model focused.
_AUTO_INJECT_PREFIXES = (
    "vault_",
    "obsidian_",
    "n8n_",
    "gh_",
    "railway_",
    "cloudinary_",
    "v0_",
    "stripe_",
    "paypal_",
    "calendar_",
    "gmail_",
    "outlook_",
    "browser_",
)
_AUTO_INJECT_EXACT = ("email_send",)

_AUTO_INJECT_TOOLS_CACHE: list[dict] | None = None
# Per-group cache: group_id -> list of OpenAI function specs. Built lazily
# on first request so we don't pay the registry-walk cost at import time.
_AUTO_INJECT_TOOLS_BY_GROUP: dict[str, list[dict]] | None = None


# Tool groups and their trigger keywords. A user message that contains any
# of a group's keywords (case-insensitive substring match) enables that
# group's tools for the request. The substring check is intentionally loose
# — if the model needs the tool and the keyword check missed, the recent-
# tool stickiness window catches it on the next turn. False positives are
# cheap (a few extra tools in the spec) compared to false negatives
# (model can't see the tool it actually needs).
#
# Order matters only for the cap-truncation step at the end: groups are
# enabled in dict-insertion order, and if the resulting tool list exceeds
# _RELEVANCE_CAP the trailing groups get dropped first.
_TOOL_GROUP_TRIGGERS: dict[str, tuple[str, ...]] = {
    # Email — gmail and outlook share most generic mail keywords on
    # purpose. The user has both accounts wired up; the model picks the
    # right tool based on whichever account the message references.
    "gmail": (
        "gmail", "google mail", "@gmail",
        "email", "mail", "inbox", "draft", "compose", "subject",
        "send a message", "reply to", "forward",
    ),
    "outlook": (
        "outlook", "office 365", "office365", "ms graph", "microsoft 365",
        "@hotmail", "@outlook", "@live",
        "email", "mail", "inbox", "draft", "compose", "subject",
        "send a message", "reply to", "forward",
    ),
    "calendar": (
        "calendar", "schedule", "meeting", "appointment", "event",
        "agenda", "free/busy", "freebusy", "availability",
        "what's on my", "what is on my", "today's", "tomorrow's",
        "this week", "next week",
    ),
    "n8n": (
        "n8n", "workflow", "workflows", "automation", "automate",
        "trigger", "webhook", "cron", "wire up", "every time",
        "credential", "credentials",
    ),
    "browser": (
        "browser", "navigate", "scrape", "open the website",
        "open page", "click on", "fill in", "screenshot", "web page",
        "url", "headless",
    ),
    "github": (
        "github", "pull request", " pr ", " pr.", "issue", "issues",
        "repo", "repos", "repository", "commit", "branch",
        "actions run", "release", "fork",
    ),
    "railway": (
        "railway", "deploy", "redeploy", "env var", "environment variable",
        "service logs", "cf 1010",
    ),
    "stripe": (
        "stripe", "charge", "charges", "subscription", "subscriptions",
        "refund", "revenue", "balance",
    ),
    "paypal": (
        "paypal", "payout", "payouts",
    ),
    "cloudinary": (
        "cloudinary", "upload an image", "image upload", "cdn",
    ),
    "v0": (
        "v0", "vercel preview", "design a ui", "generate a website",
        "build a landing page",
    ),
    "vault": (
        "vault", "obsidian", "my notes", "search the notes",
        "knowledge base", "second brain", "backlinks",
    ),
    "weather": (
        "weather", "temperature", "forecast", "rain", "rainy",
        "snow", "snowing", "sunny", "cloudy", "humidity", "wind",
        "is it going to", "what's it like outside", "degrees",
    ),
    "location": (
        "where is", "address", "geocode", "coordinates", "lat",
        "longitude", "latitude", "find the place", "find a place",
        "directions", "nearby",
    ),
    "desktop": (
        "my laptop", "the laptop", "my rog", "the rog", "my pc",
        "my desktop", "my machine", "my computer", "on the laptop",
        "on the rog", "on my", "open notepad", "run command",
        "list the folder", "list the directory", "read the file",
    ),
    "opencti": (
        "opencti", "cti", "intel", "intelligence", "threat",
        "threats", "observable", "observables", "indicator",
        "indicators", "incident", "incidents", "investigate",
        "investigation", "adversary", "actor", "threat actor",
        "ioc", "iocs", "stix", "kill chain", "campaign", "malware",
        "suspicious domain", "suspicious ip", "suspicious url",
        "log the domain", "log this", "phishing", "breach",
        "indicators of compromise",
    ),
    "web_search": (
        "search the web", "google", "google it", "look up", "look it up",
        "what is", "who is", "when did", "find out about",
        "current news", "latest", "recent", "today's news",
    ),
}

# Map tool name → group_id. Built once on first request from the registry.
_TOOL_NAME_TO_GROUP: dict[str, str] | None = None

# Maximum tools sent in a single request. The auto-inject catalog is 100+
# tools; sending all of them costs ~40 K input tokens per turn AND degrades
# tool-call accuracy (every major frontier model gets confused past
# ~20–30 tools). Anthropic / OpenAI guidance lands at 15–25 as the sweet
# spot; we pick the upper end so multi-domain prompts still fit.
_RELEVANCE_CAP = 25

# How many recent assistant turns we scan for tool_calls when deciding
# which groups to keep "sticky". Without this window, the second turn of a
# multi-step interaction (model called gmail_list_messages, now wants to
# call gmail_get_message) could lose access to gmail tools if the user's
# follow-up didn't repeat any keyword.
_RECENT_TOOL_HISTORY_TURNS = 4


def _build_group_caches() -> tuple[dict[str, list[dict]], dict[str, str]]:
    """Walk ToolRegistry once, build group → specs and name → group maps."""
    from openjarvis.core.registry import ToolRegistry

    by_group: dict[str, list[dict]] = {g: [] for g in _TOOL_GROUP_TRIGGERS}
    name_to_group: dict[str, str] = {}

    # Map prefix → group_id so we can route each tool name into the right
    # bucket. Single-direction map; "vault_" and "obsidian_" both go to
    # "vault" (they share the Obsidian-vault backend).
    prefix_to_group: dict[str, str] = {
        "gmail_": "gmail",
        "outlook_": "outlook",
        "calendar_": "calendar",
        "n8n_": "n8n",
        "browser_": "browser",
        "gh_": "github",
        "railway_": "railway",
        "stripe_": "stripe",
        "paypal_": "paypal",
        "cloudinary_": "cloudinary",
        "v0_": "v0",
        "vault_": "vault",
        "obsidian_": "vault",
        "weather_": "weather",
        "geocode_": "location",
        "desktop_": "desktop",
        "opencti_": "opencti",
        "web_search": "web_search",
    }
    exact_to_group: dict[str, str] = {
        "email_send": "gmail",  # legacy SMTP shim — surface alongside gmail
    }

    for name, tool_cls in ToolRegistry.items():
        group: str | None = None
        for prefix, gid in prefix_to_group.items():
            if name.startswith(prefix):
                group = gid
                break
        if group is None:
            group = exact_to_group.get(name)
        if group is None:
            continue
        try:
            spec = tool_cls().to_openai_function()
        except Exception:
            continue
        by_group[group].append(spec)
        name_to_group[name] = group

    return by_group, name_to_group


def _ensure_caches() -> tuple[dict[str, list[dict]], dict[str, str]]:
    global _AUTO_INJECT_TOOLS_BY_GROUP, _TOOL_NAME_TO_GROUP
    if _AUTO_INJECT_TOOLS_BY_GROUP is None or _TOOL_NAME_TO_GROUP is None:
        by_group, name_to_group = _build_group_caches()
        _AUTO_INJECT_TOOLS_BY_GROUP = by_group
        _TOOL_NAME_TO_GROUP = name_to_group
    return _AUTO_INJECT_TOOLS_BY_GROUP, _TOOL_NAME_TO_GROUP


def _detect_groups_from_text(text: str) -> set[str]:
    """Return the set of group_ids whose triggers match anywhere in text."""
    if not text:
        return set()
    lc = text.lower()
    hits: set[str] = set()
    for gid, triggers in _TOOL_GROUP_TRIGGERS.items():
        for kw in triggers:
            if kw in lc:
                hits.add(gid)
                break
    return hits


def _detect_groups_from_history(messages, name_to_group: dict[str, str]) -> set[str]:
    """Return groups whose tools were called in the recent history window.

    Looks at the last ``_RECENT_TOOL_HISTORY_TURNS`` messages for any
    ``tool_calls`` field and pulls the group of each invoked tool. This
    keeps multi-step interactions working even when the user's latest
    turn doesn't repeat the trigger keyword.
    """
    if not messages:
        return set()
    hits: set[str] = set()
    for m in list(messages)[-_RECENT_TOOL_HISTORY_TURNS:]:
        tool_calls = getattr(m, "tool_calls", None)
        if not tool_calls:
            continue
        for tc in tool_calls:
            fn = (tc or {}).get("function") or {}
            tool_name = fn.get("name") or ""
            gid = name_to_group.get(tool_name)
            if gid:
                hits.add(gid)
    return hits


def _get_always_on_tools() -> list[dict]:
    """Tools that the agent should ALWAYS see, regardless of relevance.

    Currently just ``integrations_check`` — a zero-cost env-introspection
    tool that lets the agent answer "is integration X configured?"
    without bothering the user. Critical because without it, the agent's
    default failure mode is "please set OUTLOOK_CLIENT_ID..." even when
    the variable is already set in production. Adds ~300 tokens to the
    request, prevents far worse generic responses.
    """
    from openjarvis.core.registry import ToolRegistry

    out: list[dict] = []
    for name in (
        "integrations_check",
        # Self-introspection: lets the agent investigate its own service
        # (env vars, source tree, source files) instead of assuming
        # what's set or how it's wired. Without these, the agent's
        # default failure mode for any "why doesn't X work?" question
        # is a generic walkthrough; with them, it can grep its own code
        # and check its own env to give a precise answer.
        "env_introspect",
        "source_grep",
        "source_read",
    ):
        tool_cls = ToolRegistry.get(name)
        if tool_cls is None:
            continue
        try:
            out.append(tool_cls().to_openai_function())
        except Exception:
            continue
    return out


def _select_relevant_tools(query_text: str, messages) -> tuple[list[dict], list[str]]:
    """Pick the auto-inject tools relevant to this turn.

    Returns (tools_list, enabled_groups) so the caller can log which
    groups the relevance filter chose — useful for tuning the trigger
    keywords against real traffic.

    Selection rules:
      - Always include the always-on set (currently:
        integrations_check). The agent uses these to introspect its
        own service env BEFORE telling the user to set up vars.
      - Match keywords from the latest user message → enable groups.
      - Add any group whose tools were invoked in the last N turns
        (sticky: avoids yanking tools mid-multi-step flow).
      - If nothing matched (single-word "hello", math question, etc.),
        return ONLY the always-on tools — internal tools (calculator,
        retrieval, think) the agent already loaded handle these cases.
      - Cap the final list at _RELEVANCE_CAP. Trailing groups (in
        _TOOL_GROUP_TRIGGERS dict order) get dropped first.
    """
    by_group, name_to_group = _ensure_caches()

    enabled: set[str] = _detect_groups_from_text(query_text)
    enabled |= _detect_groups_from_history(messages, name_to_group)

    always_on = _get_always_on_tools()

    if not enabled:
        return always_on, ["always_on_only"] if always_on else []

    # Start with the always-on set (integrations_check, etc.) so the
    # agent can introspect its own env before claiming setup is needed.
    out: list[dict] = list(always_on)
    enabled_ordered: list[str] = ["always_on"] if always_on else []
    for gid in _TOOL_GROUP_TRIGGERS:
        if gid not in enabled:
            continue
        specs = by_group.get(gid, [])
        if not specs:
            continue
        if len(out) + len(specs) > _RELEVANCE_CAP:
            # Adding this group would breach the cap — partial-fill what
            # fits, then stop. Better than dropping the whole group.
            remaining = _RELEVANCE_CAP - len(out)
            if remaining <= 0:
                break
            out.extend(specs[:remaining])
            enabled_ordered.append(gid + "(partial)")
            break
        out.extend(specs)
        enabled_ordered.append(gid)

    return out, enabled_ordered


def _get_auto_inject_tools() -> list[dict]:
    """Return ALL integration tools as OpenAI function-calling specs (cached).

    Kept for backwards-compat with code paths that still want the full
    catalog (none in routes.py at present). Production traffic now goes
    through :func:`_select_relevant_tools` which prunes to the relevant
    subset per-request.
    """
    global _AUTO_INJECT_TOOLS_CACHE
    if _AUTO_INJECT_TOOLS_CACHE is not None:
        return _AUTO_INJECT_TOOLS_CACHE
    by_group, _ = _ensure_caches()
    out: list[dict] = []
    for specs in by_group.values():
        out.extend(specs)
    _AUTO_INJECT_TOOLS_CACHE = out
    return out


def _to_messages(chat_messages) -> list[Message]:
    """Convert Pydantic ChatMessage objects to core Message objects."""
    messages = []
    for m in chat_messages:
        role = Role(m.role) if m.role in {r.value for r in Role} else Role.USER
        messages.append(
            Message(
                role=role,
                content=m.content or "",
                name=m.name,
                tool_call_id=m.tool_call_id,
            )
        )
    return messages


@router.post("/v1/chat/completions")
async def chat_completions(request_body: ChatCompletionRequest, request: Request):
    """Handle chat completion requests (streaming and non-streaming)."""
    engine = request.app.state.engine
    agent = getattr(request.app.state, "agent", None)
    model = request_body.model

    # Inject memory context into messages before dispatching
    config = getattr(request.app.state, "config", None)
    memory_backend = getattr(request.app.state, "memory_backend", None)
    if (
        config is not None
        and memory_backend is not None
        and config.agent.context_from_memory
        and request_body.messages
    ):
        try:
            from openjarvis.tools.storage.context import ContextConfig, inject_context

            # Extract query from the last user message
            query_text = ""
            for m in reversed(request_body.messages):
                if m.role == "user" and m.content:
                    query_text = m.content
                    break

            if query_text:
                messages = _to_messages(request_body.messages)
                ctx_cfg = ContextConfig(
                    top_k=config.memory.context_top_k,
                    min_score=config.memory.context_min_score,
                    max_context_tokens=config.memory.context_max_tokens,
                )
                enriched = inject_context(
                    query_text,
                    messages,
                    memory_backend,
                    config=ctx_cfg,
                )
                # Rebuild request messages from enriched Message objects
                if len(enriched) > len(messages):
                    from openjarvis.server.models import ChatMessage

                    new_msgs = []
                    for msg in enriched:
                        new_msgs.append(
                            ChatMessage(
                                role=msg.role.value,
                                content=msg.content,
                                name=msg.name,
                                tool_call_id=getattr(msg, "tool_call_id", None),
                            )
                        )
                    request_body.messages = new_msgs
        except Exception:
            logging.getLogger("openjarvis.server").debug(
                "Memory context injection failed",
                exc_info=True,
            )

    # Round 3.3 — Skill-aware planner short-circuit.  Before any LLM call,
    # check if a registered TOML skill matches the user's latest message
    # with high confidence.  If so, execute the skill chain directly and
    # return the result as a normal chat completion.  Hermes structurally
    # can't do this — its planner always invokes the LLM.
    if os.getenv("OPENJARVIS_SKILL_PLANNER_ENABLED", "false").lower() in ("1", "true", "yes", "on"):
        try:
            from openjarvis.agents.skill_planner import maybe_handle as _skill_maybe_handle
            latest_user = ""
            for m in reversed(request_body.messages):
                if m.role == "user" and m.content:
                    latest_user = m.content
                    break
            skill_answer = _skill_maybe_handle(latest_user) if latest_user else None
            if skill_answer:
                _fire_post_turn_hooks_safe(
                    request=request,
                    latest_user_text=latest_user,
                    assistant_text=skill_answer,
                    complexity_info=None,
                )
                return ChatCompletionResponse(
                    model=request_body.model,
                    choices=[Choice(
                        message=ChoiceMessage(role="assistant", content=skill_answer),
                        finish_reason="stop",
                    )],
                    usage=UsageInfo(prompt_tokens=0, completion_tokens=0, total_tokens=0),
                    complexity=None,
                )
        except Exception as _sp_exc:
            logging.getLogger("openjarvis.server").debug(
                "skill_planner short-circuit skipped: %s", _sp_exc,
            )

    # Round 5.1 — Predictive answer cache lookup.  After skill_planner has
    # had its first shot (skills execute fresh logic), check the cache.
    # The cache only stores reflections w/ conf>=0.85 && success=True, so a
    # hit is a previously-validated answer to a near-identical query.
    if os.getenv("OPENJARVIS_ANSWER_CACHE_LOOKUP_ENABLED", "false").lower() in ("1", "true", "yes", "on"):
        try:
            from openjarvis.learning import answer_cache as _ac
            from openjarvis.learning.domain_classifier import infer_domain as _infer_domain
            latest_user_cache = ""
            for m in reversed(request_body.messages):
                if m.role == "user" and m.content:
                    latest_user_cache = m.content
                    break
            if latest_user_cache:
                cache_domain = _infer_domain(latest_user_cache)
                hit = _ac.lookup(latest_user_cache, cache_domain)
                if hit and hit.get("answer"):
                    logging.getLogger("openjarvis.server").info(
                        "openjarvis.answer_cache.hit_serve domain=%s hits=%d conf=%.2f",
                        cache_domain, hit.get("hits", 0), hit.get("confidence", 0.0),
                    )
                    return ChatCompletionResponse(
                        model=request_body.model,
                        choices=[Choice(
                            message=ChoiceMessage(role="assistant", content=hit["answer"]),
                            finish_reason="stop",
                        )],
                        usage=UsageInfo(prompt_tokens=0, completion_tokens=0, total_tokens=0),
                        complexity=None,
                    )
        except Exception as _ac_exc:
            logging.getLogger("openjarvis.server").debug(
                "answer_cache lookup skipped: %s", _ac_exc,
            )

    # Auto-inject integration tools when the frontend didn't send any,
    # filtered by relevance to the user's latest message.
    #
    # The chat UI omits `tools` from its request body. Without this hook,
    # every "send Pedro an email" / "what's on my calendar" / "list my
    # workflows" prompt would land on a tool-blind cascade and the model
    # would hallucinate prose about the API instead of calling registered
    # tools. The OLD version of this block injected ALL ~100 integration
    # tools on every request (~40 K input tokens of function specs per
    # turn). The relevance filter cuts that to a handful of relevant
    # tools per turn — typical reduction is ~85-90 % input tokens, while
    # multi-step interactions still work via the recent-tool stickiness
    # window in :func:`_select_relevant_tools`.
    if not request_body.tools:
        try:
            # Latest user message drives the keyword match.
            latest_user = ""
            for m in reversed(request_body.messages):
                if m.role == "user" and m.content:
                    latest_user = m.content
                    break
            relevant_tools, enabled_groups = _select_relevant_tools(
                latest_user, request_body.messages,
            )
            if relevant_tools:
                request_body.tools = relevant_tools
                logging.getLogger("openjarvis.server").info(
                    "auto-injected %d tools (groups=%s) into request",
                    len(relevant_tools), enabled_groups,
                )
        except Exception:
            logging.getLogger("openjarvis.server").debug(
                "auto-inject of integration tools failed",
                exc_info=True,
            )

    # Run complexity analysis on the last user message
    complexity_info = None
    query_text_for_complexity = ""
    for m in reversed(request_body.messages):
        if m.role == "user" and m.content:
            query_text_for_complexity = m.content
            break
    if query_text_for_complexity:
        try:
            from openjarvis.learning.routing.complexity import (
                adjust_tokens_for_model,
                score_complexity,
            )

            cr = score_complexity(query_text_for_complexity)
            suggested = adjust_tokens_for_model(
                cr.suggested_max_tokens,
                model,
            )
            complexity_info = ComplexityInfo(
                score=cr.score,
                tier=cr.tier,
                suggested_max_tokens=suggested,
            )
            # Bump max_tokens when complexity suggests more than what
            # the client requested — never reduce below the request value.
            if suggested > request_body.max_tokens:
                request_body.max_tokens = suggested
        except Exception:
            logging.getLogger("openjarvis.server").debug(
                "Complexity analysis failed",
                exc_info=True,
            )

    # Round 5.7 — Hybrid 4-tier router. When enabled, computes a tier from
    # complexity x vault-confidence and may force orchestrator-consensus
    # mode for this request (via thread-local env override). Decision is
    # non-binding: Tier 2/3 still flow through the orchestrator gate below;
    # Tier 0/1 fall through to single-engine path.
    _hybrid_tier = None
    try:
        from openjarvis.learning.routing import hybrid as _hybrid
        if complexity_info is not None:
            _hybrid_tier = _hybrid.decide(query_text_for_complexity, complexity_info.score)
    except Exception:
        _hybrid_tier = None

    # Round 1.2 (architectural fix) — Parallel orchestrator gate.  Original
    # gate `if not req.tools` was dead-code because tools are auto-injected
    # on every request.  Correct gate: route trivial/simple complexity-tier
    # turns through the orchestrator ensemble (parallel ping all providers,
    # pick best by mode=fastest|consensus).  These tiers don't need tools
    # anyway, and Hermes structurally cannot do parallel ensemble.
    # Round 5.7: also activate for hybrid tiers 2 + 3 (medium/high complexity)
    _orch_gate_open = (
        not request_body.stream
        and complexity_info is not None
        and os.getenv("OPENJARVIS_ORCHESTRATOR_ENABLED", "false").lower() in ("1", "true", "yes", "on")
        and (
            complexity_info.tier in ("trivial", "simple")
            or (_hybrid_tier is not None and _hybrid_tier.tier in (
                _hybrid.TIER_ORCH_FASTEST, _hybrid.TIER_ORCH_CONSENSUS
            ))
        )
    )
    if _orch_gate_open:
        try:
            from openjarvis.orchestrator.router import (
                run_all as _orch_run_all,
                pick_best as _orch_pick_best,
            )
            _orch_msgs = [
                {"role": m.role.value if hasattr(m.role, "value") else m.role,
                 "content": m.content or ""}
                for m in request_body.messages
            ]
            responses = await _orch_run_all(_orch_msgs)
            if responses:
                # Round 5.2 — pass inferred domain so pick_best can use
                # model_preference as a tiebreak in fastest mode.
                try:
                    from openjarvis.learning.domain_classifier import infer_domain as _id
                    _orch_domain = _id(query_text_for_complexity,
                                       getattr(complexity_info, "signals", None) if complexity_info else None)
                except Exception:
                    _orch_domain = "general"
                # Hybrid tier picks consensus mode explicitly (no env mutation)
                _orch_mode = ""
                try:
                    if _hybrid_tier and _hybrid_tier.tier == _hybrid.TIER_ORCH_CONSENSUS:
                        _orch_mode = "consensus"
                except Exception:
                    pass
                best = _orch_pick_best(responses, mode=_orch_mode, domain=_orch_domain)
                _text = best.get("text", "")
                if _text and "Error" not in _text:
                    logging.getLogger("openjarvis.server").info(
                        "openjarvis.orchestrator.dispatch winner=%s providers=%d tier=%s",
                        best.get("model", "?"), len(responses), complexity_info.tier,
                    )
                    latest_user_text = ""
                    for m in reversed(request_body.messages):
                        if m.role == "user" and m.content:
                            latest_user_text = m.content
                            break
                    _fire_post_turn_hooks_safe(
                        request=request,
                        latest_user_text=latest_user_text,
                        assistant_text=_text,
                        complexity_info=complexity_info,
                    )
                    return ChatCompletionResponse(
                        model=request_body.model,
                        choices=[Choice(
                            message=ChoiceMessage(role="assistant", content=_text),
                            finish_reason="stop",
                        )],
                        usage=UsageInfo(prompt_tokens=0, completion_tokens=0, total_tokens=0),
                        complexity=complexity_info,
                    )
        except Exception as _orch_exc:
            logging.getLogger("openjarvis.server").debug(
                "orchestrator dispatch-level skipped: %s", _orch_exc,
            )

    if request_body.stream:
        bus = getattr(request.app.state, "bus", None)
        # X-OpenJarvis-Direct: the caller (e.g. the LiveKit voice worker)
        # wants the FAST path — skip the agent orchestrator entirely and
        # stream the engine token-by-token. The agent bridge runs
        # agent.run() to completion before emitting anything, which adds
        # seconds of latency; voice turns can't afford that.
        openjarvis_direct = (
            request.headers.get("x-openjarvis-direct", "").strip().lower()
            in ("1", "true", "yes")
        )
        # Use the agent stream bridge only when tools are present (the
        # bridge runs agent.run() synchronously and word-splits the result,
        # so it can't stream tokens in real-time).  For plain chat, stream
        # directly from the engine for true token-by-token output.
        if (
            agent is not None
            and bus is not None
            and request_body.tools
            and not openjarvis_direct
        ):
            # Plain OpenAI clients (e.g. the LiveKit voice worker) send
            # X-OpenJarvis-Stream: openai and need a strict OpenAI SSE
            # stream — no custom `event:` UI messages, which crash the
            # OpenAI client parser with "choices is None".
            openai_strict = (
                request.headers.get("x-openjarvis-stream", "").strip().lower()
                == "openai"
            )
            return await _handle_agent_stream(
                agent, bus, model, request_body,
                memory_backend=memory_backend,
                openai_strict=openai_strict,
            )
        return await _handle_stream(
            engine, model, request_body, complexity_info,
            memory_backend=memory_backend,
        )

    # Non-streaming: use agent if available, otherwise direct engine call
    if agent is not None:
        return _handle_agent(agent, model, request_body, complexity_info)

    bus = getattr(request.app.state, "bus", None)
    return _handle_direct(
        engine,
        model,
        request_body,
        bus=bus,
        complexity_info=complexity_info,
    )


def _record_cost(result: Any, *, model: str, latency_ms: int, role: str = "main") -> None:
    """Round 5.4 — record main-path engine call into cost_telemetry."""
    try:
        from openjarvis.learning import cost_telemetry as _ct
        usage = (result or {}).get("usage") if isinstance(result, dict) else {}
        if not isinstance(usage, dict):
            usage = {}
        tokens_in = int(usage.get("prompt_tokens", 0) or 0)
        tokens_out = int(usage.get("completion_tokens", 0) or 0)
        # Char-estimate fallback when provider doesn't return usage
        if tokens_in == 0 and isinstance(result, dict):
            content = result.get("content") or ""
            tokens_out = tokens_out or max(1, len(content) // 4)
        provider = model.split("/")[0] if "/" in model else (model or "unknown")
        success = bool((result or {}).get("content") if isinstance(result, dict) else False)
        _ct.record(
            provider=provider, model=model,
            tokens_in=tokens_in, tokens_out=tokens_out,
            latency_ms=int(latency_ms), success=success, role=role,
        )
    except Exception:
        pass


def _handle_direct(
    engine,
    model: str,
    req: ChatCompletionRequest,
    bus=None,
    complexity_info=None,
) -> ChatCompletionResponse:
    """Direct engine call without agent."""
    import time as _time
    messages = _to_messages(req.messages)
    kwargs: dict[str, Any] = {}
    if req.tools:
        kwargs["tools"] = req.tools

    # Round 5 BONUS-B — temperature auto-tune. Only override when the
    # caller's temperature looks like a default (not explicitly tuned per-call)
    # and the tuner has been recording. Hermes can't do this — it has one
    # global temperature.
    try:
        from openjarvis.learning import temperature_tuner as _tt
        from openjarvis.learning.domain_classifier import infer_domain as _id_dom
        if _tt._enabled() and (req.temperature is None or abs((req.temperature or 0.0) - 0.7) < 0.01):
            _latest_user = ""
            for _m in reversed(req.messages):
                if _m.role == "user" and _m.content:
                    _latest_user = _m.content
                    break
            _dom = _id_dom(_latest_user)
            _rec_temp = _tt.recommended(_dom)
            if _rec_temp is not None:
                req.temperature = _rec_temp
    except Exception:
        pass
    if bus:
        from openjarvis.telemetry.wrapper import instrumented_generate

        _t0 = _time.time()
        result = instrumented_generate(
            engine,
            messages,
            model=model,
            bus=bus,
            temperature=req.temperature,
            max_tokens=req.max_tokens,
            **kwargs,
        )
        _record_cost(result, model=model,
                     latency_ms=int((_time.time() - _t0) * 1000),
                     role="main_instrumented")
    else:
        # Round 5 BONUS-A — Speculative race REMOVED from _handle_direct.
        # Original implementation called asyncio.run() / run_until_complete()
        # from inside this sync function (which FastAPI dispatches via its
        # threadpool). Each request leaked an event loop + aiohttp session;
        # a burst of ~20 sequential calls wedged the service.
        #
        # The speculative module itself stays in the codebase as a primitive —
        # to re-enable, wire it from a genuinely async FastAPI handler (not
        # from _handle_direct's sync threadpool context). The orchestrator
        # gate at chat_completions() already provides parallel-LLM benefit
        # via an async path.
        result = None

        # Round 1.2 — Parallel orchestrator: when env-enabled, ping all
        # configured LLM providers concurrently and pick the best/consensus
        # response via orchestrator.pick_best.  Falls back to engine.generate
        # if orchestrator fails or returns nothing usable.
        if result is None and (
            os.getenv("OPENJARVIS_ORCHESTRATOR_ENABLED", "false").lower() in ("1", "true", "yes", "on")
            and not req.tools  # skip orchestrator when caller passes tools (orchestrator doesn't proxy tool calls)
        ):
            try:
                import asyncio as _asyncio
                from openjarvis.orchestrator.router import run_all as _orch_run_all
                from openjarvis.orchestrator.router import pick_best as _orch_pick_best

                _msgs_for_orch = [
                    {"role": m.role.value if hasattr(m.role, "value") else m.role,
                     "content": m.content or ""}
                    for m in messages
                ]
                try:
                    _loop = _asyncio.get_event_loop()
                    if _loop.is_running():
                        # Already inside an async context (FastAPI handler dispatched
                        # this sync function via a threadpool).  Use a new loop.
                        raise RuntimeError("nested-loop")
                    responses = _loop.run_until_complete(_orch_run_all(_msgs_for_orch))
                except RuntimeError:
                    responses = _asyncio.run(_orch_run_all(_msgs_for_orch))

                if responses:
                    best = _orch_pick_best(responses)  # honors OPENJARVIS_ORCHESTRATOR_MODE
                    _text = best.get("text", "")
                    if _text and "Error" not in _text:
                        result = {"content": _text, "usage": {}, "tool_calls": None,
                                  "finish_reason": "stop"}
                        logging.getLogger("openjarvis.server").info(
                            "openjarvis.orchestrator.dispatch winner=%s providers=%d",
                            best.get("model", "?"), len(responses),
                        )
                    else:
                        result = None
                else:
                    result = None
            except Exception as _orch_exc:
                logging.getLogger("openjarvis.server").debug(
                    "orchestrator path skipped: %s", _orch_exc,
                )
                result = None

            if result is None:
                _t0 = _time.time()
                result = engine.generate(
                    messages, model=model,
                    temperature=req.temperature, max_tokens=req.max_tokens,
                    **kwargs,
                )
                _record_cost(result, model=model,
                             latency_ms=int((_time.time() - _t0) * 1000),
                             role="main_fallback")
        elif result is None:
            # Neither speculative nor orchestrator filled result — raw single-engine path
            _t0 = _time.time()
            result = engine.generate(
                messages,
                model=model,
                temperature=req.temperature,
                max_tokens=req.max_tokens,
                **kwargs,
            )
            _record_cost(result, model=model,
                         latency_ms=int((_time.time() - _t0) * 1000),
                         role="main_raw")
    content = result.get("content", "")
    usage = result.get("usage", {})

    choice_msg = ChoiceMessage(role="assistant", content=content)
    # Include tool calls if present
    tool_calls = result.get("tool_calls")
    if tool_calls:
        choice_msg.tool_calls = [
            {
                "id": tc.get("id", ""),
                "type": "function",
                "function": {
                    "name": tc.get("name", ""),
                    "arguments": tc.get("arguments", "{}"),
                },
            }
            for tc in tool_calls
        ]

    # Rounds 2.1 + 3.5 — fire reflector + distillation feedback callbacks.
    # Off-path (background); never blocks the user response.
    _fire_post_turn_hooks_safe(
        request=None,
        latest_user_text=_extract_latest_user_text_from_messages(messages),
        assistant_text=content,
        complexity_info=complexity_info,
    )

    return ChatCompletionResponse(
        model=model,
        choices=[
            Choice(
                message=choice_msg,
                finish_reason=result.get("finish_reason", "stop"),
            )
        ],
        usage=UsageInfo(
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
        ),
        complexity=complexity_info,
    )


def _handle_agent(
    agent,
    model: str,
    req: ChatCompletionRequest,
    complexity_info=None,
) -> ChatCompletionResponse:
    """Run through agent."""
    from openjarvis.agents._stubs import AgentContext

    # Build context from prior messages
    ctx = AgentContext()
    if len(req.messages) > 1:
        prior = _to_messages(req.messages[:-1])
        for m in prior:
            ctx.conversation.add(m)

    # Last message is the input
    input_text = req.messages[-1].content if req.messages else ""

    # Override agent model for this request if the caller specified one
    original_model = agent._model
    if model:
        agent._model = model
    try:
        result = agent.run(input_text, context=ctx)
    finally:
        agent._model = original_model

    usage = UsageInfo(
        prompt_tokens=result.metadata.get("prompt_tokens", 0),
        completion_tokens=result.metadata.get("completion_tokens", 0),
        total_tokens=result.metadata.get("total_tokens", 0),
    )

    # Include audio metadata if the agent produced audio (e.g. morning digest)
    audio_meta = None
    audio_path = result.metadata.get("audio_path", "")
    if audio_path:
        from pathlib import Path

        from openjarvis.server.models import AudioMeta

        if Path(audio_path).exists():
            audio_meta = AudioMeta(url="/api/digest/audio")

    # Rounds 2.1 + 3.5 — fire reflector + distillation feedback callbacks.
    _fire_post_turn_hooks_safe(
        request=None,
        latest_user_text=input_text,
        assistant_text=result.content,
        complexity_info=complexity_info,
    )

    return ChatCompletionResponse(
        model=model,
        choices=[
            Choice(
                message=ChoiceMessage(
                    role="assistant",
                    content=result.content,
                    audio=audio_meta,
                ),
                finish_reason="stop",
            )
        ],
        usage=usage,
        complexity=complexity_info,
    )


async def _handle_agent_stream(
    agent, bus, model, req, *, memory_backend=None, openai_strict=False
):
    """Stream agent response with EventBus events via SSE."""
    from openjarvis.server.stream_bridge import create_agent_stream

    return await create_agent_stream(
        agent, bus, model, req,
        memory_backend=memory_backend,
        openai_strict=openai_strict,
    )


async def _handle_stream(
    engine,
    model: str,
    req: ChatCompletionRequest,
    complexity_info=None,
    *,
    memory_backend=None,
):
    """Stream response using SSE format."""
    from openjarvis.server.cloud_router import (
        is_cloud_model,
        stream_cloud,
        stream_local,
    )
    from openjarvis.server import elaboration_worker, tier_cascade
    from openjarvis.server.elaboration_store import get_store as _elab_store

    messages = _to_messages(req.messages)
    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"

    # Route directly to the right backend — bypasses engine routing entirely
    # so broken MultiEngine state can never misdirect requests.
    use_cloud = is_cloud_model(model)
    use_cascade = tier_cascade.is_auto_model(model)

    # If the cascade is active, kick off the slow Claude-CLI elaboration
    # track in parallel. The chat handler keeps a reference to the record so
    # the spoken_answer can be written back when this stream finishes.
    elaboration = None
    if use_cascade:
        # Original question is the most recent user message
        original_question = ""
        for m in reversed(req.messages):
            if m.role == "user" and m.content:
                original_question = m.content
                break
        try:
            messages_json = [
                {"role": m.role, "content": m.content or ""}
                for m in req.messages
            ]
            elaboration = await elaboration_worker.spawn_elaboration(
                messages=messages_json,
                original_question=original_question,
                conversation_id=None,
                memory_backend=memory_backend,
            )
        except Exception as exc:
            import logging as _logging
            _logging.getLogger("openjarvis.server").debug(
                "Elaboration spawn failed (non-fatal): %s", exc,
            )

    async def generate():
        # Send role chunk first
        first_chunk = ChatCompletionChunk(
            id=chunk_id,
            model=model,
            choices=[
                StreamChoice(
                    delta=DeltaMessage(role="assistant"),
                )
            ],
        )
        yield f"data: {first_chunk.model_dump_json()}\n\n"

        # Accumulate the streamed text so we can write it back into the
        # elaboration record when the stream finishes.
        spoken_buffer: list[str] = []

        try:
            if req.tools:
                # Tool-using path: must stream StreamChunks (content +
                # tool_calls + finish_reason) so function-calling deltas
                # reach the client. The plain-text cascade and
                # cloud_router.stream_cloud strip tools entirely, so we
                # route through engine.stream_full() (single model) or
                # tool_cascade.cascade_tools() (auto, races tool-capable
                # cloud models with key-availability filtering).
                from openjarvis.server import tool_cascade

                if use_cascade:
                    chunk_iter = tool_cascade.cascade_tools(
                        engine,
                        messages,
                        req.tools,
                        temperature=req.temperature,
                        max_tokens=req.max_tokens,
                    )
                else:
                    chunk_iter = engine.stream_full(
                        messages,
                        model=model,
                        tools=req.tools,
                        temperature=req.temperature,
                        max_tokens=req.max_tokens,
                    )
                async for sc in chunk_iter:
                    if sc.content:
                        spoken_buffer.append(sc.content)
                        content_chunk = ChatCompletionChunk(
                            id=chunk_id,
                            model=model,
                            choices=[
                                StreamChoice(
                                    delta=DeltaMessage(content=sc.content),
                                )
                            ],
                        )
                        yield f"data: {content_chunk.model_dump_json()}\n\n"
                    if sc.tool_calls:
                        tc_chunk = ChatCompletionChunk(
                            id=chunk_id,
                            model=model,
                            choices=[
                                StreamChoice(
                                    delta=DeltaMessage(tool_calls=sc.tool_calls),
                                )
                            ],
                        )
                        yield f"data: {tc_chunk.model_dump_json()}\n\n"
                    if sc.finish_reason:
                        # The model has signalled end-of-turn (either
                        # "stop" or "tool_calls"); fall through to the
                        # outer finish-chunk emission.
                        break
            elif use_cascade:
                # Plain chat on auto → 3-tier text race (existing path).
                token_iter = tier_cascade.cascade(
                    messages,
                    temperature=req.temperature,
                    max_tokens=req.max_tokens,
                )
                async for token in token_iter:
                    spoken_buffer.append(token)
                    chunk = ChatCompletionChunk(
                        id=chunk_id,
                        model=model,
                        choices=[
                            StreamChoice(
                                delta=DeltaMessage(content=token),
                            )
                        ],
                    )
                    yield f"data: {chunk.model_dump_json()}\n\n"
            elif use_cloud:
                token_iter = stream_cloud(
                    model, messages, req.temperature, req.max_tokens
                )
                async for token in token_iter:
                    spoken_buffer.append(token)
                    chunk = ChatCompletionChunk(
                        id=chunk_id,
                        model=model,
                        choices=[
                            StreamChoice(
                                delta=DeltaMessage(content=token),
                            )
                        ],
                    )
                    yield f"data: {chunk.model_dump_json()}\n\n"
            else:
                # Use engine.stream() by default (preserves mock-engine
                # compatibility in tests).  Only fall back to stream_local()
                # when a real MultiEngine would mis-route the local model to a
                # cloud backend — detected via isinstance so mocks are not
                # accidentally matched.
                _use_local_fallback = False
                try:
                    from openjarvis.engine.multi import MultiEngine

                    _inner = getattr(engine, "_inner", engine)
                    if isinstance(_inner, MultiEngine):
                        _routed = _inner._engine_for(model)
                        if _routed is not None and getattr(_routed, "is_cloud", False):
                            _use_local_fallback = True
                except Exception:
                    pass
                if _use_local_fallback:
                    token_iter = stream_local(
                        model, messages, req.temperature, req.max_tokens
                    )
                else:
                    token_iter = engine.stream(
                        messages,
                        model=model,
                        temperature=req.temperature,
                        max_tokens=req.max_tokens,
                    )
                async for token in token_iter:
                    spoken_buffer.append(token)
                    chunk = ChatCompletionChunk(
                        id=chunk_id,
                        model=model,
                        choices=[
                            StreamChoice(
                                delta=DeltaMessage(content=token),
                            )
                        ],
                    )
                    yield f"data: {chunk.model_dump_json()}\n\n"
        except Exception as exc:
            # Surface errors as a content chunk so the frontend can
            # display them instead of silently failing.
            import logging

            logging.getLogger("openjarvis.server").error(
                "Stream error: %s",
                exc,
                exc_info=True,
            )
            error_chunk = ChatCompletionChunk(
                id=chunk_id,
                model=model,
                choices=[
                    StreamChoice(
                        delta=DeltaMessage(
                            content=f"\n\nError during generation: {exc}",
                        ),
                        finish_reason="stop",
                    )
                ],
            )
            yield f"data: {error_chunk.model_dump_json()}\n\n"
            yield "data: [DONE]\n\n"
            return

        # Send finish chunk with usage data if available
        import json as _json

        finish_data = ChatCompletionChunk(
            id=chunk_id,
            model=model,
            choices=[
                StreamChoice(
                    delta=DeltaMessage(),
                    finish_reason="stop",
                )
            ],
        )
        finish_dict = _json.loads(finish_data.model_dump_json())

        # Tag the finish chunk with the correct engine label.
        # We use the routing decision (use_cloud) directly rather than
        # unwrapping the engine chain, which can be in a broken state.
        finish_dict.setdefault("telemetry", {})
        if use_cascade:
            finish_dict["telemetry"]["engine"] = "cascade"
        else:
            finish_dict["telemetry"]["engine"] = "cloud" if use_cloud else "ollama"

        # Write the spoken text into the elaboration record so the worker
        # can compare it against Claude-CLI's eventual answer.
        full_spoken = "".join(spoken_buffer)
        if elaboration is not None:
            try:
                await _elab_store().set_spoken_answer(
                    elaboration.id, full_spoken,
                )
            except Exception:
                pass

        # Auto-store the Q&A pair into long-term memory so future
        # conversations can retrieve it via inject_context. Skips
        # trivial / error-shaped responses; non-fatal on failure.
        if memory_backend is not None:
            try:
                from openjarvis.server.memory_writeback import store_qa

                user_question = ""
                for m in reversed(req.messages):
                    if m.role == "user" and m.content:
                        user_question = m.content
                        break
                store_qa(
                    backend=memory_backend,
                    question=user_question,
                    answer=full_spoken,
                    source="fast_path",
                    model=model,
                )
            except Exception:
                pass

        if complexity_info is not None:
            finish_dict["complexity"] = complexity_info.model_dump()

        yield f"data: {_json.dumps(finish_dict)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@router.get("/v1/models")
async def list_models(request: Request) -> ModelListResponse:
    """List locally installed models (Ollama).

    Cloud models are not included here — they live in the Cloud Models tab
    of the UI and are selected there, not from this endpoint.
    """
    from openjarvis.server.cloud_router import is_cloud_model, list_local_models

    # Prefer engine.list_models() so mock engines work in tests.
    # Filter out any cloud model IDs that may appear via MultiEngine.
    # Fall back to direct Ollama query only when the engine returns nothing.
    engine = request.app.state.engine
    all_ids = engine.list_models()
    model_ids = [m for m in all_ids if not is_cloud_model(m)]
    if not model_ids:
        model_ids = await list_local_models()

    return ModelListResponse(
        data=[ModelObject(id=mid) for mid in model_ids],
    )


@router.post("/v1/models/pull")
async def pull_model(request: Request):
    """Pull / download a model from the Ollama registry."""
    body = await request.json()
    model_name = body.get("model", "").strip()
    if not model_name:
        raise HTTPException(status_code=400, detail="'model' field is required")

    engine = request.app.state.engine
    engine_name = getattr(request.app.state, "engine_name", "")
    # Only Ollama supports pulling
    if engine_name != "ollama" and getattr(engine, "engine_id", "") != "ollama":
        raise HTTPException(
            status_code=501,
            detail="Model pulling is only supported with the Ollama engine",
        )

    import httpx as _httpx

    host = getattr(engine, "_host", "http://localhost:11434")
    client = _httpx.Client(base_url=host, timeout=600.0)
    try:
        resp = client.post(
            "/api/pull",
            json={"name": model_name, "stream": False},
        )
        resp.raise_for_status()
    except (_httpx.ConnectError, _httpx.TimeoutException) as exc:
        raise HTTPException(status_code=502, detail=f"Ollama unreachable: {exc}")
    except _httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=exc.response.status_code,
            detail=f"Ollama error: {exc.response.text[:300]}",
        )
    finally:
        client.close()

    return {"status": "ok", "model": model_name}


@router.delete("/v1/models/{model_name:path}")
async def delete_model(model_name: str, request: Request):
    """Delete a model from Ollama."""
    engine = request.app.state.engine
    engine_name = getattr(request.app.state, "engine_name", "")
    if engine_name != "ollama" and getattr(engine, "engine_id", "") != "ollama":
        raise HTTPException(status_code=501, detail="Only supported with Ollama engine")

    import httpx as _httpx

    host = getattr(engine, "_host", "http://localhost:11434")
    client = _httpx.Client(base_url=host, timeout=30.0)
    try:
        resp = client.request(
            "DELETE",
            "/api/delete",
            json={"name": model_name},
        )
        resp.raise_for_status()
    except (_httpx.ConnectError, _httpx.TimeoutException) as exc:
        raise HTTPException(status_code=502, detail=f"Ollama unreachable: {exc}")
    except _httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=exc.response.status_code,
            detail=f"Ollama error: {exc.response.text[:300]}",
        )
    finally:
        client.close()

    return {"status": "deleted", "model": model_name}


@router.post("/v1/cloud/reload")
async def reload_cloud_engine(request: Request):
    """Hot-reload cloud API keys and (re-)initialize the cloud engine.

    Called by the desktop app immediately after the user saves a cloud API
    key so that cloud models become available without a full app restart.
    """
    import os
    from pathlib import Path

    # Re-read ~/.openjarvis/cloud-keys.env and update the running process env.
    keys_path = Path.home() / ".openjarvis" / "cloud-keys.env"
    if keys_path.exists():
        for raw_line in keys_path.read_text().splitlines():
            line = raw_line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip()

    # Try to build a fresh CloudEngine.
    try:
        from openjarvis.engine.cloud import CloudEngine
        from openjarvis.engine.multi import MultiEngine

        cloud = CloudEngine()
        if not cloud.health():
            return {
                "status": "no_cloud",
                "message": "No cloud models available (check API keys)",
            }
    except Exception as exc:
        return {"status": "error", "message": str(exc)}

    # Locate the innermost engine, working through InstrumentedEngine layers.
    outer = request.app.state.engine
    inner = getattr(outer, "_inner", outer)

    if isinstance(inner, MultiEngine):
        # Replace or insert the cloud entry in the existing MultiEngine.
        new_engines = [(k, e) for k, e in inner._engines if k != "cloud"]
        new_engines.append(("cloud", cloud))
        inner._engines = new_engines
        inner._refresh_map()
    else:
        # Wrap the existing engine (which may be security-wrapped) with a new
        # MultiEngine that includes the cloud engine.
        engine_name = getattr(request.app.state, "engine_name", "local")
        new_multi = MultiEngine([(engine_name, inner), ("cloud", cloud)])
        if hasattr(outer, "_inner"):
            outer._inner = new_multi
        else:
            request.app.state.engine = new_multi
        request.app.state.engine_name = "multi"

    return {"status": "ok", "message": "Cloud engine reloaded"}


@router.get("/v1/savings")
async def savings(request: Request):
    """Return savings summary compared to cloud providers.

    Only includes telemetry from the current server session so that
    counters start at zero each time a new model + agent is launched.
    """
    from openjarvis.core.config import DEFAULT_CONFIG_DIR
    from openjarvis.server.savings import compute_savings, savings_to_dict
    from openjarvis.telemetry.aggregator import TelemetryAggregator

    db_path = DEFAULT_CONFIG_DIR / "telemetry.db"
    if not db_path.exists():
        empty = compute_savings(0, 0, 0)
        return savings_to_dict(empty)

    session_start = getattr(request.app.state, "session_start", None)

    agg = TelemetryAggregator(db_path)
    try:
        summary = agg.summary(since=session_start)
        # Exclude cloud model tokens from savings — only local
        # inference counts toward cost savings.
        _cloud_prefixes = (
            "gpt-",
            "o1-",
            "o3-",
            "o4-",
            "claude-",
            "gemini-",
            "openrouter/",
        )
        local_models = [
            m
            for m in summary.per_model
            if not any(m.model_id.startswith(p) for p in _cloud_prefixes)
        ]
        result = compute_savings(
            prompt_tokens=sum(m.prompt_tokens for m in local_models),
            completion_tokens=sum(m.completion_tokens for m in local_models),
            total_calls=sum(m.call_count for m in local_models),
            session_start=session_start if session_start else 0.0,
            prompt_tokens_evaluated=sum(
                m.prompt_tokens_evaluated for m in local_models
            ),
        )
        return savings_to_dict(result)
    finally:
        agg.close()


@router.post("/v1/telemetry/reset")
async def reset_telemetry():
    """Clear all stored telemetry records.

    Useful after updating token-counting methodology — clears
    historical records that were computed under the old rules so
    that the savings dashboard and leaderboard submissions start
    fresh with corrected values.
    """
    from openjarvis.core.config import DEFAULT_CONFIG_DIR
    from openjarvis.telemetry.aggregator import TelemetryAggregator

    db_path = DEFAULT_CONFIG_DIR / "telemetry.db"
    if not db_path.exists():
        return {"status": "ok", "records_cleared": 0}

    agg = TelemetryAggregator(db_path)
    try:
        count = agg.clear()
    finally:
        agg.close()
    return {"status": "ok", "records_cleared": count}


@router.get("/v1/info")
async def server_info(request: Request):
    """Return server configuration: model, agent, engine."""
    agent = getattr(request.app.state, "agent", None)
    agent_id = getattr(agent, "agent_id", None) if agent else None
    # Fall back to configured agent name if agent didn't instantiate
    if agent_id is None:
        agent_id = getattr(request.app.state, "agent_name", None)
    return {
        "model": getattr(request.app.state, "model", ""),
        "agent": agent_id,
        "engine": getattr(request.app.state, "engine_name", ""),
    }


@router.get("/v1/version")
async def server_version() -> dict:
    """Return the deployed git commit + build time.

    Lets you confirm Railway is running the code you pushed without
    digging through dashboards. The git_commit value is read from
    OPENJARVIS_GIT_COMMIT (set by the build pipeline) or "unknown" if
    running from a checkout where the commit isn't recorded.
    """
    import os as _os
    return {
        "git_commit": _os.environ.get("OPENJARVIS_GIT_COMMIT", "unknown"),
        "build_time": _os.environ.get("OPENJARVIS_BUILD_TIME", "unknown"),
        "deployment": _os.environ.get("RAILWAY_DEPLOYMENT_ID", "local"),
    }


@router.get("/health")
async def health(request: Request):
    """Health check endpoint."""
    engine = request.app.state.engine
    healthy = engine.health()
    if not healthy:
        raise HTTPException(status_code=503, detail="Engine unhealthy")
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Channel endpoints
# ---------------------------------------------------------------------------


@router.get("/v1/channels")
async def list_channels(request: Request):
    """List available messaging channels."""
    bridge = getattr(request.app.state, "channel_bridge", None)
    if bridge is None:
        return {"channels": [], "message": "Channel bridge not configured"}
    channels = bridge.list_channels()
    return {"channels": channels, "status": bridge.status().value}


@router.post("/v1/channels/send")
async def channel_send(request: Request):
    """Send a message to a channel."""
    bridge = getattr(request.app.state, "channel_bridge", None)
    if bridge is None:
        raise HTTPException(status_code=503, detail="Channel bridge not configured")

    body = await request.json()
    channel_name = body.get("channel", "")
    content = body.get("content", "")
    conversation_id = body.get("conversation_id", "")

    if not channel_name or not content:
        raise HTTPException(
            status_code=400,
            detail="'channel' and 'content' are required",
        )

    ok = bridge.send(channel_name, content, conversation_id=conversation_id)
    if not ok:
        raise HTTPException(status_code=502, detail="Failed to send message")
    return {"status": "sent", "channel": channel_name}


@router.get("/v1/channels/status")
async def channel_status(request: Request):
    """Return channel bridge connection status."""
    bridge = getattr(request.app.state, "channel_bridge", None)
    if bridge is None:
        return {"status": "not_configured"}
    return {"status": bridge.status().value}


# ---------------------------------------------------------------------------
# Security scan endpoint
# ---------------------------------------------------------------------------


@router.get("/v1/security/scan")
async def security_scan():
    """Run a read-only security environment audit and return findings."""
    from openjarvis.cli.scan_cmd import PrivacyScanner

    scanner = PrivacyScanner()
    results = scanner.run_all()
    return {
        "has_warnings": any(r.status == "warn" for r in results),
        "has_failures": any(r.status == "fail" for r in results),
        "findings": [
            {
                "name": r.name,
                "status": r.status,
                "message": r.message,
                "platform": r.platform,
            }
            for r in results
        ],
    }


__all__ = ["router"]


# ---------------------------------------------------------------------------
# Post-turn hook fan-out (Rounds 2.1 + 3.5)
# ---------------------------------------------------------------------------

def _extract_latest_user_text_from_messages(messages) -> str:
    """Pull the last user message text from a Message list. Best-effort."""
    for m in reversed(messages or []):
        try:
            role = m.role.value if hasattr(m.role, "value") else m.role
        except Exception:
            role = ""
        if role == "user":
            return getattr(m, "content", "") or ""
    return ""


def _fire_post_turn_hooks_safe(*, request, latest_user_text: str,
                                assistant_text: str, complexity_info=None) -> None:
    """Fire reflector + distillation feedback callbacks off-path.

    Both hooks run in background threads inside their own implementations;
    this function just calls them and swallows any exception so the live
    chat response is never affected.

    Round 2.1 — reflector posts a structured critique of (user, assistant)
    to ~/.openjarvis/reflections/<session>.jsonl.
    Round 3.5 — distillation queues turns with low-confidence reflection
    OR explicit failure markers to the OnDemandTrigger pipeline so
    OpenJarvis learns from mistakes in near-real-time.
    """
    if not latest_user_text or not assistant_text:
        return

    # Derive session id from request headers when available; fallback to a
    # stable hash so per-session reflection history aggregates correctly
    # even for stateless clients.
    session_id = ""
    try:
        if request is not None:
            session_id = (
                request.headers.get("X-OpenJarvis-Session-Id")
                or request.headers.get("X-Hermes-Session-Id")
                or ""
            ).strip()
    except Exception:
        session_id = ""
    if not session_id:
        import hashlib as _h
        session_id = _h.sha1((latest_user_text[:80] or "anon").encode("utf-8", errors="replace")).hexdigest()[:12]

    # Complexity score for reflector context.
    complexity_score = None
    try:
        if complexity_info is not None:
            complexity_score = float(getattr(complexity_info, "score", None) or
                                       (complexity_info.get("score") if isinstance(complexity_info, dict) else 0.0))
    except Exception:
        complexity_score = None

    # Round 5.6 — infer per-request domain instead of hardcoding "general"
    inferred_domain = "general"
    try:
        from openjarvis.learning.domain_classifier import infer_domain as _id
        _signals = None
        if complexity_info is not None:
            _signals = getattr(complexity_info, "signals", None)
            if _signals is None and isinstance(complexity_info, dict):
                _signals = complexity_info.get("signals")
        inferred_domain = _id(latest_user_text, _signals)
    except Exception:
        pass

    # Round 2.1 — Reflector
    try:
        from openjarvis.learning.reflector import reflect_async as _reflect_async
        _reflect_async(
            session_id=session_id,
            user_text=latest_user_text,
            assistant_text=assistant_text,
            domain=inferred_domain,
            complexity=complexity_score,
        )
    except Exception:
        pass

    # Round 3.5 — Online distillation trigger.  Currently fires only for
    # explicit failure markers in the response text; once Round 2.1's
    # reflector confidence is flowing, this gate broadens to include
    # low-confidence reflections (read via reflector.last_reflection).
    try:
        if os.getenv("OPENJARVIS_DISTILLER_ONLINE_ENABLED", "false").lower() in ("1", "true", "yes", "on"):
            looks_failed = any(
                marker in assistant_text.lower() for marker in (
                    "i'm sorry", "i cannot", "i can't", "i don't know",
                    "unable to", "i'm not able", "error:", "[no result]",
                )
            )
            if looks_failed:
                # Round 5.9 — corpus-shim distiller. Replaces the previous
                # DistillationOrchestrator() call which crashed at construction
                # (it requires 9 dependency singletons not built today). The
                # shim writes a structured JSONL record per failed turn,
                # producing real, verifiable training data.
                try:
                    from openjarvis.learning import online_distiller as _od
                    # We don't have a full reflection here (this fires before
                    # reflector finishes its async critique), so synthesise
                    # one with the explicit_failed flag set.
                    _od.queue(
                        {"confidence": 0.0, "success": False, "refusal_risk": True,
                         "tags": ["explicit-failure-marker"], "domain": inferred_domain},
                        user_text=latest_user_text,
                        assistant_text=assistant_text,
                        session_id=session_id,
                        explicit_failed=True,
                    )
                except Exception as _dexc:
                    logging.getLogger("openjarvis.server").debug(
                        "online_distiller queue failed: %s", _dexc,
                    )
    except Exception:
        pass
