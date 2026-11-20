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

"""Per-user sign-in to the FHIR server (FHIR_SERVER_AUTH_MODE=user_smart_login).

Each MCP user connects their own FHIR server account (for example an Epic
clinician or MyChart login) through SMART App Launch, and the FHIR server sees
that user rather than the MCP server. The MCP identity provider is expected to
federate to the FHIR server, so the MCP token carries the user's `fhirUser`.

When a tool needs the FHIR server and the user has not connected yet, the tool
answers with a URL-mode elicitation pointing at `/connect/fhir`. In the browser:

1. `/connect/fhir` checks the one-time ticket, then redirects to the FHIR
   server's login (authorization code with PKCE and `aud`), setting a
   short-lived HttpOnly cookie that ties the FHIR sign-in to that browser.
2. `/connect/fhir/callback` requires that cookie, so a FHIR login URL handed to
   someone else is refused (plain 403). It exchanges the code and requires the
   FHIR sign-in (ID token fhirUser, or the SMART patient context) to name the
   same person as the MCP token's fhirUser, so a link forwarded to someone else
   cannot attach their FHIR account to the wrong MCP user. It then keeps the
   user's FHIR tokens, keyed by their MCP `client_id` and `sub`. They never reach
   the MCP client.
"""

import html
import logging
import secrets
import time

from functools import cached_property

from base64 import urlsafe_b64encode
from hashlib import sha256
from typing import Any, Dict
from urllib.parse import urlencode

import anyio
import httpx2
import jwt

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from fhir_mcp_server.backend_auth import client_secret_basic, fhir_tls
from fhir_mcp_server.oauth.types import ServerConfigs
from fhir_mcp_server.token_store import TokenStore, create_token_store

logger: logging.Logger = logging.getLogger(__name__)

CONNECT_PATH = "/connect/fhir"
FHIR_CALLBACK_PATH = "/connect/fhir/callback"

TICKET_SECONDS = 600  # how long a connect link stays valid
SIGN_IN_SECONDS = 600  # how long a browser has to finish the FHIR sign-in
REFRESH_MARGIN_SECONDS = 60
DEFAULT_EXPIRES_IN_SECONDS = 300
REFRESH_TOKEN_KEEP_SECONDS = 24 * 3600  # how long a connection is kept when a refresh token exists


class FhirConnectError(RuntimeError):
    """A sign-in step with the FHIR server failed."""


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = urlsafe_b64encode(sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _page(status: int, title: str, message: str) -> HTMLResponse:
    body = (
        "<!doctype html><html><head><meta charset='utf-8'><title>FHIR MCP Server</title></head>"
        "<body style='font-family:sans-serif;max-width:40em;margin:4em auto;line-height:1.5'>"
        f"<h1 style='font-size:1.4em'>{html.escape(title)}</h1><p>{html.escape(message)}</p></body></html>"
    )
    return HTMLResponse(body, status_code=status)


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


class FhirConnect:
    def __init__(self, configs: ServerConfigs) -> None:
        self._configs = configs
        self._fhir_endpoints: Dict[str, str] | None = None
        self._refresh_lock = anyio.Lock()

    @cached_property
    def _store(self) -> TokenStore:
        """Tickets, pending sign-ins and users' FHIR tokens (FHIR_MCP_TOKEN_STORE),
        created on first use."""
        return create_token_store(self._configs)

    # Used by the tools. Each connection belongs to one MCP user (`sub`) on one MCP
    # client (`client_id`), both taken from the validated MCP access token, so the same
    # user connects separately from each client they use.

    async def connect_link(self, sub: str, client_id: str = "", fhir_user: str | None = None) -> str:
        """A single-use link that starts the browser sign-in for this MCP user and client.
        A client that retries before the user has finished signing in gets the same link.
        `fhir_user` is the MCP token's fhirUser claim, compared with the FHIR server's
        identity when the sign-in finishes."""
        owner = {"sub": sub, "client": client_id, "fhir_user": fhir_user}
        ticket = await self._store.get(f"link:{client_id}:{sub}")
        if not ticket or await self._store.get(f"ticket:{ticket}") != owner:
            ticket = secrets.token_urlsafe(32)
            await self._store.put(f"ticket:{ticket}", owner, TICKET_SECONDS)
            await self._store.put(f"link:{client_id}:{sub}", ticket, TICKET_SECONDS)
        return f"{self._url(CONNECT_PATH)}?{urlencode({'ticket': ticket})}"

    async def cancel_link(self, sub: str, client_id: str = "") -> None:
        """Withdraw the pending link after the user declined or cancelled it."""
        ticket = await self._store.get(f"link:{client_id}:{sub}")
        if ticket:
            await self._store.delete(f"ticket:{ticket}")
            await self._store.delete(f"link:{client_id}:{sub}")

    async def user_token(self, sub: str, client_id: str = "") -> str | None:
        """The user's FHIR access token, refreshed when it is about to expire."""
        key = f"user:{client_id}:{sub}"
        record = await self._store.get(key)
        if record and record["refresh_at"] > time.time():
            return record["access_token"]
        if not record or not record.get("refresh_token"):
            if record and record["expires_at"] > time.time():
                return record["access_token"]  # no refresh token: use it until it expires
            await self._store.delete(key)
            return None
        async with self._refresh_lock:
            record = await self._store.get(key)  # another call may have refreshed it
            if record and record["refresh_at"] > time.time():
                return record["access_token"]
            if not record:
                return None
            try:
                tokens = await self._fhir_token_request(
                    {"grant_type": "refresh_token", "refresh_token": record["refresh_token"]}
                )
            except FhirConnectError as ex:
                logger.info("Could not refresh the FHIR token for a user; asking them to connect again: %s", ex)
                await self._store.delete(key)
                return None
            return await self._save_user_tokens(key, tokens, record)

    async def user_patient(self, sub: str, client_id: str = "") -> str | None:
        """The patient the user's FHIR sign-in is limited to (SMART `patient` launch
        context), if the FHIR server returned one; for example a MyChart login."""
        record = await self._store.get(f"user:{client_id}:{sub}")
        return record.get("patient") if record else None

    # Browser routes

    def register_routes(self, mcp: Any) -> None:
        mcp.custom_route(CONNECT_PATH, methods=["GET"])(self.start)
        mcp.custom_route(FHIR_CALLBACK_PATH, methods=["GET"])(self.fhir_callback)

    async def start(self, request: Request) -> Response:
        owner = await self._store.pop(f"ticket:{request.query_params.get('ticket', '')}")
        if not owner:
            return _page(400, "This link has expired", "Run the tool again to get a new link.")
        # The FHIR server's sign-in must name the same person as the MCP token's
        # fhirUser, checked when the FHIR server answers.
        await self._store.delete(f"link:{owner['client']}:{owner['sub']}")
        if not _resource_ref(owner.get("fhir_user")):
            logger.warning("FHIR connect refused: the MCP token has no fhirUser to compare with.")
            return _page(
                400,
                "Your account cannot be connected",
                "Your sign-in does not say which FHIR record is yours. Ask your administrator.",
            )
        return await self._redirect_to_fhir(owner["sub"], owner["client"], expect=owner["fhir_user"])

    async def _redirect_to_fhir(self, sub: str, client: str, expect: str) -> Response:
        """Send the browser to the FHIR server's login. `expect` is the fhirUser the FHIR
        sign-in must match."""
        try:
            endpoints = await self._fhir()
        except FhirConnectError as ex:
            logger.error("Cannot find the FHIR server's sign-in endpoints: %s", ex)
            return _page(502, "FHIR sign-in is unavailable", "The FHIR server's sign-in could not be found.")
        state = secrets.token_urlsafe(32)
        verifier, challenge = _pkce_pair()
        # Ties the FHIR sign-in to this browser. Without it, the FHIR login URL below could
        # be handed to someone else, whose FHIR account would then be attached to this user.
        browser = secrets.token_urlsafe(32)
        await self._store.put(
            f"fhir:{state}",
            {"sub": sub, "client": client, "verifier": verifier, "browser": browser, "expect": expect},
            SIGN_IN_SECONDS,
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
        response = RedirectResponse(f"{endpoints['authorize']}?{urlencode(query)}", status_code=302)
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

    async def fhir_callback(self, request: Request) -> Response:
        params = request.query_params
        pending = await self._store.pop(f"fhir:{params.get('state', '')}")
        if not pending:
            return _page(400, "Sign-in expired", "Run the tool again to get a new link.")
        if not secrets.compare_digest(request.cookies.get(self._cookie_name(), ""), pending["browser"]):
            logger.warning("FHIR sign-in finished in a different browser than the one that opened the link.")
            return _forbidden()
        if params.get("error") or not params.get("code"):
            return _page(400, "FHIR sign-in was not completed", params.get("error_description") or params.get("error", ""))
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
        if self._fhir_identity(tokens) != _resource_ref(pending["expect"]):
            # The tokens are dropped: someone other than the MCP user signed in at the FHIR server.
            logger.warning("FHIR connect refused: the FHIR sign-in is not the MCP user's fhirUser.")
            return _forbidden()
        await self._save_user_tokens(f"user:{pending['client']}:{pending['sub']}", tokens, None)
        logger.info("A user connected their FHIR server account (scope: %s).", tokens.get("scope", ""))
        response = _page(200, "Connected", "Your FHIR server account is connected. You can return to your MCP client.")
        response.delete_cookie(self._cookie_name(), path="/")
        return response

    # Helpers

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

    async def _save_user_tokens(self, key: str, tokens: Dict[str, Any], previous: Dict[str, Any] | None) -> str:
        """Keep the user's tokens under `user:<client_id>:<sub>`; a refresh response may
        omit the refresh token and patient."""
        previous = previous or {}
        access_token = str(tokens["access_token"])
        refresh_token = tokens.get("refresh_token") or previous.get("refresh_token")
        expires_in = float(tokens.get("expires_in") or DEFAULT_EXPIRES_IN_SECONDS)
        keep = REFRESH_TOKEN_KEEP_SECONDS if refresh_token else expires_in
        now = time.time()
        # Refresh a little before expiry, but never sooner than halfway through a short-lived token.
        margin = min(REFRESH_MARGIN_SECONDS, expires_in / 2)
        await self._store.put(
            key,
            {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_at": now + expires_in,
                "refresh_at": now + expires_in - margin,
                "patient": tokens.get("patient") or previous.get("patient"),
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

