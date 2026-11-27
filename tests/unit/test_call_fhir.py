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

"""call_fhir: a rejected cached token is dropped and the request retried once; the FHIR
server's 401 and 403 become OperationOutcomes that say what happened."""

from typing import Any, List

import pytest

from fhirpy.base.exceptions import AuthorizationError, ForbiddenError, OperationOutcome
from mcp.server.auth.provider import AccessToken

from fhir_mcp_server import server as server_module

USER = AccessToken(token="mcp-token", client_id="mcp-client", scopes=["fhir_read"], subject="user-1")


@pytest.fixture
def setup(monkeypatch: pytest.MonkeyPatch) -> List[Any]:
    """token_vault mode with a fake client; returns the list of forgotten subjects."""
    forgotten: List[Any] = []
    monkeypatch.setattr(server_module.configs, "server_auth_mode", "token_vault")
    monkeypatch.setattr(server_module, "get_access_token", lambda: USER)

    async def fake_client() -> object:
        return object()

    async def forget(sub: Any) -> None:
        forgotten.append(sub)

    monkeypatch.setattr(server_module, "get_async_fhir_client", fake_client)
    monkeypatch.setattr(server_module.token_exchange_provider, "forget", forget)
    return forgotten


def failing(*errors: Exception) -> Any:
    """An operation that raises the given errors in turn, then returns 'ok'."""
    calls: List[int] = []

    async def operation(client: object) -> str:
        calls.append(1)
        if len(calls) <= len(errors):
            raise errors[len(calls) - 1]
        return "ok"

    operation.calls = calls  # type: ignore[attr-defined]
    return operation


async def test_rejected_cached_token_is_dropped_and_retried_once(setup):
    operation = failing(AuthorizationError(""))

    assert await server_module.call_fhir(operation) == "ok"
    assert setup == ["user-1"], "the cached token is dropped for the caller"
    assert len(operation.calls) == 2


async def test_second_rejection_reports_the_401(setup):
    operation = failing(AuthorizationError(""), AuthorizationError(""))

    with pytest.raises(OperationOutcome) as error:
        await server_module.call_fhir(operation)

    issue = error.value.resource["issue"][0]
    assert issue["code"] == "login" and "HTTP 401" in issue["diagnostics"]
    assert len(operation.calls) == 2, "only one retry"


async def test_forbidden_is_reported_without_retry(setup):
    operation = failing(ForbiddenError(""))

    with pytest.raises(OperationOutcome) as error:
        await server_module.call_fhir(operation)

    issue = error.value.resource["issue"][0]
    assert issue["code"] == "forbidden" and "HTTP 403" in issue["diagnostics"]
    assert setup == [] and len(operation.calls) == 1


async def test_no_retry_in_modes_without_a_cached_token(setup, monkeypatch):
    monkeypatch.setattr(server_module.configs, "server_auth_mode", "none")
    operation = failing(AuthorizationError(""))

    with pytest.raises(OperationOutcome):
        await server_module.call_fhir(operation)
    assert setup == [] and len(operation.calls) == 1
