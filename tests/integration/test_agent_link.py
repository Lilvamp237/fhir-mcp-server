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

"""Linking an agent to its user (FHIR_SERVER_CLIENT_ID is set).

An agent calls with its own MCP token from the IdP stub. On the first FHIR call
the server asks the client to open a link (URL-mode elicitation). Following it in
a "browser" shows the consent page, where the user chooses the agent's tools,
then signs in at the SMART stub. The server keeps the user's FHIR token and the
chosen tools in the agent's session, and later calls use them.
"""

import re
import time

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Iterator, List
from unittest.mock import patch
from urllib.parse import urlsplit

import httpx2
import pytest

from mcp.shared.exceptions import UrlElicitationRequiredError
from mcp_types import ElicitResult

from fhir_mcp_server import fhir_connect as fhir_connect_module
from fhir_mcp_server import server as server_module
from fhir_mcp_server.fhir_connect import AGENTS_PATH, CONNECT_PATH, REVOKE_PATH
from fhir_mcp_server.oauth.types import ConfigurationError, ServerConfigs
from tests.integration.mcp_client import LEGACY, MODERN, connect
from tests.integration.stubs.smart_auth_stub import EHR_CLIENT, SmartAuthStub


@pytest.fixture
def smart_auth() -> Iterator[SmartAuthStub]:
    stub = SmartAuthStub()
    stub.start()
    try:
        yield stub
    finally:
        stub.stop()


@pytest.fixture
def serve_federated(serve_with_idp, smart_auth):
    # The FHIR sign-in names the same person as the IdP stub's default MCP token.
    smart_auth.fhir_user = "Practitioner/practitioner-1"

    @contextmanager
    def serve(scopes: str = "openid fhirUser user/Patient.read") -> Iterator[str]:
        with serve_with_idp(
            server_client_id=EHR_CLIENT[0],
            server_client_secret=EHR_CLIENT[1],
            server_authorize_url=f"{smart_auth.base_url}/authorize",
            server_token_url=f"{smart_auth.base_url}/token",
            server_scopes=scopes,
        ) as url:
            yield url

    return serve


def mcp_token(idp_stub, url: str, sub: str = "user-1") -> str:
    return idp_stub.token(url, "fhir-read", sub=sub)


def agent_token(idp_stub, url: str, scope: str = "fhir-read", agent: str = "agent-1") -> str:
    """A client credentials token: it names only the agent (no user, no fhirUser)."""
    return idp_stub.token(url, scope, sub=agent, client_id=agent, fhirUser=None)


def _form(page: str) -> tuple[str, List[str]]:
    ticket = re.search(r"name='ticket' value='([^']+)'", page)
    assert ticket, "the consent page carries the ticket"
    return ticket.group(1), re.findall(r"name='tools' value='([^']+)'", page)


async def consent(browser: httpx2.AsyncClient, link: str, tools: List[str] | None = None) -> httpx2.Response:
    """Open the link, choose tools (all offered ones by default) and submit."""
    page = await browser.get(link)
    if page.status_code != 200:
        return page
    ticket, offered = _form(page.text)
    origin = "{0.scheme}://{0.netloc}".format(urlsplit(link))
    chosen = offered if tools is None else tools
    return await browser.post(f"{origin}{CONNECT_PATH}", data={"ticket": ticket, "tools": chosen})


async def open_in_browser(link: str, tools: List[str] | None = None) -> httpx2.Response:
    """Follow the link through the consent page and the FHIR sign-in, as a browser would."""
    async with httpx2.AsyncClient(follow_redirects=True, timeout=30) as browser:
        return await consent(browser, link, tools)


class BrowserCallback:
    """Client elicitation handler that completes the link before accepting."""

    def __init__(self, tools: List[str] | None = None) -> None:
        self.tools = tools
        self.links: List[str] = []
        self.responses: List[httpx2.Response] = []

    async def __call__(self, context: Any, params: Any) -> ElicitResult:
        assert params.mode == "url"
        self.links.append(params.url)
        self.responses.append(await open_in_browser(params.url, self.tools))
        return ElicitResult(action="accept")


async def test_first_call_asks_to_link_then_uses_the_users_fhir_token(
    serve_federated, idp_stub, smart_auth, fhir_stub
):
    callback = BrowserCallback()
    with serve_federated() as url:
        async with connect(url, MODERN, token=mcp_token(idp_stub, url), elicitation_callback=callback) as session:
            first = await session.call_tool("read", {"type": "Patient", "id": "123"})
            second = await session.call_tool("read", {"type": "Patient", "id": "123"})

    assert first.text["id"] == "123" and second.text["id"] == "123"
    assert len(callback.links) == 1, "only the first call should ask the user to link the agent"
    assert "/connect/fhir?ticket=" in callback.links[0]
    assert callback.responses[0].status_code == 200
    assert [r.authorization for r in fhir_stub.requests] == ["Bearer ehr-user-token-1"] * 2

    sign_in = smart_auth.authorize_requests[0]
    assert sign_in["client_id"] == EHR_CLIENT[0]
    assert sign_in["aud"] == fhir_stub.base_url
    assert sign_in["code_challenge_method"] == "S256"
    assert sign_in["scope"] == "openid fhirUser user/Patient.read"


async def test_agent_token_without_a_user_links_to_whoever_signs_in(serve_federated, idp_stub, smart_auth, fhir_stub):
    # A client credentials token names only the agent. The user who opens the link and
    # signs in at the FHIR server becomes the user the agent acts for.
    smart_auth.fhir_user, smart_auth.patient = None, "123"  # a patient (MyChart) login
    callback = BrowserCallback()
    with serve_federated() as url:
        async with connect(url, MODERN, token=agent_token(idp_stub, url), elicitation_callback=callback) as session:
            result = await session.call_tool("get_user", {})
        assert await server_module.fhir_connect.user_identity("agent-1", "agent-1") == "Patient/123"

    assert result.text["id"] == "123"
    assert fhir_stub.requests[-1].path.endswith("/Patient/123")


async def test_consent_page_offers_only_the_tools_the_token_allows(serve_federated, idp_stub):
    with serve_federated():
        link = await server_module.fhir_connect.connect_link("agent-1", "agent-1", None, ["fhir-read"], "read")
        async with httpx2.AsyncClient(timeout=30) as browser:
            page = await browser.get(link)
        wider = await server_module.fhir_connect.connect_link(
            "agent-2", "agent-2", None, ["fhir-read", "fhir-write"], "read"
        )
        async with httpx2.AsyncClient(timeout=30) as browser:
            wider_page = await browser.get(wider)

    _, offered = _form(page.text)
    assert offered == ["search", "read", "get_user"], "no write or delete tools without their scopes"
    assert "name='tools' value='read' checked" in page.text, "the requested tool is ticked"
    assert "name='tools' value='search' checked" not in page.text
    _, wider_offered = _form(wider_page.text)
    assert wider_offered == ["search", "read", "get_user", "create", "update"]
    assert page.headers["x-frame-options"] == "DENY"


async def test_a_tool_the_user_did_not_allow_asks_again(serve_federated, idp_stub, fhir_stub):
    callback = BrowserCallback(tools=["read"])
    with serve_federated() as url:
        async with connect(url, MODERN, token=agent_token(idp_stub, url), elicitation_callback=callback) as session:
            read = await session.call_tool("read", {"type": "Patient", "id": "123"})
            callback.tools = None  # this time the user allows every offered tool
            search = await session.call_tool("search", {"type": "Patient", "searchParam": {}})
        record = await server_module.fhir_connect.session("agent-1", "agent-1")

    assert read.text["id"] == "123"
    assert len(callback.links) == 2, "search was not allowed, so the user is asked again"
    assert not isinstance(search.text, dict) or "issue" not in search.text
    assert record["tools"] == ["search", "read", "get_user"]


async def test_declined_tool_is_not_run(serve_federated, idp_stub, fhir_stub):
    with serve_federated() as url:
        async with connect(url, MODERN, token=agent_token(idp_stub, url),
                           elicitation_callback=BrowserCallback(tools=["read"])) as session:
            await session.call_tool("read", {"type": "Patient", "id": "123"})
        async with connect(url, MODERN, token=agent_token(idp_stub, url),
                           elicitation_callback=DecliningCallback("decline")) as session:
            result = await session.call_tool("search", {"type": "Patient", "searchParam": {}})

    assert result.text["issue"][0]["code"] == "login"
    assert len(fhir_stub.requests) == 1, "only the allowed read reached the FHIR server"


async def test_changing_tools_needs_the_same_account(serve_federated, idp_stub, smart_auth, fhir_stub):
    # Once linked, nobody else can take the agent over, or widen it, by signing in as someone else.
    with serve_federated() as url:
        async with connect(url, MODERN, token=agent_token(idp_stub, url),
                           elicitation_callback=BrowserCallback(tools=["read"])) as session:
            await session.call_tool("read", {"type": "Patient", "id": "123"})
        smart_auth.fhir_user = "Practitioner/someone-else"
        link = await server_module.fhir_connect.connect_link("agent-1", "agent-1", None, ["fhir-read"], "search")
        response = await open_in_browser(link)
        record = await server_module.fhir_connect.session("agent-1", "agent-1")

    assert response.status_code == 403
    assert record["tools"] == ["read"] and record["identity"] == "Practitioner/practitioner-1"


async def test_no_tools_chosen_is_refused(serve_federated, idp_stub, smart_auth):
    with serve_federated():
        link = await server_module.fhir_connect.connect_link("agent-1", "agent-1", None, ["fhir-read"], "read")
        response = await open_in_browser(link, tools=[])

    assert response.status_code == 400
    assert smart_auth.authorize_requests == []


async def test_user_sees_and_revokes_their_agents(serve_federated, idp_stub, smart_auth, fhir_stub):
    with serve_federated() as url:
        async with connect(url, MODERN, token=agent_token(idp_stub, url),
                           elicitation_callback=BrowserCallback(tools=["read"])) as session:
            await session.call_tool("read", {"type": "Patient", "id": "123"})
        origin = url.removesuffix("/mcp")
        async with httpx2.AsyncClient(follow_redirects=True, timeout=30) as browser:
            page = await browser.get(f"{origin}{AGENTS_PATH}")
            manage = re.search(r"name='manage' value='([^']+)'", page.text).group(1)
            after = await browser.post(
                f"{origin}{REVOKE_PATH}", data={"manage": manage, "client": "agent-1", "sub": "agent-1"}
            )
        revoked = await server_module.fhir_connect.session("agent-1", "agent-1")

    assert "agent-1" in page.text and "read" in page.text
    assert "No agents are linked" in after.text
    assert revoked is None


async def test_others_cannot_revoke_an_agent(serve_federated, idp_stub, smart_auth):
    with serve_federated() as url:
        async with connect(url, MODERN, token=agent_token(idp_stub, url),
                           elicitation_callback=BrowserCallback(tools=["read"])) as session:
            await session.call_tool("read", {"type": "Patient", "id": "123"})
        smart_auth.fhir_user = "Practitioner/someone-else"
        origin = url.removesuffix("/mcp")
        async with httpx2.AsyncClient(follow_redirects=True, timeout=30) as browser:
            page = await browser.get(f"{origin}{AGENTS_PATH}")
            manage = re.search(r"name='manage' value='([^']+)'", page.text)
            assert manage is None, "someone else's page lists no agents"
        assert await server_module.fhir_connect.session("agent-1", "agent-1") is not None


async def test_legacy_client_without_declared_capabilities_gets_the_link_as_text(serve_federated, idp_stub, fhir_stub):
    # Pre-2026 requests to this stateless endpoint carry no client capabilities. The
    # server must not send an elicitation mode the client has not declared, so it gives
    # the link as text rather than the -32042 URL elicitation error.
    with serve_federated() as url:
        async with connect(url, LEGACY, token=mcp_token(idp_stub, url), elicitation_callback=BrowserCallback()) as session:
            first = await session.call_tool("read", {"type": "Patient", "id": "123"})
            issue = first.text["issue"][0]
            assert issue["code"] == "login"
            link = issue["diagnostics"].split()[-1]
            assert "/connect/fhir?ticket=" in link
            assert (await open_in_browser(link)).status_code == 200
            result = await session.call_tool("read", {"type": "Patient", "id": "123"})

    assert result.text["id"] == "123"
    assert [r.authorization for r in fhir_stub.requests] == ["Bearer ehr-user-token-1"]


async def test_legacy_client_that_declared_url_elicitation_gets_the_error(serve_federated, idp_stub):
    # With declared URL support, an earlier-protocol request gets the -32042 error.
    with serve_federated():
        token = server_module.AccessToken(token="t", client_id="mcp-client", scopes=["fhir-read"], subject="user-1")
        capabilities = SimpleNamespace(elicitation=SimpleNamespace(url=SimpleNamespace()))
        ctx = SimpleNamespace(input_responses=None, client_capabilities=capabilities, protocol_version="2025-11-25")
        with patch.object(server_module, "get_access_token", lambda: token):
            with pytest.raises(UrlElicitationRequiredError) as error:
                await server_module.check_tool_access("read", ctx)

    elicitation = error.value.error.data["elicitations"][0]
    assert elicitation["mode"] == "url" and elicitation["elicitationId"]
    assert "/connect/fhir?ticket=" in elicitation["url"]


class DecliningCallback:
    def __init__(self, action: str) -> None:
        self.action = action
        self.links: List[str] = []

    async def __call__(self, context: Any, params: Any) -> ElicitResult:
        self.links.append(params.url)
        return ElicitResult(action=self.action)


@pytest.mark.parametrize("action", ["decline", "cancel"])
async def test_declined_link_stops_the_request_and_is_withdrawn(serve_federated, idp_stub, smart_auth, fhir_stub, action):
    callback = DecliningCallback(action)
    with serve_federated() as url:
        async with connect(url, MODERN, token=mcp_token(idp_stub, url), elicitation_callback=callback) as session:
            result = await session.call_tool("read", {"type": "Patient", "id": "123"})
        opened = await open_in_browser(callback.links[0])

    issue = result.text["issue"][0]
    assert issue["code"] == "login"
    assert issue["diagnostics"] == server_module.FHIR_CONNECT_DECLINED_MESSAGE
    assert len(callback.links) == 1, "a declined link must not be offered again on the same request"
    assert opened.status_code == 400, "a declined link must stop working"
    assert smart_auth.authorize_requests == [] and fhir_stub.requests == []


async def test_each_agent_is_linked_separately(serve_federated, idp_stub, fhir_stub):
    # A session belongs to one agent (MCP client and subject), so another agent of the
    # same user is linked on its own.
    first, second = BrowserCallback(), BrowserCallback()
    with serve_federated() as url:
        async with connect(url, MODERN, token=mcp_token(idp_stub, url), elicitation_callback=first) as session:
            await session.call_tool("read", {"type": "Patient", "id": "123"})
        other = idp_stub.token(url, "fhir-read", sub="user-1", client_id="other-client")
        async with connect(url, MODERN, token=other, elicitation_callback=second) as session:
            result = await session.call_tool("read", {"type": "Patient", "id": "123"})
        assert await server_module.fhir_connect.user_token("user-1", "mcp-client") is not None
        assert await server_module.fhir_connect.user_token("user-1", "other-client") is not None

    assert result.text["id"] == "123"
    assert len(first.links) == 1 and len(second.links) == 1
    assert first.links[0] != second.links[0]


async def test_fhir_login_handed_to_another_browser_is_refused(serve_federated, idp_stub, smart_auth, fhir_stub):
    # The user submits the consent page, then hands the FHIR login URL to someone else.
    # That browser has no binding cookie, so its FHIR account must not be attached.
    with serve_federated():
        link = await server_module.fhir_connect.connect_link(
            "user-1", "mcp-client", "Practitioner/practitioner-1", ["fhir-read"], "read"
        )
        async with httpx2.AsyncClient(timeout=30) as owner, httpx2.AsyncClient(timeout=30, follow_redirects=True) as other:
            submitted = await consent(owner, link)
            assert submitted.headers["location"].startswith(smart_auth.base_url)
            handed_over = await other.get(submitted.headers["location"])
        assert await server_module.fhir_connect.user_token("user-1", "mcp-client") is None

    assert handed_over.status_code == 403
    assert handed_over.text == "Forbidden"
    assert smart_auth.grants == [], "the FHIR code must not be exchanged"
    assert fhir_stub.requests == []


async def test_connect_link_works_once(serve_federated, idp_stub):
    callback = BrowserCallback()
    with serve_federated() as url:
        async with connect(url, MODERN, token=mcp_token(idp_stub, url), elicitation_callback=callback) as session:
            await session.call_tool("read", {"type": "Patient", "id": "123"})
        replay = await open_in_browser(callback.links[0])

    assert replay.status_code == 400


async def test_expiring_token_is_refreshed_once(serve_federated, idp_stub, smart_auth, fhir_stub):
    callback = BrowserCallback()
    with serve_federated() as url:
        async with connect(url, MODERN, token=mcp_token(idp_stub, url), elicitation_callback=callback) as session:
            await session.call_tool("read", {"type": "Patient", "id": "123"})
            # An hour later the user's FHIR token is about to expire.
            later = SimpleNamespace(time=lambda: time.time() + 3590)
            with patch.object(fhir_connect_module, "time", later):
                await session.call_tool("read", {"type": "Patient", "id": "123"})

    assert smart_auth.grants == ["authorization_code", "refresh_token"]
    assert len(callback.links) == 1, "a refresh must not ask the user to sign in again"
    assert [r.authorization for r in fhir_stub.requests] == [
        "Bearer ehr-user-token-1",
        "Bearer ehr-user-token-2",
    ]


async def test_short_lived_tokens_are_not_refreshed_on_every_call(serve_federated, idp_stub, smart_auth):
    smart_auth.expires_in = 30
    with serve_federated() as url:
        async with connect(url, MODERN, token=mcp_token(idp_stub, url), elicitation_callback=BrowserCallback()) as session:
            await session.call_tool("read", {"type": "Patient", "id": "123"})
            await session.call_tool("read", {"type": "Patient", "id": "123"})

    assert smart_auth.grants == ["authorization_code"]


async def test_failed_refresh_asks_the_user_to_link_again(serve_federated, idp_stub, smart_auth):
    callback = BrowserCallback()
    with serve_federated() as url:
        async with connect(url, MODERN, token=mcp_token(idp_stub, url), elicitation_callback=callback) as session:
            await session.call_tool("read", {"type": "Patient", "id": "123"})
            smart_auth._refresh_tokens.clear()  # the EHR revoked the refresh token
            later = SimpleNamespace(time=lambda: time.time() + 3590)
            with patch.object(fhir_connect_module, "time", later):
                result = await session.call_tool("read", {"type": "Patient", "id": "123"})

    assert result.text["id"] == "123"
    assert len(callback.links) == 2, "the user is asked to sign in again"


async def test_client_without_url_elicitation_gets_the_link_as_text(serve_federated, idp_stub, fhir_stub):
    with serve_federated() as url:
        async with connect(url, MODERN, token=mcp_token(idp_stub, url)) as session:
            result = await session.call_tool("read", {"type": "Patient", "id": "123"})

    issue = result.text["issue"][0]
    assert issue["code"] == "login"
    assert "/connect/fhir?ticket=" in issue["diagnostics"]
    assert fhir_stub.requests == []


async def test_agents_do_not_share_sessions(serve_federated, idp_stub, fhir_stub):
    with serve_federated() as url:
        async with connect(url, MODERN, token=mcp_token(idp_stub, url), elicitation_callback=BrowserCallback()) as session:
            await session.call_tool("read", {"type": "Patient", "id": "123"})
        assert await server_module.fhir_connect.user_token("user-1", "mcp-client") is not None
        assert await server_module.fhir_connect.user_token("user-2", "mcp-client") is None


async def test_retry_before_signing_in_gets_the_same_link(serve_federated, idp_stub):
    with serve_federated() as url:
        async with connect(url, MODERN, token=mcp_token(idp_stub, url)) as session:
            first = await session.call_tool("read", {"type": "Patient", "id": "123"})
            second = await session.call_tool("read", {"type": "Patient", "id": "123"})

    assert first.text["issue"][0]["diagnostics"] == second.text["issue"][0]["diagnostics"]


EPIC_BASE = "https://fhir.example.org/api/FHIR/R4"


@pytest.fixture
def serve_fhir_user_check(serve_federated):
    """Patient-scoped sign-in; each test sets who signs in at the FHIR server."""
    return lambda: serve_federated(scopes="openid fhirUser patient/Patient.read")


async def test_fhir_user_check_links_when_the_fhir_sign_in_matches(
    serve_fhir_user_check, idp_stub, smart_auth, fhir_stub
):
    # The MCP token's fhirUser is a full URL; the FHIR server's ID token names the same patient.
    smart_auth.fhir_user = f"{EPIC_BASE}/Patient/123"
    callback = BrowserCallback()
    with serve_fhir_user_check() as url:
        token = idp_stub.token(url, "fhir-read", fhirUser=f"{EPIC_BASE}/Patient/123")
        async with connect(url, MODERN, token=token, elicitation_callback=callback) as session:
            result = await session.call_tool("read", {"type": "Patient", "id": "123"})

    assert result.text["id"] == "123"
    assert callback.responses[0].status_code == 200
    assert smart_auth.authorize_requests[0]["scope"] == "openid fhirUser patient/Patient.read"
    assert [r.authorization for r in fhir_stub.requests] == ["Bearer ehr-user-token-1"]


async def test_fhir_user_check_falls_back_to_the_patient_context(serve_fhir_user_check, idp_stub, smart_auth, fhir_stub):
    smart_auth.fhir_user, smart_auth.patient = None, "123"  # no ID token fhirUser, only the patient context
    with serve_fhir_user_check() as url:
        token = idp_stub.token(url, "fhir-read", fhirUser="Patient/123")
        async with connect(url, MODERN, token=token, elicitation_callback=BrowserCallback()) as session:
            result = await session.call_tool("read", {"type": "Patient", "id": "123"})

    assert result.text["id"] == "123"


async def test_fhir_user_check_refuses_a_different_person(serve_fhir_user_check, idp_stub, smart_auth, fhir_stub):
    # A user's own MCP token names them, so someone signing in as another patient is refused.
    smart_auth.fhir_user = f"{EPIC_BASE}/Patient/someone-else"
    callback = BrowserCallback()
    with serve_fhir_user_check() as url:
        token = idp_stub.token(url, "fhir-read", fhirUser=f"{EPIC_BASE}/Patient/123")
        async with connect(url, MODERN, token=token, elicitation_callback=callback) as session:
            with pytest.raises(Exception):
                await session.call_tool("read", {"type": "Patient", "id": "123"})
        assert await server_module.fhir_connect.user_token("user-1", "mcp-client") is None

    assert callback.responses[0].status_code == 403
    assert callback.responses[0].text == "Forbidden"
    assert fhir_stub.requests == []


class TestConfiguration:
    def _configs(self, **values: Any) -> ServerConfigs:
        base = {
            "_env_file": None,
            "mcp_auth_mode": "resource_server",
            "mcp_idp_issuer": "https://idp.example.com",
            "server_client_id": "ehr-user-app",
            "server_scopes": "openid fhirUser patient/Patient.read",
        }
        return ServerConfigs(**{**base, **values})

    def test_requires_mcp_login(self):
        with pytest.raises(ConfigurationError, match="FHIR_MCP_AUTH_MODE=resource_server"):
            self._configs(mcp_auth_mode="disabled").validate_for_startup()

    def test_needs_openid_in_the_fhir_scopes(self):
        with pytest.raises(ConfigurationError, match="openid fhirUser"):
            self._configs(server_scopes="patient/Patient.read").validate_for_startup()

    def test_without_a_fhir_client_id_there_is_no_sign_in(self):
        configs = self._configs(server_client_id="")
        configs.validate_for_startup()
        assert configs.fhir_sign_in is False

    def test_complete_configuration_passes(self):
        self._configs().validate_for_startup()
