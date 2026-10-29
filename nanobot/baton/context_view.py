"""The exact-context view: what a resident's model sees.

``last_request_text`` returns the last request body the model queue forwarded
for the resident, byte for byte (pretty-printed unless ``raw``).
``assemble_next`` builds the next request the way the resident's AgentLoop
would for a given message: the same context builder (system prompt and
bundle), the stored history, and the same tool definitions.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from nanobot.baton.queue import last_request, pretty

if TYPE_CHECKING:
    from nanobot.config.schema import Config


def last_request_text(config: Config, resident: str, *, raw: bool = False) -> str | None:
    body = last_request(config.baton.capture_dir, resident)
    if body is None:
        return None
    return body.decode("utf-8", "replace") if raw else pretty(body)


async def assemble_next(config: Config, resident: str, message: str, *, session_key: str = "cli:direct", raw: bool = False) -> str:
    from nanobot.agent.loop import AgentLoop
    from nanobot.agent.tools.context import (
        RequestContext,
        bind_request_context,
        reset_request_context,
    )
    from nanobot.agent.tools.registry import ToolRegistry
    from nanobot.baton.town import Town
    from nanobot.session.summary import session_summary_from_metadata

    cfg = config.model_copy(deep=True)
    cfg.agents.defaults.workspace = str(Town.open(cfg.baton.town_path).resident_dir(resident))
    loop = AgentLoop.from_config(cfg, tool_registry=ToolRegistry())
    try:
        session = await loop.sessions.state.read(session_key)
        history = session.get_history() if session is not None else []
        summary = session_summary_from_metadata(session.metadata, fallback_last_active=session.updated_at) if session is not None else None
        channel = session_key.split(":", 1)[0] if ":" in session_key else "cli"
        token = bind_request_context(RequestContext(channel=channel, chat_id="direct", session_key=session_key))
        try:
            messages = loop.context.build_messages(history=history, current_message=message, channel=channel, session_summary=summary)
        finally:
            reset_request_context(token)
        body: dict[str, Any] = {
            "model": cfg.resolve_preset().model,
            "messages": [{k: v for k, v in m.items() if not k.startswith("_")} for m in messages],
            "tools": loop.tools.get_definitions(),
        }
        text = json.dumps(body, ensure_ascii=False, separators=(",", ":")) if raw else json.dumps(body, ensure_ascii=False, indent=2)
        return text
    finally:
        await loop.aclose()
