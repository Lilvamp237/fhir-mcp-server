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

"""A minimal agent on the MCP Python SDK that signs in with client credentials.

The SDK's ClientCredentialsOAuthProvider finds the identity provider from the MCP
server's metadata, gets a client credentials token, and gets a new one whenever
the server answers 401 (for example after the token expired). Nothing here
handles tokens. When the server sends its consent link (URL elicitation), the
link is printed: open it, choose the tools the agent may use, sign in at the FHIR
server, then press Enter.

Settings: the JSON file named by FHIR_AGENT_CONFIG, by default
`~/.fhir-agent/agent.json` (see agent.example.json; `token_endpoint` is not used
here, the SDK discovers it).

Usage:
  python examples/agents/fhir_agent.py                  # interactive: type tool calls
  python examples/agents/fhir_agent.py read '{"type": "Patient", "id": "123"}'
"""

import asyncio
import json
import os
import ssl
import sys

from pathlib import Path
from typing import Any

import anyio
import httpx2

from mcp import Client
from mcp.client.auth.extensions.client_credentials import ClientCredentialsOAuthProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from mcp_types import ElicitResult

CONFIG = Path(os.environ.get("FHIR_AGENT_CONFIG", Path.home() / ".fhir-agent" / "agent.json"))


class MemoryTokenStorage:
    """Keeps the agent's token for this run only."""

    def __init__(self) -> None:
        self._tokens: OAuthToken | None = None
        self._client_info: OAuthClientInformationFull | None = None

    async def get_tokens(self) -> OAuthToken | None:
        return self._tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self._tokens = tokens

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return self._client_info

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self._client_info = client_info


async def ask_user(context: Any, params: Any) -> ElicitResult:
    """The server asks the user to open a link: print it and wait for them."""
    print(f"\n{params.message}\n  {params.url}\n")
    answer = await anyio.to_thread.run_sync(input, "Press Enter when done, or type d to decline: ")
    return ElicitResult(action="decline" if answer.strip().lower() == "d" else "accept")


def show(result: Any) -> None:
    for block in result.content:
        if block.type == "text":
            try:
                print(json.dumps(json.loads(block.text), indent=2))
            except ValueError:
                print(block.text)


async def main() -> None:
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    auth = ClientCredentialsOAuthProvider(
        server_url=config["resource"],
        storage=MemoryTokenStorage(),
        client_id=config["client_id"],
        client_secret=config["client_secret"],
        scope=config.get("scope"),
        issuer=config["issuer"],  # the agent's secret is only ever sent to this IdP
    )
    # With ca_cert, trust only that CA (an IdP with a self-signed certificate); else the system's CAs.
    tls = ssl.create_default_context(cafile=config.get("ca_cert") or None)
    http_client = httpx2.AsyncClient(auth=auth, verify=tls, timeout=60)
    target = streamable_http_client(config["resource"], http_client=http_client)
    async with Client(target, mode="2026-07-28", elicitation_callback=ask_user) as client:
        tools = await client.list_tools()
        print("Connected. Tools:", ", ".join(tool.name for tool in tools.tools))
        if len(sys.argv) > 1:
            arguments = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
            show(await client.call_tool(sys.argv[1], arguments))
            return
        print('Type a tool call as `<tool> <json arguments>`, for example: read {"type": "Patient", "id": "123"}')
        while True:
            line = (await anyio.to_thread.run_sync(input, "\nagent> ")).strip()
            if line in ("", "exit", "quit"):
                break
            tool, _, arguments = line.partition(" ")
            try:
                show(await client.call_tool(tool, json.loads(arguments) if arguments else {}))
            except Exception as ex:
                print(f"{type(ex).__name__}: {ex}")


if __name__ == "__main__":
    asyncio.run(main())
