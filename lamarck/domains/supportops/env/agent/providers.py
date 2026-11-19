"""LLM provider protocol and errors used by the agent loop.

(lamarck: the original module also contained an OpenAI-SDK provider and a scripted provider.
Inside the sandbox the only provider is the gateway-backed one in
lamarck.domains.supportops.runtime, so this file keeps just the interface.)
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from .types import LLMResponse, Message, ToolSpec


class LLMError(Exception):
    """A provider failure that retrying will not fix (bad key, bad request, ...)."""


class TransientLLMError(LLMError):
    """A failure worth retrying (rate limit, timeout, 5xx, connection reset)."""


class LLMProvider(Protocol):
    name: str
    model: str

    async def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> LLMResponse: ...
