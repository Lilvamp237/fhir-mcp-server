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

"""The Streamable HTTP endpoint, as real clients reach it.

One endpoint serves both legacy (`initialize` handshake) clients and
2026-07-28 clients.
"""

from tests.integration.mcp_client import LEGACY, MODERN, connect


async def test_legacy_client_over_http(mcp_http_url: str) -> None:
    async with connect(mcp_http_url, LEGACY) as session:
        assert session.protocol_version and session.protocol_version < "2026-07-28"
        tools = await session.list_tools()
        result = await session.call_tool("get_capabilities", {"type": "Patient"})

    assert [t["name"] for t in tools] == [
        "create", "delete", "get_capabilities", "get_user", "read", "search", "update"
    ]
    assert not result.is_error
    assert result.text["type"] == "Patient"


async def test_modern_client_over_http(mcp_http_url: str) -> None:
    async with connect(mcp_http_url, MODERN) as session:
        assert session.protocol_version == MODERN
        result = await session.call_tool("read", {"type": "Patient", "id": "123"})

    assert not result.is_error
    assert result.text["id"] == "123"


async def test_separate_connections_do_not_share_state(mcp_http_url: str, fhir_stub) -> None:
    # The server runs with stateless_http=True, so each connection stands alone.
    for _ in range(2):
        async with connect(mcp_http_url, LEGACY) as session:
            result = await session.call_tool("read", {"type": "Patient", "id": "123"})
            assert result.text["id"] == "123"

    assert fhir_stub.calls() == [("GET", "/Patient/123"), ("GET", "/Patient/123")]
