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

"""System credentials for calls from the MCP server to the FHIR server.

The FHIR server sees the MCP server's own identity, never the MCP client's
token. Two OAuth client-credentials variants are supported:

- `client_credentials`: the client authenticates with a client secret.
- `private_key_jwt`: the client signs a short-lived JWT assertion with its
  private key (RFC 7523), as SMART Backend Services requires (e.g. Epic).

The token is cached in memory until shortly before it expires.
"""

import base64
import logging
import ssl
import time
import uuid

from typing import Any, Dict
from urllib.parse import quote_plus

import anyio
import httpx2
import jwt

from fhir_mcp_server.oauth.types import ServerConfigs

logger: logging.Logger = logging.getLogger(__name__)

CLIENT_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
ASSERTION_LIFETIME_SECONDS = 300  # SMART Backend Services allows at most five minutes
REFRESH_MARGIN_SECONDS = 60
DEFAULT_EXPIRES_IN_SECONDS = 300


class BackendAuthError(RuntimeError):
    """The FHIR server's authorization server did not issue a system token."""


def fhir_tls(configs: ServerConfigs) -> ssl.SSLContext | bool:
    """TLS verification for calls to the FHIR server and its token endpoints.

    Without FHIR_SERVER_CA_CERT this is the client default. With it, the system's
    trusted CAs plus that certificate, so public servers such as Epic still verify.
    """
    if not configs.server_ca_cert:
        return True
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=configs.server_ca_cert)
    return context


def client_secret_basic(client_id: str, client_secret: str) -> Dict[str, str]:
    """The `Authorization` header for client_secret_basic (RFC 6749 section 2.3.1).

    The ID and secret are form-urlencoded before Base64, so secrets containing
    characters such as `+`, `/` or `=` reach the server unchanged.
    """
    credentials = f"{quote_plus(client_id)}:{quote_plus(client_secret)}"
    return {"Authorization": "Basic " + base64.b64encode(credentials.encode()).decode()}


class SystemTokenProvider:
    def __init__(self, configs: ServerConfigs) -> None:
        self._configs = configs
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._token_endpoint: str | None = configs.server_token_url
        self._lock = anyio.Lock()

    async def get_token(self) -> str:
        async with self._lock:
            if self._token and time.time() < self._expires_at - REFRESH_MARGIN_SECONDS:
                return self._token
            self._token, self._expires_at = await self._request_token()
            return self._token

    async def _request_token(self) -> tuple[str, float]:
        endpoint = await self._endpoint()
        form: Dict[str, str] = {"grant_type": "client_credentials"}
        if self._configs.server_scopes:
            form["scope"] = " ".join(self._configs.scopes)
        if self._configs.server_token_resource:
            form["resource"] = self._configs.server_token_resource
        headers: Dict[str, str] = {"Accept": "application/json"}
        if self._configs.effective_server_auth_mode == "private_key_jwt":
            form["client_assertion_type"] = CLIENT_ASSERTION_TYPE
            form["client_assertion"] = self._client_assertion(endpoint)
        else:
            headers.update(client_secret_basic(self._configs.server_client_id, self._configs.server_client_secret))

        async with httpx2.AsyncClient(
            verify=fhir_tls(self._configs), timeout=self._configs.mcp_request_timeout
        ) as client:
            response = await client.post(endpoint, data=form, headers=headers)
        if response.status_code != 200:
            raise BackendAuthError(
                f"The FHIR server's token endpoint refused the system token request "
                f"(HTTP {response.status_code}): {_error_summary(response)}"
            )
        payload: Dict[str, Any] = response.json()
        token = payload.get("access_token")
        if not token:
            raise BackendAuthError("The FHIR server's token endpoint returned no access_token.")
        expires_in = float(payload.get("expires_in") or DEFAULT_EXPIRES_IN_SECONDS)
        logger.info(
            "Obtained a system token for the FHIR server (scope: %s, expires in %ss).",
            payload.get("scope", ""),
            int(expires_in),
        )
        return token, time.time() + expires_in

    async def _endpoint(self) -> str:
        if self._token_endpoint:
            return self._token_endpoint
        discovery = f"{self._configs.server_base_url.rstrip('/')}/.well-known/smart-configuration"
        async with httpx2.AsyncClient(
            verify=fhir_tls(self._configs), timeout=self._configs.mcp_request_timeout
        ) as client:
            response = await client.get(discovery, headers={"Accept": "application/json"})
        if response.status_code != 200 or not response.json().get("token_endpoint"):
            raise BackendAuthError(
                f"No token endpoint found at {discovery}; set FHIR_SERVER_TOKEN_URL."
            )
        self._token_endpoint = str(response.json()["token_endpoint"])
        return self._token_endpoint

    def _client_assertion(self, token_endpoint: str) -> str:
        return signed_client_assertion(self._configs, token_endpoint)


def signed_client_assertion(configs: ServerConfigs, token_endpoint: str) -> str:
    """A short-lived JWT signed with FHIR_SERVER_PRIVATE_KEY_FILE that authenticates
    the client at the token endpoint (private_key_jwt, RFC 7523; SMART asymmetric
    client authentication)."""
    now = int(time.time())
    client_id = configs.server_client_id
    with open(configs.server_private_key_file, "rb") as key_file:
        private_key = key_file.read()
    headers = {"typ": "JWT"}
    if configs.server_private_key_id:
        headers["kid"] = configs.server_private_key_id
    return jwt.encode(
        {
            "iss": client_id,
            "sub": client_id,
            "aud": token_endpoint,
            "jti": str(uuid.uuid4()),
            "iat": now,
            "nbf": now,
            "exp": now + ASSERTION_LIFETIME_SECONDS,
        },
        private_key,
        algorithm=configs.server_jwt_algorithm,
        headers=headers,
    )


def _error_summary(response: httpx2.Response) -> str:
    """The OAuth error code and description, without echoing anything else."""
    try:
        body = response.json()
    except ValueError:
        return "non-JSON error response"
    error = body.get("error") or "unknown_error"
    description = body.get("error_description")
    return f"{error}: {description}" if description else error

