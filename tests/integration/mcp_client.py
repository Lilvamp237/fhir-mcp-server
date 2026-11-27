# Copyright (c) 2026, Choreo Labs LLC. (https://choreolabs.com)

# Choreo Labs licenses this file to you under the Apache License,
# Version 2.0 (the "License"); you may not use this file except
# in compliance with the License.
# You may obtain a copy of the License at

# http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied. See the License for the
# specific language governing permissions and limitations
# under the License.

"""One MCP client API for the integration tests.

The tests talk to the server only through `connect()`. Results are normalised
to plain Python values, so the tests compare data, not SDK model classes.

Protocol modes:
- "legacy": the `initialize` handshake used by pre-2026 clients.
- "2026-07-28": the stateless protocol.
"""

import json

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, List

import httpx2

from mcp import Client
from mcp.client.streamable_http import streamable_http_client

LEGACY = "legacy"
MODERN = "2026-07-28"
PROTOCOL_MODES = [LEGACY, MODERN]


@dataclass
class ToolCall:
    is_error: bool
    structured: Any
    text: Any  # the first text content block, parsed as JSON when possible


def _normalise_call(result: Any) -> ToolCall:
    text: Any = None
    for block in result.content:
        if block.type == "text":
            try:
                text = json.loads(block.text)
            except ValueError:
                text = block.text
            break
    return ToolCall(
        is_error=bool(result.is_error),
        structured=result.structured_content,
        text=text,
    )


def _normalise_tool(tool: Any) -> Dict[str, Any]:
    data = tool.model_dump(mode="json", by_alias=True, exclude_none=True)
    return {
        key: data[key]
        for key in ("name", "description", "inputSchema", "outputSchema")
        if key in data
    }


class Session:
    def __init__(self, client: Any, protocol_version: str | None) -> None:
        self._client = client
        self.protocol_version = protocol_version

    async def list_tools(self) -> List[Dict[str, Any]]:
        result = await self._client.list_tools()
        return sorted((_normalise_tool(t) for t in result.tools), key=lambda t: t["name"])

    async def call_tool(self, name: str, arguments: Dict[str, Any] | None = None) -> ToolCall:
        return _normalise_call(await self._client.call_tool(name, arguments or {}))


@asynccontextmanager
async def connect(
    target: Any, mode: str = LEGACY, token: str | None = None, elicitation_callback: Any = None
) -> AsyncIterator[Session]:
    """Connect to an in-process server object or a Streamable HTTP URL.

    With `token`, every HTTP request carries `Authorization: Bearer <token>`.
    With `elicitation_callback`, the client declares elicitation support and
    answers the server's elicitation requests with it.
    """
    if token is not None:
        http_client = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30)
        target = streamable_http_client(target, http_client=http_client)
    async with Client(target, mode=mode, elicitation_callback=elicitation_callback) as client:
        yield Session(client, client.protocol_version)
