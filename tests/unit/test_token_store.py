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

"""The pluggable token store (FHIR_MCP_TOKEN_STORE)."""

import json
import time

from types import SimpleNamespace
from typing import Any, Dict, List, Tuple
from unittest.mock import patch

import pytest

from fhir_mcp_server import memory_store as memory_store_module
from fhir_mcp_server.oauth.types import ConfigurationError, ServerConfigs
from fhir_mcp_server.token_store import MemoryTokenStore, TokenStore, create_token_store


class RecordingStore:
    """A stand-in for a shared store: values go through JSON, as they would to Redis."""

    instances: List["RecordingStore"] = []

    def __init__(self, configs: ServerConfigs) -> None:
        self.configs = configs
        self.items: Dict[str, Tuple[float, str]] = {}
        RecordingStore.instances.append(self)

    async def get(self, key: str) -> Any | None:
        item = self.items.get(key)
        if item is None or item[0] <= time.time():
            return None
        return json.loads(item[1])

    async def put(self, key: str, value: Any, ttl_seconds: float) -> None:
        self.items[key] = (time.time() + ttl_seconds, json.dumps(value))

    async def pop(self, key: str) -> Any | None:
        value = await self.get(key)
        self.items.pop(key, None)
        return value

    async def delete(self, key: str) -> None:
        self.items.pop(key, None)


def create_recording_store(configs: ServerConfigs) -> RecordingStore:
    return RecordingStore(configs)


def create_incomplete_store(configs: ServerConfigs) -> Any:
    return SimpleNamespace(get=lambda key: None)


NOT_CALLABLE = "a string"


def configs(**values: Any) -> ServerConfigs:
    return ServerConfigs(_env_file=None, **values)


class TestMemoryTokenStore:
    async def test_stores_until_the_entry_expires(self):
        store = MemoryTokenStore()
        await store.put("k", {"a": 1}, 60)
        assert await store.get("k") == {"a": 1}

        later = SimpleNamespace(time=lambda: time.time() + 61)
        with patch.object(memory_store_module, "time", later):
            assert await store.get("k") is None

    async def test_pop_is_single_use(self):
        store = MemoryTokenStore()
        await store.put("ticket", "user-1", 60)
        assert await store.pop("ticket") == "user-1"
        assert await store.pop("ticket") is None

    async def test_delete(self):
        store = MemoryTokenStore()
        await store.put("k", "v", 60)
        await store.delete("k")
        assert await store.get("k") is None


class TestCreateTokenStore:
    def test_memory_by_default(self):
        store = create_token_store(configs())
        assert isinstance(store, MemoryTokenStore)
        assert isinstance(store, TokenStore)

    def test_loads_a_custom_store_from_the_setting(self):
        settings = configs(mcp_token_store=f"{__name__}:create_recording_store")
        store = create_token_store(settings)
        assert isinstance(store, RecordingStore)
        assert store.configs is settings, "the factory receives the server's settings"

    @pytest.mark.parametrize(
        "setting, message",
        [
            ("redis", "must be 'memory' or 'module:function'"),
            ("no_such_module_xyz:create", "could not be loaded"),
            (f"{__name__}:no_such_factory", "could not be loaded"),
            (f"{__name__}:NOT_CALLABLE", "is not callable"),
            (f"{__name__}:create_incomplete_store", "lacks the method(s): put, pop, delete"),
        ],
    )
    def test_rejects_a_setting_that_does_not_give_a_store(self, setting, message):
        with pytest.raises(ConfigurationError, match=message.replace("(", r"\(").replace(")", r"\)")):
            create_token_store(configs(mcp_token_store=setting))

    def test_startup_check_reports_a_store_that_cannot_load(self):
        settings = configs(mcp_auth_mode="disabled", mcp_token_store="no_such_module_xyz:create")
        with pytest.raises(ConfigurationError, match="could not be loaded"):
            settings.validate_for_startup()
