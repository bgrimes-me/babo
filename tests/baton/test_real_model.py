"""Optional tests against a real llama-server (e.g. a tiny CPU GGUF).

Skipped unless ``BATON_REAL_MODEL_URL`` points at a running llama-server, for
example one started with::

    llama-server -m tinyllama-1.1b-chat.Q4_K_M.gguf --jinja -c 4096 --port 8099

and ``BATON_REAL_MODEL_URL=http://127.0.0.1:8099``.
"""

from __future__ import annotations

import os

import httpx
import pytest

from babo.baton.config import BatonModelConfig
from babo.baton.model_client import BatonModelClient
from babo.baton.schemas import CHECKPOINT_SCHEMA, parse_reply

URL = os.environ.get("BATON_REAL_MODEL_URL", "").rstrip("/")


def _reachable() -> bool:
    if not URL:
        return False
    try:
        return httpx.get(URL + "/health", timeout=3).status_code == 200
    except httpx.HTTPError:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason="no real llama-server at BATON_REAL_MODEL_URL")


async def test_real_model_writes_a_valid_checkpoint() -> None:
    client = BatonModelClient(BatonModelConfig(endpoint=URL + "/v1", model="local", schema_max_tokens=600))
    messages = [
        {"role": "system", "content": "You are a resident of a small town of agents."},
        {"role": "user", "content": "Plan three raised garden beds."},
        {"role": "assistant", "content": "I sketched three 1x2 m beds facing south."},
        {"role": "user", "content": "Write your checkpoint as JSON: thread, doing, done, learned, next, blockers, confidence, knowledge_ops, promote."},
    ]
    result = await client.chat_json(messages, CHECKPOINT_SCHEMA, name="checkpoint")
    reply, error = parse_reply(result.data)
    assert reply is not None, f"{error}: {result.raw[:300]}"
