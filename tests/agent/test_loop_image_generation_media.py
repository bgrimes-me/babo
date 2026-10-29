from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from babo.agent.loop import AgentLoop
from babo.agent.tools.image_generation import register_image_generation_tool
from babo.agent.tools.registry import ToolRegistry
from babo.bus.events import InboundMessage
from babo.bus.queue import MessageBus
from babo.config.loader import set_config_path
from babo.config.schema import Config
from babo.providers.base import LLMResponse, ToolCallRequest
from babo.providers.image_generation import GeneratedImageResponse

PNG_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


class FakeImageClient:
    def __init__(self, **kwargs: Any) -> None:
        pass

    async def generate(self, **kwargs: Any) -> GeneratedImageResponse:
        return GeneratedImageResponse(images=[PNG_DATA_URL], content="", raw={})


@pytest.mark.asyncio
async def test_generate_image_does_not_attach_media_to_final_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generating an image does not attach it to the final outbound response."""
    set_config_path(tmp_path / "config.json")
    monkeypatch.setattr(
        "babo.agent.tools.image_generation.get_image_gen_provider",
        lambda name: FakeImageClient if name == "openrouter" else None,
    )
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation.max_tokens = 4096
    provider.chat_stream_with_retry = AsyncMock(
        side_effect=[
            LLMResponse(
                content="",
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCallRequest(
                        id="call_img",
                        name="generate_image",
                        arguments={"prompt": "draw a tiny icon"},
                    )
                ],
            ),
            LLMResponse(content="Done", finish_reason="stop"),
        ]
    )
    config = Config()
    config.tools.image_generation.enabled = True
    config.providers.openrouter.api_key = "sk-or-test"
    tools = ToolRegistry()
    register_image_generation_tool(config, tools, workspace=tmp_path)
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        tools_config=config.tools,
        tool_registry=tools,
    )

    result = await loop._process_message(
        InboundMessage(
            channel="websocket",
            sender_id="user",
            chat_id="chat-image",
            content="draw an icon",
        )
    )

    assert result is not None
    assert result.content == "Done"
    # Image delivery requires a separate message tool call.
    assert result.media == []
