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

"""A small in-memory key-value store with expiry.

Holds per-user FHIR tokens and pending sign-ins for a single server process.
Everything is lost on restart, and replicas do not share it; a shared store
(e.g. Redis) can replace it behind the same methods.
"""

import time

from typing import Any, Dict, Tuple


class MemoryStore:
    def __init__(self) -> None:
        self._items: Dict[str, Tuple[float, Any]] = {}

    def put(self, key: str, value: Any, ttl_seconds: float) -> None:
        self._purge()
        self._items[key] = (time.time() + ttl_seconds, value)

    def get(self, key: str) -> Any | None:
        item = self._items.get(key)
        if item is None:
            return None
        expires_at, value = item
        if expires_at <= time.time():
            self._items.pop(key, None)
            return None
        return value

    def pop(self, key: str) -> Any | None:
        """Return the value and remove it, so single-use entries cannot be replayed."""
        value = self.get(key)
        self._items.pop(key, None)
        return value

    def delete(self, key: str) -> None:
        self._items.pop(key, None)

    def _purge(self) -> None:
        now = time.time()
        for key in [k for k, (expires_at, _) in self._items.items() if expires_at <= now]:
            self._items.pop(key, None)

