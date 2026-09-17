#!/usr/bin/env python3
"""providers — one adapter per provider wire format, behind a single canonical shape.

The gateway layer's whole job is to make four incompatible APIs look like one. Every
adapter translates into and out of the types declared here, so nothing upstream of this
package knows whether it is talking to OpenAI, Anthropic, Ollama or a test double.
"""

from .base import (
    ChatRequest,
    ChatResponse,
    Chunk,
    ContentBlock,
    FinishReason,
    GatewayError,
    ErrorKind,
    Message,
    Provider,
    ProviderCapabilities,
    Role,
    ToolCall,
    ToolSpec,
    Usage,
)

__all__ = [
    "ChatRequest",
    "ChatResponse",
    "Chunk",
    "ContentBlock",
    "FinishReason",
    "GatewayError",
    "ErrorKind",
    "Message",
    "Provider",
    "ProviderCapabilities",
    "Role",
    "ToolCall",
    "ToolSpec",
    "Usage",
]
