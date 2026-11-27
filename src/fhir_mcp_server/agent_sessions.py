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

"""The agent session map: which user each agent acts for, and what it may do.

An agent (for example one Claude Code instance, registered at the identity
provider) calls the MCP server with its own access token. The user it acts for
links it once in the browser: they choose which tools it may use, then sign in
at the FHIR server. The session keeps, per agent, that user's FHIR tokens and the
tools they allowed. Each agent acts for one user.

Sessions live in this process's memory only: nothing touches disk, a restart
forgets every session (the user links the agent again), and server replicas do
not share them. The same map holds the short-lived, single-use link tickets and
sign-in states of the browser flow.
"""

from typing import Any

from fhir_mcp_server.memory_store import MemoryStore


class AgentSessions:
    """A key-value map with a per-entry expiry. Values are JSON-compatible."""

    def __init__(self) -> None:
        self._items = MemoryStore()

    async def get(self, key: str) -> Any | None:
        """The value, or None if it is missing or expired."""
        return self._items.get(key)

    async def put(self, key: str, value: Any, ttl_seconds: float) -> None:
        """Store the value, replacing any old one, for `ttl_seconds`."""
        self._items.put(key, value, ttl_seconds)

    async def pop(self, key: str) -> Any | None:
        """Return the value and delete it in one step (single-use entries)."""
        return self._items.pop(key)

    async def delete(self, key: str) -> None:
        """Remove the value if present."""
        self._items.delete(key)
