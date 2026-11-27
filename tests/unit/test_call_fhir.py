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

"""call_fhir: a rejected token is refreshed and the request retried once; the FHIR
server's 401 and 403 become OperationOutcomes that say what happened."""

from typing import Any, List

import pytest

from fhirpy.base.exceptions import AuthorizationError, ForbiddenError, OperationOutcome
from mcp.server.auth.provider import AccessToken

from fhir_mcp_server import server as server_module
from fhir_mcp_server.backend_auth import BackendAuthError

AGENT = AccessToken(token="mcp-token", client_id="agent-1", scopes=["fhir_read"], subject="agent-1")


@pytest.fixture
def setup(monkeypatch: pytest.MonkeyPatch) -> List[Any]:
    """Agent sign-in on, with a fake client; returns the (sub, client) pairs whose token expired."""
    expired: List[Any] = []
    monkeypatch.setattr(server_module.configs, "server_client_id", "fhir-app")
    monkeypatch.setattr(server_module, "get_access_token", lambda: AGENT)

    async def fake_client() -> object:
        return object()

    async def expire_token(sub: str, client_id: str = "") -> bool:
        expired.append((sub, client_id))
        return True

    monkeypatch.setattr(server_module, "get_async_fhir_client", fake_client)
    monkeypatch.setattr(server_module.fhir_connect, "expire_token", expire_token)
    return expired


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


async def test_rejected_token_is_expired_and_retried_once(setup):
    operation = failing(AuthorizationError(""))

    assert await server_module.call_fhir(operation) == "ok"
    assert setup == [("agent-1", "agent-1")], "the calling agent's token is expired"
    assert len(operation.calls) == 2


async def test_second_rejection_reports_the_401(setup):
    operation = failing(AuthorizationError(""), AuthorizationError(""))

    with pytest.raises(OperationOutcome) as error:
        await server_module.call_fhir(operation)

    issue = error.value.resource["issue"][0]
    assert issue["code"] == "login" and "HTTP 401" in issue["diagnostics"]
    assert len(operation.calls) == 2, "only one retry"


async def test_session_lost_on_retry_reports_the_401(setup, monkeypatch):
    """The refresh failed, so the session ended: the retry has no token to use."""
    calls: List[int] = []

    async def client_then_none() -> object:
        calls.append(1)
        if len(calls) > 1:
            raise BackendAuthError("No user has linked this agent")
        return object()

    monkeypatch.setattr(server_module, "get_async_fhir_client", client_then_none)

    with pytest.raises(OperationOutcome) as error:
        await server_module.call_fhir(failing(AuthorizationError("")))
    assert error.value.resource["issue"][0]["code"] == "login"


async def test_forbidden_is_reported_without_retry(setup):
    operation = failing(ForbiddenError(""))

    with pytest.raises(OperationOutcome) as error:
        await server_module.call_fhir(operation)

    issue = error.value.resource["issue"][0]
    assert issue["code"] == "forbidden" and "HTTP 403" in issue["diagnostics"]
    assert setup == [] and len(operation.calls) == 1


async def test_no_retry_without_fhir_sign_in(setup, monkeypatch):
    monkeypatch.setattr(server_module.configs, "server_client_id", "")
    operation = failing(AuthorizationError(""))

    with pytest.raises(OperationOutcome):
        await server_module.call_fhir(operation)
    assert setup == [] and len(operation.calls) == 1
