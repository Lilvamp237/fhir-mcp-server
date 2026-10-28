# Copyright (c) 2026, WSO2 LLC. (https://www.wso2.com/) All Rights Reserved.

# WSO2 LLC. licenses this file to you under the Apache License,
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

"""Per-tool scopes.

The SDK enforces one scope set for the whole MCP endpoint. Tools here need
different scopes (reading vs. changing vs. deleting FHIR data), so a tool call
whose token lacks the tool's scope is answered with HTTP 403
`insufficient_scope` naming the missing scope, as the MCP authorization
specification describes for step-up authorization.
"""

import json
import logging

from typing import Dict, List

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from fhir_mcp_server.oauth.types import ServerConfigs

logger: logging.Logger = logging.getLogger(__name__)

READ_TOOLS = ("get_capabilities", "search", "read", "get_user")
WRITE_TOOLS = ("create", "update")
DELETE_TOOLS = ("delete",)


def tool_scopes(configs: ServerConfigs) -> Dict[str, str]:
    """The scope each tool requires."""
    scopes: Dict[str, str] = {}
    scopes.update({tool: configs.mcp_read_scope for tool in READ_TOOLS})
    scopes.update({tool: configs.mcp_write_scope for tool in WRITE_TOOLS})
    scopes.update({tool: configs.mcp_delete_scope for tool in DELETE_TOOLS})
    return scopes


def _called_tools(body: bytes) -> List[str]:
    """Names of the tools a JSON-RPC request body calls, if any."""
    try:
        payload = json.loads(body)
    except ValueError:
        return []
    messages = payload if isinstance(payload, list) else [payload]
    names = []
    for message in messages:
        if isinstance(message, dict) and message.get("method") == "tools/call":
            params = message.get("params") or {}
            if isinstance(params, dict) and isinstance(params.get("name"), str):
                names.append(params["name"])
    return names


class ToolScopeMiddleware:
    """ASGI middleware, placed after the SDK's bearer authentication."""

    def __init__(self, app: ASGIApp, scopes: Dict[str, str], resource_metadata_url: str) -> None:
        self.app = app
        self.scopes = scopes
        self.resource_metadata_url = resource_metadata_url

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        auth = scope.get("auth")
        authenticated = getattr(scope.get("user"), "is_authenticated", False)
        if scope["type"] != "http" or scope.get("method") != "POST" or auth is None or not authenticated:
            # Unauthenticated requests are answered 401 by the SDK's RequireAuthMiddleware.
            await self.app(scope, receive, send)
            return

        messages: List[Message] = []
        body = b""
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] != "http.request":
                break
            body += message.get("body", b"")
            if not message.get("more_body"):
                break

        missing = sorted(
            {
                self.scopes[name]
                for name in _called_tools(body)
                if name in self.scopes and self.scopes[name] not in auth.scopes
            }
        )
        if missing:
            logger.info("Rejected tool call: token lacks scope(s) %s", " ".join(missing))
            await self._insufficient_scope(send, " ".join(missing))
            return

        async def replay() -> Message:
            return messages.pop(0) if messages else await receive()

        await self.app(scope, replay, send)

    async def _insufficient_scope(self, send: Send, required: str) -> None:
        description = f"This tool requires the scope: {required}"
        challenge = (
            f'Bearer error="insufficient_scope", scope="{required}", '
            f'resource_metadata="{self.resource_metadata_url}", error_description="{description}"'
        )
        body = json.dumps({"error": "insufficient_scope", "error_description": description}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"www-authenticate", challenge.encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
