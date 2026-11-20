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

"""Where the server keeps per-user tokens and pending sign-ins.

The default keeps them in this process's memory: nothing touches disk, and a
restart forgets every connection. A deployment that needs connections to survive
restarts, or several server replicas, can plug in its own store (Redis, a
database, a token vault) with FHIR_MCP_TOKEN_STORE, without changing this code:

    FHIR_MCP_TOKEN_STORE=my_package.stores:create_store

`create_store(configs)` receives the server's ServerConfigs and returns an object
with the four async methods of `TokenStore`. It may be called more than once (one
store per component); read its own settings, such as a Redis URL, from the
environment. A plugged-in store must:

- keep values only until `ttl_seconds` passes;
- make `pop` atomic (read and delete in one step), because tickets and sign-in
  states are single-use and replaying one must fail;
- encrypt values at rest, because they include FHIR access and refresh tokens;
- accept JSON-compatible values (dicts, lists, strings, numbers, booleans, None).

Token refreshes are serialised within one process only. With several replicas
sharing a store, two may refresh the same user's FHIR token at once; with rolling
refresh tokens one of them fails, and that user is asked to connect again.
"""

import importlib

from typing import Any, Callable, Protocol, runtime_checkable

from fhir_mcp_server.memory_store import MemoryStore
from fhir_mcp_server.oauth.types import ConfigurationError, ServerConfigs

MEMORY = "memory"
_METHODS = ("get", "put", "pop", "delete")


@runtime_checkable
class TokenStore(Protocol):
    """A key-value store with a per-entry expiry. Values are JSON-compatible."""

    async def get(self, key: str) -> Any | None:
        """The value, or None if it is missing or expired."""

    async def put(self, key: str, value: Any, ttl_seconds: float) -> None:
        """Store the value, replacing any old one, for `ttl_seconds`."""

    async def pop(self, key: str) -> Any | None:
        """Return the value and delete it in one atomic step (single-use entries)."""

    async def delete(self, key: str) -> None:
        """Remove the value if present."""


class MemoryTokenStore:
    """The default: this process's memory. Lost on restart, not shared by replicas."""

    def __init__(self) -> None:
        self._items = MemoryStore()

    async def get(self, key: str) -> Any | None:
        return self._items.get(key)

    async def put(self, key: str, value: Any, ttl_seconds: float) -> None:
        self._items.put(key, value, ttl_seconds)

    async def pop(self, key: str) -> Any | None:
        return self._items.pop(key)

    async def delete(self, key: str) -> None:
        self._items.delete(key)


def store_factory(setting: str) -> Callable[[ServerConfigs], Any]:
    """The factory named by FHIR_MCP_TOKEN_STORE (`module:function`)."""
    module_name, _, attribute = setting.partition(":")
    if not module_name or not attribute:
        raise ConfigurationError(
            f"FHIR_MCP_TOKEN_STORE={setting!r} must be 'memory' or 'module:function', "
            "for example my_package.stores:create_store."
        )
    try:
        factory = getattr(importlib.import_module(module_name), attribute)
    except (ImportError, AttributeError) as ex:
        raise ConfigurationError(f"FHIR_MCP_TOKEN_STORE={setting!r} could not be loaded: {ex}") from ex
    if not callable(factory):
        raise ConfigurationError(f"FHIR_MCP_TOKEN_STORE={setting!r} is not callable.")
    return factory


def create_token_store(configs: ServerConfigs) -> TokenStore:
    """The token store chosen by FHIR_MCP_TOKEN_STORE; in memory by default."""
    setting = (configs.mcp_token_store or MEMORY).strip()
    if setting == MEMORY:
        return MemoryTokenStore()
    store = store_factory(setting)(configs)
    missing = [name for name in _METHODS if not callable(getattr(store, name, None))]
    if missing:
        raise ConfigurationError(
            f"The store from FHIR_MCP_TOKEN_STORE={setting!r} lacks the method(s): {', '.join(missing)}."
        )
    return store
