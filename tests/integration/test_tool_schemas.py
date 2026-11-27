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

"""Tool names, descriptions and input/output schemas must not change by accident.

The snapshot was recorded on SDK 1.27.1 and still matches on SDK 2.x. To
accept an intended change, review the diff and re-record with:

    UPDATE_TOOL_SNAPSHOT=1 pytest tests/integration/test_tool_schemas.py
"""

import json
import os

from pathlib import Path
from typing import Any

from tests.integration.mcp_client import connect

SNAPSHOT = Path(__file__).parent / "snapshots" / "tools.json"


async def test_tool_list_matches_snapshot(mcp_server: Any, protocol_mode: str) -> None:
    async with connect(mcp_server, protocol_mode) as session:
        tools = await session.list_tools()

    if os.environ.get("UPDATE_TOOL_SNAPSHOT") == "1":
        SNAPSHOT.parent.mkdir(exist_ok=True)
        SNAPSHOT.write_text(json.dumps(tools, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert [t["name"] for t in tools] == [t["name"] for t in expected]
    for actual, recorded in zip(tools, expected):
        assert actual == recorded, f"tool '{actual['name']}' differs from the snapshot"
