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
that user rather than the MCP server.

When a tool needs the FHIR server and the user has not connected yet, the tool
answers with a URL-mode elicitation pointing at `/connect/fhir`. In the browser:

1. `/connect/fhir` checks the one-time ticket, then signs the browser user in at
   the MCP identity provider, so a link forwarded to someone else cannot attach
   their FHIR account to the wrong MCP user.
2. `/connect/idp/callback` validates the ID token and requires its `sub` to be the
   MCP user the link was issued to, then redirects to the FHIR server's login
   (authorization code with PKCE and `aud`).
3. `/connect/fhir/callback` exchanges the code and keeps the user's FHIR tokens,
   keyed by their MCP `sub`. They never reach the MCP client.
"""

import html
import logging
import secrets
import time

from base64 import urlsafe_b64encode
from hashlib import sha256
from typing import Any, Dict
from urllib.parse import urlencode

import anyio
import httpx2
import jwt

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from fhir_mcp_server.auth.verifiers import DISCOVERY_PATHS, ssl_context
from fhir_mcp_server.backend_auth import (
    CLIENT_ASSERTION_TYPE,
    client_secret_basic,
    fhir_tls,
    signed_client_assertion,
)
from fhir_mcp_server.memory_store import MemoryStore
from fhir_mcp_server.oauth.types import ServerConfigs

logger: logging.Logger = logging.getLogger(__name__)

CONNECT_PATH = "/connect/fhir"
IDP_CALLBACK_PATH = "/connect/idp/callback"
FHIR_CALLBACK_PATH = "/connect/fhir/callback"

TICKET_SECONDS = 600  # how long a connect link stays valid
SIGN_IN_SECONDS = 600  # how long a browser has to finish each sign-in
REFRESH_MARGIN_SECONDS = 60
DEFAULT_EXPIRES_IN_SECONDS = 300
REFRESH_TOKEN_KEEP_SECONDS = 24 * 3600  # how long a connection is kept when a refresh token exists


class FhirConnectError(RuntimeError):
    """A sign-in step with the identity provider or the FHIR server failed."""


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = urlsafe_b64encode(sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _same_url(a: str, b: str) -> bool:
    return a.rstrip("/") == b.rstrip("/")


def _page(status: int, title: str, message: str) -> HTMLResponse:
    body = (
        "<!doctype html><html><head><meta charset='utf-8'><title>FHIR MCP Server</title></head>"
        "<body style='font-family:sans-serif;max-width:40em;margin:4em auto;line-height:1.5'>"
        f"<h1 style='font-size:1.4em'>{html.escape(title)}</h1><p>{html.escape(message)}</p></body></html>"
    )
    return HTMLResponse(body, status_code=status)


class FhirConnect:
    def __init__(self, configs: ServerConfigs) -> None:
        self._configs = configs
        self._store = MemoryStore()
        self._idp_metadata: Dict[str, Any] | None = None
        self._idp_keys: jwt.PyJWKClient | None = None
        self._fhir_endpoints: Dict[str, str] | None = None
        self._refresh_lock = anyio.Lock()

    # Used by the tools

    def connect_link(self, sub: str) -> str:
        """A single-use link that starts the browser sign-in for this MCP user."""
        ticket = secrets.token_urlsafe(32)
        self._store.put(f"ticket:{ticket}", sub, TICKET_SECONDS)
        return f"{self._url(CONNECT_PATH)}?{urlencode({'ticket': ticket})}"

    async def user_token(self, sub: str) -> str | None:
        """The user's FHIR access token, refreshed when it is about to expire."""
        record = self._store.get(f"user:{sub}")
        if record and record["refresh_at"] > time.time():
            return record["access_token"]
        if not record or not record.get("refresh_token"):
            if record and record["expires_at"] > time.time():
                return record["access_token"]  # no refresh token: use it until it expires
            self._store.delete(f"user:{sub}")
            return None
        async with self._refresh_lock:
            record = self._store.get(f"user:{sub}")  # another call may have refreshed it
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
                self._store.delete(f"user:{sub}")
                return None
            return self._save_user_tokens(sub, tokens, record)

    def user_patient(self, sub: str) -> str | None:
        """The patient the user's FHIR sign-in is limited to (SMART `patient` launch
        context), if the FHIR server returned one; for example a MyChart login."""
        record = self._store.get(f"user:{sub}")
        return record.get("patient") if record else None

    # Browser routes

    def register_routes(self, mcp: Any) -> None:
        mcp.custom_route(CONNECT_PATH, methods=["GET"])(self.start)
        mcp.custom_route(IDP_CALLBACK_PATH, methods=["GET"])(self.idp_callback)
        mcp.custom_route(FHIR_CALLBACK_PATH, methods=["GET"])(self.fhir_callback)

    async def start(self, request: Request) -> Response:
        sub = self._store.pop(f"ticket:{request.query_params.get('ticket', '')}")
        if not sub:
            return _page(400, "This link has expired", "Run the tool again to get a new link.")
        try:
            metadata = await self._idp()
        except FhirConnectError as ex:
            logger.error("Cannot reach the identity provider: %s", ex)
            return _page(502, "Sign-in is unavailable", "The identity provider could not be reached.")
        state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        verifier, challenge = _pkce_pair()
        self._store.put(f"idp:{state}", {"sub": sub, "nonce": nonce, "verifier": verifier}, SIGN_IN_SECONDS)
        query = {
            "response_type": "code",
            "client_id": self._configs.mcp_connect_client_id,
            "redirect_uri": self._url(IDP_CALLBACK_PATH),
            "scope": "openid",
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return RedirectResponse(f"{metadata['authorization_endpoint']}?{urlencode(query)}", status_code=302)

    async def idp_callback(self, request: Request) -> Response:
        params = request.query_params
        pending = self._store.pop(f"idp:{params.get('state', '')}")
        if not pending:
            return _page(400, "Sign-in expired", "Run the tool again to get a new link.")
        if params.get("error") or not params.get("code"):
            return _page(400, "Sign-in was not completed", params.get("error_description") or params.get("error", ""))
        try:
            metadata = await self._idp()
            if params.get("iss") and not _same_url(params["iss"], metadata["issuer"]):
                raise FhirConnectError("the authorization response came from a different issuer")
            subject = await self._sign_in_subject(metadata, params["code"], pending)
        except FhirConnectError as ex:
            logger.warning("Rejected identity provider sign-in during FHIR connect: %s", ex)
            return _page(400, "Sign-in failed", "The identity provider sign-in could not be verified.")
        if subject != pending["sub"]:
            logger.warning("FHIR connect link was opened by a different user than it was issued to.")
            return _page(
                403,
                "This link belongs to another user",
                "Sign in as the user who asked to connect, or run the tool again from your own session.",
            )

        try:
            endpoints = await self._fhir()
        except FhirConnectError as ex:
            logger.error("Cannot find the FHIR server's sign-in endpoints: %s", ex)
            return _page(502, "FHIR sign-in is unavailable", "The FHIR server's sign-in could not be found.")
        state = secrets.token_urlsafe(32)
        verifier, challenge = _pkce_pair()
        self._store.put(f"fhir:{state}", {"sub": subject, "verifier": verifier}, SIGN_IN_SECONDS)
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
        return RedirectResponse(f"{endpoints['authorize']}?{urlencode(query)}", status_code=302)

    async def fhir_callback(self, request: Request) -> Response:
        params = request.query_params
        pending = self._store.pop(f"fhir:{params.get('state', '')}")
        if not pending:
            return _page(400, "Sign-in expired", "Run the tool again to get a new link.")
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
        self._save_user_tokens(pending["sub"], tokens, None)
        logger.info("A user connected their FHIR server account (scope: %s).", tokens.get("scope", ""))
        return _page(200, "Connected", "Your FHIR server account is connected. You can return to your MCP client.")

    # Helpers

    def _url(self, path: str) -> str:
        return f"{self._configs.effective_server_url.rstrip('/')}{path}"

    def _save_user_tokens(self, sub: str, tokens: Dict[str, Any], previous: Dict[str, Any] | None) -> str:
        """Keep the user's tokens; a refresh response may omit the refresh token and patient."""
        previous = previous or {}
        access_token = str(tokens["access_token"])
        refresh_token = tokens.get("refresh_token") or previous.get("refresh_token")
        expires_in = float(tokens.get("expires_in") or DEFAULT_EXPIRES_IN_SECONDS)
        keep = REFRESH_TOKEN_KEEP_SECONDS if refresh_token else expires_in
        now = time.time()
        # Refresh a little before expiry, but never sooner than halfway through a short-lived token.
        margin = min(REFRESH_MARGIN_SECONDS, expires_in / 2)
        self._store.put(
            f"user:{sub}",
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

    async def _idp(self) -> Dict[str, Any]:
        if self._idp_metadata is None:
            issuer = self._configs.mcp_idp_issuer.rstrip("/")
            async with httpx2.AsyncClient(verify=ssl_context(self._configs.mcp_idp_ca_cert), timeout=10) as client:
                for path in DISCOVERY_PATHS:
                    try:
                        response = await client.get(f"{issuer}{path}")
                    except httpx2.HTTPError as ex:
                        raise FhirConnectError(str(ex)) from ex
                    if response.status_code == 200 and response.json().get("authorization_endpoint"):
                        self._idp_metadata = response.json()
                        break
            if self._idp_metadata is None:
                raise FhirConnectError(f"no authorization server metadata for {issuer}")
        return self._idp_metadata

    async def _sign_in_subject(self, metadata: Dict[str, Any], code: str, pending: Dict[str, Any]) -> str:
        """Exchange the identity provider's code and return the validated ID token's `sub`."""
        async with httpx2.AsyncClient(verify=ssl_context(self._configs.mcp_idp_ca_cert), timeout=10) as client:
            try:
                response = await client.post(
                    metadata["token_endpoint"],
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": self._url(IDP_CALLBACK_PATH),
                        "code_verifier": pending["verifier"],
                    },
                    headers={
                        "Accept": "application/json",
                        **client_secret_basic(
                            self._configs.mcp_connect_client_id, self._configs.mcp_connect_client_secret
                        ),
                    },
                )
            except httpx2.HTTPError as ex:
                raise FhirConnectError(str(ex)) from ex
        if response.status_code != 200 or not response.json().get("id_token"):
            raise FhirConnectError(f"token endpoint answered HTTP {response.status_code} without an ID token")
        if self._idp_keys is None:
            self._idp_keys = jwt.PyJWKClient(
                metadata["jwks_uri"], cache_keys=True, ssl_context=ssl_context(self._configs.mcp_idp_ca_cert)
            )
        id_token = response.json()["id_token"]
        try:
            key = await anyio.to_thread.run_sync(self._idp_keys.get_signing_key_from_jwt, id_token)
            claims = jwt.decode(
                id_token,
                key.key,
                algorithms=["RS256", "RS384", "RS512", "PS256", "ES256", "ES384"],
                audience=self._configs.mcp_connect_client_id,
                options={"require": ["exp", "iss", "aud", "sub"]},
                leeway=60,
            )
        except jwt.PyJWTError as ex:
            raise FhirConnectError(f"invalid ID token: {ex}") from ex
        if not _same_url(str(claims["iss"]), metadata["issuer"]):
            raise FhirConnectError("ID token issuer does not match the identity provider")
        if claims.get("nonce") != pending["nonce"]:
            raise FhirConnectError("ID token nonce does not match the sign-in request")
        return str(claims["sub"])

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
        if self._configs.server_private_key_file:
            # Confidential client with a key pair (SMART asymmetric client authentication).
            method = f"private_key_jwt (key {self._configs.server_private_key_id or 'without kid'})"
            form = {
                **form,
                "client_assertion_type": CLIENT_ASSERTION_TYPE,
                "client_assertion": signed_client_assertion(self._configs, endpoint),
            }
        elif self._configs.server_client_secret:
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

