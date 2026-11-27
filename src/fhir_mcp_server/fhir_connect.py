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

"""Linking an agent to the user it acts for (FHIR_SERVER_CLIENT_ID is set).

An agent calls the MCP server with its own access token from the identity
provider (for example a client credentials token). The token names the agent,
not a user. The first time the agent needs the FHIR server, the tool answers
with a URL-mode elicitation pointing at `/connect/fhir`. In the browser:

1. `/connect/fhir` checks the one-time ticket and shows the consent page: the
   user chooses which tools this agent may use. Only tools the agent's token is
   allowed (its scopes) can be chosen.
2. Submitting the page redirects to the FHIR server's login (authorization code
   with PKCE and `aud`), setting a short-lived HttpOnly cookie that ties the FHIR
   sign-in to that browser.
3. `/connect/fhir/callback` requires that cookie, so a FHIR login URL handed to
   someone else is refused (plain 403). It exchanges the code and reads who
   signed in (ID token fhirUser, or the SMART patient context). It then keeps the
   user's FHIR tokens and the chosen tools in the agent's session. They never
   reach the agent.

Each agent acts for one user. Once an agent is linked, changing its tools needs
the same user to sign in at the FHIR server again, so neither the agent nor
anyone else can widen its access or attach another account. When the MCP token
does carry a fhirUser (a user's own token), the FHIR sign-in must match it.

`/connect/agents` lets a user sign in at the FHIR server, see the agents linked
to their account with the tools each may use, and revoke them.
"""

import html
import logging
import secrets
import time

from base64 import urlsafe_b64encode
from hashlib import sha256
from typing import Any, Dict, Iterable, List
from urllib.parse import urlencode

import anyio
import httpx2
import jwt

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from fhir_mcp_server.agent_sessions import AgentSessions
from fhir_mcp_server.auth.scopes import tool_scopes
from fhir_mcp_server.backend_auth import client_secret_basic, fhir_tls
from fhir_mcp_server.oauth.types import ServerConfigs

logger: logging.Logger = logging.getLogger(__name__)

CONNECT_PATH = "/connect/fhir"
FHIR_CALLBACK_PATH = "/connect/fhir/callback"
AGENTS_PATH = "/connect/agents"
REVOKE_PATH = "/connect/agents/revoke"

TICKET_SECONDS = 600  # how long a connect link stays valid
SIGN_IN_SECONDS = 600  # how long a browser has to finish the FHIR sign-in
MANAGE_SECONDS = 600  # how long the agents page stays usable after signing in
REFRESH_MARGIN_SECONDS = 60
DEFAULT_EXPIRES_IN_SECONDS = 300
REFRESH_TOKEN_KEEP_SECONDS = 24 * 3600  # how long a session is kept when a refresh token exists

# The tools a user chooses for an agent, with what each lets the agent do.
# get_capabilities only reads the FHIR server's public metadata, so it is always allowed.
CONSENT_TOOLS: Dict[str, str] = {
    "search": "Search your health records",
    "read": "Read a record by its id",
    "get_user": "Read your own profile",
    "create": "Create new records",
    "update": "Change existing records",
    "delete": "Delete records",
}
ALWAYS_ALLOWED_TOOLS = ("get_capabilities",)


class FhirConnectError(RuntimeError):
    """A sign-in step with the FHIR server failed."""


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = urlsafe_b64encode(sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


_STYLE = (
    "font-family:sans-serif;max-width:40em;margin:4em auto;padding:0 1em;line-height:1.5"
)


def _html(status: int, title: str, inner: str) -> HTMLResponse:
    """A page with an escaped title and already-escaped inner HTML."""
    body = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>FHIR MCP Server</title></head>"
        f"<body style='{_STYLE}'><h1 style='font-size:1.4em'>{html.escape(title)}</h1>{inner}</body></html>"
    )
    response = HTMLResponse(body, status_code=status)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Frame-Options"] = "DENY"  # the consent page must not be framed (clickjacking)
    return response


def _page(status: int, title: str, message: str) -> HTMLResponse:
    return _html(status, title, f"<p>{html.escape(message)}</p>")


def _resource_ref(value: Any) -> str | None:
    """`Type/id` from a fhirUser value, which may be relative or a full URL
    (`https://…/FHIR/R4/Patient/123` → `Patient/123`)."""
    if not isinstance(value, str):
        return None
    parts = [p for p in value.strip().split("?")[0].split("/") if p]
    if len(parts) < 2 or not parts[-2][:1].isupper():
        return None
    return f"{parts[-2]}/{parts[-1]}"


def _forbidden() -> Response:
    """A plain 403 that says nothing about whose link it was or why it failed."""
    return Response("Forbidden", status_code=403, media_type="text/plain")


def _session_key(client: str, sub: str) -> str:
    return f"session:{client}:{sub}"


class FhirConnect:
    def __init__(self, configs: ServerConfigs) -> None:
        self._configs = configs
        self._fhir_endpoints: Dict[str, str] | None = None
        self._refresh_lock = anyio.Lock()
        self._sessions = AgentSessions()

    # Used by the tools. A session belongs to one agent: the MCP client (`client_id`) and
    # subject (`sub`) of its validated access token. A client credentials token names
    # only the agent, so each agent has one session and acts for one user.

    def consentable_tools(self, scopes: Iterable[str]) -> List[str]:
        """The tools a user may allow for an agent whose token has these scopes."""
        granted = set(scopes)
        required = tool_scopes(self._configs)
        return [tool for tool in CONSENT_TOOLS if required.get(tool) in granted]

    async def connect_link(
        self,
        sub: str,
        client_id: str = "",
        fhir_user: str | None = None,
        scopes: Iterable[str] = (),
        tool: str | None = None,
    ) -> str:
        """A single-use link to the consent page for this agent. A client that retries
        before the user has finished gets the same link. `fhir_user` is the MCP token's
        fhirUser claim, if any, which the FHIR sign-in must then match; `tool` is the
        tool the agent asked for, ticked on the page."""
        owner = {
            "sub": sub,
            "client": client_id,
            "fhir_user": fhir_user,
            "scopes": sorted(set(scopes)),
            "tool": tool,
        }
        ticket = await self._sessions.get(f"link:{client_id}:{sub}")
        if not ticket or await self._sessions.get(f"ticket:{ticket}") != owner:
            ticket = secrets.token_urlsafe(32)
            await self._sessions.put(f"ticket:{ticket}", owner, TICKET_SECONDS)
            await self._sessions.put(f"link:{client_id}:{sub}", ticket, TICKET_SECONDS)
        return f"{self._url(CONNECT_PATH)}?{urlencode({'ticket': ticket})}"

    async def cancel_link(self, sub: str, client_id: str = "") -> None:
        """Withdraw the pending link after the user declined or cancelled it."""
        ticket = await self._sessions.get(f"link:{client_id}:{sub}")
        if ticket:
            await self._sessions.delete(f"ticket:{ticket}")
            await self._sessions.delete(f"link:{client_id}:{sub}")

    async def session(self, sub: str, client_id: str = "") -> Dict[str, Any] | None:
        """The agent's session, without refreshing its FHIR token."""
        return await self._sessions.get(_session_key(client_id, sub))

    async def allows(self, sub: str, client_id: str, tool: str) -> bool:
        """Whether the user who linked this agent allowed it to use the tool."""
        if tool in ALWAYS_ALLOWED_TOOLS:
            return True
        record = await self.session(sub, client_id)
        return record is not None and tool in record.get("tools", [])

    async def user_token(self, sub: str, client_id: str = "") -> str | None:
        """The FHIR access token of the user this agent acts for, refreshed when it is
        about to expire. None if the agent is not linked (or no longer can be)."""
        key = _session_key(client_id, sub)
        record = await self._sessions.get(key)
        if record and record["refresh_at"] > time.time():
            return record["access_token"]
        if not record or not record.get("refresh_token"):
            if record and record["expires_at"] > time.time():
                return record["access_token"]  # no refresh token: use it until it expires
            await self._end_session(client_id, sub, record)
            return None
        async with self._refresh_lock:
            record = await self._sessions.get(key)  # another call may have refreshed it
            if record and record["refresh_at"] > time.time():
                return record["access_token"]
            if not record:
                return None
            try:
                tokens = await self._fhir_token_request(
                    {"grant_type": "refresh_token", "refresh_token": record["refresh_token"]}
                )
            except FhirConnectError as ex:
                logger.info("Could not refresh the FHIR token for an agent; asking the user to link it again: %s", ex)
                await self._end_session(client_id, sub, record)
                return None
            return await self._save_session(key, tokens, record)

    async def expire_token(self, sub: str, client_id: str = "") -> bool:
        """Stop using the agent's FHIR access token, after the FHIR server rejected it:
        the next call refreshes it, or, without a refresh token, the user links the agent
        again. True if there was a session."""
        key = _session_key(client_id, sub)
        record = await self._sessions.get(key)
        if not record:
            return False
        if not record.get("refresh_token"):
            await self._end_session(client_id, sub, record)
            return True
        await self._sessions.put(key, {**record, "refresh_at": 0}, REFRESH_TOKEN_KEEP_SECONDS)
        return True

    async def user_identity(self, sub: str, client_id: str = "") -> str | None:
        """Who the agent acts for at the FHIR server, as `Type/id` (for example
        `Patient/123` from a MyChart login), if the FHIR sign-in said."""
        record = await self.session(sub, client_id)
        return record.get("identity") if record else None

    # Browser routes

    def register_routes(self, mcp: Any) -> None:
        mcp.custom_route(CONNECT_PATH, methods=["GET"])(self.consent_page)
        mcp.custom_route(CONNECT_PATH, methods=["POST"])(self.consent_submit)
        mcp.custom_route(FHIR_CALLBACK_PATH, methods=["GET"])(self.fhir_callback)
        mcp.custom_route(AGENTS_PATH, methods=["GET"])(self.agents_sign_in)
        mcp.custom_route(REVOKE_PATH, methods=["POST"])(self.revoke)

    async def consent_page(self, request: Request) -> Response:
        """Step 1: the user chooses which tools the agent may use."""
        ticket = request.query_params.get("ticket", "")
        owner = await self._sessions.get(f"ticket:{ticket}")
        if not owner:
            return _page(400, "This link has expired", "Ask the agent to run the tool again to get a new link.")
        available = self.consentable_tools(owner.get("scopes", []))
        if not available:
            return _page(
                403,
                "This agent cannot use any tools",
                "Your administrator has not allowed this agent any FHIR access.",
            )
        current = await self.session(owner["sub"], owner["client"])
        checked = set(current.get("tools", []) if current else []) | {owner.get("tool")}
        rows = "".join(
            "<label style='display:block;margin:.4em 0'>"
            f"<input type='checkbox' name='tools' value='{html.escape(tool)}'"
            f"{' checked' if tool in checked else ''}> <b>{html.escape(tool)}</b>: "
            f"{html.escape(CONSENT_TOOLS[tool])}</label>"
            for tool in available
        )
        relink = (
            "<p>This agent is already linked. To change its tools, sign in with the same account.</p>"
            if current
            else ""
        )
        inner = (
            f"<p>The agent <code>{html.escape(owner['client'])}</code> wants to use this FHIR server "
            "on your behalf. Choose what it may do, then sign in to the FHIR server.</p>"
            f"{relink}"
            f"<form method='post' action='{html.escape(CONNECT_PATH)}'>"
            f"<input type='hidden' name='ticket' value='{html.escape(ticket)}'>"
            f"<fieldset style='border:1px solid #ccc;padding:.5em 1em'><legend>Allowed tools</legend>{rows}</fieldset>"
            "<p style='color:#555'>It can always read the server's capabilities (no health data).</p>"
            "<button type='submit'>Allow and sign in</button></form>"
            "<p style='color:#555'>Not expecting this? Close the page; nothing is shared.</p>"
        )
        return _html(200, "Allow this agent?", inner)

    async def consent_submit(self, request: Request) -> Response:
        """Step 2: keep the chosen tools with the sign-in and redirect to the FHIR server."""
        form = await request.form()
        ticket = str(form.get("ticket", ""))
        owner = await self._sessions.get(f"ticket:{ticket}")
        if not owner:
            return _page(400, "This link has expired", "Ask the agent to run the tool again to get a new link.")
        available = self.consentable_tools(owner.get("scopes", []))
        chosen = [tool for tool in available if tool in {str(t) for t in form.getlist("tools")}]
        if not chosen:
            return _page(400, "No tools chosen", "Go back and choose at least one tool, or close the page.")
        if not await self._sessions.pop(f"ticket:{ticket}"):
            return _page(400, "This link has expired", "Ask the agent to run the tool again to get a new link.")
        await self._sessions.delete(f"link:{owner['client']}:{owner['sub']}")
        # The FHIR sign-in must be the account the agent is already linked to, or the one
        # named by the MCP token's fhirUser; a new agent's first sign-in names its user.
        current = await self.session(owner["sub"], owner["client"])
        expect = (current or {}).get("identity") or _resource_ref(owner.get("fhir_user"))
        return await self._redirect_to_fhir(
            {
                "purpose": "link",
                "sub": owner["sub"],
                "client": owner["client"],
                "expect": expect,
                "tools": chosen,
            }
        )

    async def agents_sign_in(self, request: Request) -> Response:
        """The agents page starts with a FHIR sign-in, which says whose agents to show."""
        return await self._redirect_to_fhir({"purpose": "manage"})

    async def fhir_callback(self, request: Request) -> Response:
        """Step 3: the FHIR server's answer."""
        params = request.query_params
        pending = await self._sessions.pop(f"fhir:{params.get('state', '')}")
        if not pending:
            return _page(400, "Sign-in expired", "Start again from the link.")
        if not secrets.compare_digest(request.cookies.get(self._cookie_name(), ""), pending["browser"]):
            logger.warning("FHIR sign-in finished in a different browser than the one that opened the link.")
            return _forbidden()
        if params.get("error") or not params.get("code"):
            reason = params.get("error_description") or params.get("error", "")
            return _page(400, "FHIR sign-in was not completed", reason)
        try:
            tokens = await self._fhir_token_request(
                {
                    "grant_type": "authorization_code",
                    "code": params["code"],
                    "redirect_uri": self._url(FHIR_CALLBACK_PATH),
                    "code_verifier": pending["verifier"],
                }
            )
        except FhirConnectError as ex:
            logger.warning("FHIR server refused the authorization code: %s", ex)
            return _page(400, "FHIR sign-in failed", "The FHIR server did not issue a token.")
        identity = self._fhir_identity(tokens)
        if not identity:
            logger.warning("FHIR sign-in refused: the FHIR server did not say who signed in.")
            return _page(400, "Your account cannot be used", "The FHIR server did not say who signed in.")
        if pending["purpose"] == "manage":
            response = await self._agents_page(identity, None)
        else:
            if pending.get("expect") and identity != pending["expect"]:
                # The tokens are dropped: someone other than the agent's user signed in.
                logger.warning("FHIR sign-in refused: not the account this agent is linked to or named by its token.")
                return _forbidden()
            key = _session_key(pending["client"], pending["sub"])
            await self._save_session(key, tokens, None, identity=identity, tools=pending["tools"])
            await self._index(identity, pending["client"], pending["sub"])
            logger.info(
                "A user linked an agent (tools: %s; FHIR scope: %s).",
                ", ".join(pending["tools"]),
                tokens.get("scope", ""),
            )
            tools = ", ".join(pending["tools"])
            response = _html(
                200,
                "Agent linked",
                f"<p>The agent <code>{html.escape(pending['client'])}</code> may now use: "
                f"{html.escape(tools)}. You can return to it.</p>"
                f"<p>To see or revoke your agents later, open <a href='{html.escape(AGENTS_PATH)}'>"
                f"{html.escape(self._url(AGENTS_PATH))}</a>.</p>",
            )
        response.delete_cookie(self._cookie_name(), path="/")
        return response

    async def revoke(self, request: Request) -> Response:
        """End an agent's session from the agents page. Only the account the agent is
        linked to can end it: the page's token names who signed in."""
        form = await request.form()
        manage = str(form.get("manage", ""))
        identity = await self._sessions.get(f"manage:{manage}")
        if not identity:
            return _page(400, "This page has expired", "Open the agents page again.")
        client, sub = str(form.get("client", "")), str(form.get("sub", ""))
        key = _session_key(client, sub)
        record = await self._sessions.get(key)
        if record and record.get("identity") == identity:
            await self._end_session(client, sub, record)
            logger.info("A user revoked an agent.")
        return await self._agents_page(identity, manage)

    # Helpers

    async def _agents_page(self, identity: str, manage: str | None) -> HTMLResponse:
        """The agents linked to this FHIR account, each with a revoke button."""
        if not manage:
            manage = secrets.token_urlsafe(32)
            await self._sessions.put(f"manage:{manage}", identity, MANAGE_SECONDS)
        rows = []
        for entry in await self._linked(identity):
            record = await self._sessions.get(_session_key(entry["client"], entry["sub"]))
            if not record or record.get("identity") != identity:
                continue
            rows.append(
                "<li style='margin:.6em 0'>"
                f"<code>{html.escape(entry['client'])}</code>: {html.escape(', '.join(record.get('tools', [])))}"
                f"<form method='post' action='{html.escape(REVOKE_PATH)}' style='display:inline;margin-left:1em'>"
                f"<input type='hidden' name='manage' value='{html.escape(manage)}'>"
                f"<input type='hidden' name='client' value='{html.escape(entry['client'])}'>"
                f"<input type='hidden' name='sub' value='{html.escape(entry['sub'])}'>"
                "<button type='submit'>Revoke</button></form></li>"
            )
        listing = f"<ul>{''.join(rows)}</ul>" if rows else "<p>No agents are linked to your account.</p>"
        return _html(200, "Your linked agents", listing)

    async def _linked(self, identity: str) -> List[Dict[str, str]]:
        return list(await self._sessions.get(f"owner:{identity}") or [])

    async def _index(self, identity: str, client: str, sub: str) -> None:
        """Remember that this account linked the agent, for the agents page."""
        entries = [e for e in await self._linked(identity) if (e["client"], e["sub"]) != (client, sub)]
        entries.append({"client": client, "sub": sub})
        await self._sessions.put(f"owner:{identity}", entries, REFRESH_TOKEN_KEEP_SECONDS)

    async def _end_session(self, client: str, sub: str, record: Dict[str, Any] | None) -> None:
        """Forget the agent's session, and drop it from its user's agents page."""
        await self._sessions.delete(_session_key(client, sub))
        identity = (record or {}).get("identity")
        if identity:
            entries = [e for e in await self._linked(identity) if (e["client"], e["sub"]) != (client, sub)]
            await self._sessions.put(f"owner:{identity}", entries, REFRESH_TOKEN_KEEP_SECONDS)

    async def _redirect_to_fhir(self, pending: Dict[str, Any]) -> Response:
        """Send the browser to the FHIR server's login, keeping `pending` for the callback."""
        try:
            endpoints = await self._fhir()
        except FhirConnectError as ex:
            logger.error("Cannot find the FHIR server's sign-in endpoints: %s", ex)
            return _page(502, "FHIR sign-in is unavailable", "The FHIR server's sign-in could not be found.")
        state = secrets.token_urlsafe(32)
        verifier, challenge = _pkce_pair()
        # Ties the FHIR sign-in to this browser. Without it, the FHIR login URL below could
        # be handed to someone else, whose FHIR account would then be attached to the agent.
        browser = secrets.token_urlsafe(32)
        await self._sessions.put(
            f"fhir:{state}", {**pending, "verifier": verifier, "browser": browser}, SIGN_IN_SECONDS
        )
        query = {
            "response_type": "code",
            "client_id": self._configs.server_client_id,
            "redirect_uri": self._url(FHIR_CALLBACK_PATH),
            "scope": " ".join(self._configs.scopes),
            "state": state,
            "aud": self._configs.server_base_url,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        response = RedirectResponse(f"{endpoints['authorize']}?{urlencode(query)}", status_code=303)
        response.set_cookie(
            self._cookie_name(),
            browser,
            max_age=SIGN_IN_SECONDS,
            path="/",
            secure=self._cookie_name().startswith("__Host-"),
            httponly=True,
            samesite="lax",  # sent on the FHIR server's top-level redirect back
        )
        return response

    def _fhir_identity(self, tokens: Dict[str, Any]) -> str | None:
        """Who signed in at the FHIR server, as `Type/id`: the fhirUser claim of its ID
        token, or else the SMART `patient` context. The ID token comes straight from the
        token endpoint over TLS, so its signature need not be checked again (OpenID Connect
        Core 3.1.3.7); its audience must still be this client."""
        id_token = tokens.get("id_token")
        if id_token:
            try:
                claims = jwt.decode(id_token, options={"verify_signature": False})
            except jwt.PyJWTError:
                return None
            audience = claims.get("aud")
            audiences = audience if isinstance(audience, list) else [audience]
            if self._configs.server_client_id not in audiences:
                return None
            if ref := _resource_ref(claims.get("fhirUser")):
                return ref
        patient = tokens.get("patient")
        return f"Patient/{patient}" if patient else None

    def _cookie_name(self) -> str:
        """`__Host-` (Secure, whole host) over HTTPS; a plain name on http://localhost."""
        secure = self._configs.effective_server_url.lower().startswith("https://")
        return f"{'__Host-' if secure else ''}fhir_connect"

    def _url(self, path: str) -> str:
        return f"{self._configs.effective_server_url.rstrip('/')}{path}"

    async def _save_session(
        self,
        key: str,
        tokens: Dict[str, Any],
        previous: Dict[str, Any] | None,
        identity: str | None = None,
        tools: List[str] | None = None,
    ) -> str:
        """Keep the user's FHIR tokens in the agent's session. A refresh response may omit
        the refresh token and patient; the identity and tools carry over."""
        previous = previous or {}
        access_token = str(tokens["access_token"])
        refresh_token = tokens.get("refresh_token") or previous.get("refresh_token")
        expires_in = float(tokens.get("expires_in") or DEFAULT_EXPIRES_IN_SECONDS)
        keep = REFRESH_TOKEN_KEEP_SECONDS if refresh_token else expires_in
        now = time.time()
        # Refresh a little before expiry, but never sooner than halfway through a short-lived token.
        margin = min(REFRESH_MARGIN_SECONDS, expires_in / 2)
        await self._sessions.put(
            key,
            {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_at": now + expires_in,
                "refresh_at": now + expires_in - margin,
                "patient": tokens.get("patient") or previous.get("patient"),
                "identity": identity or previous.get("identity"),
                "tools": tools if tools is not None else previous.get("tools", []),
                "scope": tokens.get("scope") or previous.get("scope"),
            },
            keep,
        )
        return access_token

    async def _fhir(self) -> Dict[str, str]:
        if self._fhir_endpoints is None:
            authorize, token = self._configs.server_authorize_url, self._configs.server_token_url
            if not (authorize and token):
                discovery = f"{self._configs.server_base_url.rstrip('/')}/.well-known/smart-configuration"
                try:
                    async with httpx2.AsyncClient(verify=fhir_tls(self._configs), timeout=10) as client:
                        response = await client.get(discovery, headers={"Accept": "application/json"})
                except httpx2.HTTPError as ex:
                    raise FhirConnectError(str(ex)) from ex
                if response.status_code != 200:
                    raise FhirConnectError(f"{discovery} answered HTTP {response.status_code}")
                smart = response.json()
                authorize = authorize or smart.get("authorization_endpoint")
                token = token or smart.get("token_endpoint")
            if not (authorize and token):
                raise FhirConnectError("set FHIR_SERVER_AUTHORIZE_URL and FHIR_SERVER_TOKEN_URL")
            self._fhir_endpoints = {"authorize": str(authorize), "token": str(token)}
        return self._fhir_endpoints

    async def _fhir_token_request(self, form: Dict[str, str]) -> Dict[str, Any]:
        endpoint = (await self._fhir())["token"]
        headers: Dict[str, str] = {"Accept": "application/json"}
        if self._configs.server_client_secret:
            method = "client_secret_basic"
            headers.update(client_secret_basic(self._configs.server_client_id, self._configs.server_client_secret))
        else:
            method = "public client (PKCE only)"
            form = {**form, "client_id": self._configs.server_client_id}
        logger.info("FHIR token request (%s) authenticated with %s.", form.get("grant_type"), method)
        try:
            async with httpx2.AsyncClient(
                verify=fhir_tls(self._configs), timeout=self._configs.mcp_request_timeout
            ) as client:
                response = await client.post(endpoint, data=form, headers=headers)
        except httpx2.HTTPError as ex:
            raise FhirConnectError(str(ex)) from ex
        if response.status_code != 200:
            try:
                body = response.json()
                error = body.get("error") or "unknown_error"
                if body.get("error_description"):
                    error = f"{error}: {body['error_description']}"
            except ValueError:
                error = "non-JSON error response"
            raise FhirConnectError(f"token endpoint answered HTTP {response.status_code} ({error})")
        tokens: Dict[str, Any] = response.json()
        if not tokens.get("access_token"):
            raise FhirConnectError("token endpoint returned no access_token")
        return tokens
