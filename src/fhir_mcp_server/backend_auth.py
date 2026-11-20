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

"""Credentials for calls from the MCP server to the FHIR server.

- `token_exchange`: the MCP server exchanges the caller's MCP access token
  (RFC 8693) at the same identity provider that issued it, for a new token
  audienced to the FHIR API. The FHIR server sees the real user; the IdP caps
  the exchanged token's scope to what the MCP token was granted. Exchanged
  tokens are cached per user until shortly before they expire.

Also the TLS and client-authentication helpers shared with the per-user FHIR
sign-in (fhir_mcp_server.fhir_connect).
"""

import base64
import logging
import ssl

from functools import cached_property
from typing import Any, Dict
from urllib.parse import quote_plus

import anyio
import httpx2

from fhir_mcp_server.auth.verifiers import DISCOVERY_PATHS, ssl_context
from fhir_mcp_server.oauth.types import ServerConfigs
from fhir_mcp_server.token_store import TokenStore, create_token_store

logger: logging.Logger = logging.getLogger(__name__)

REFRESH_MARGIN_SECONDS = 60
DEFAULT_EXPIRES_IN_SECONDS = 300


class BackendAuthError(RuntimeError):
    """No FHIR credential could be obtained for this request."""


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


TOKEN_EXCHANGE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"


class TokenExchangeProvider:
    """Exchanges the caller's MCP access token for a FHIR-audienced token (RFC 8693),
    at the identity provider named by FHIR_MCP_IDP_ISSUER (not the FHIR server).

    The exchanged token is cached per `sub` until shortly before it expires; a missing
    or expired entry triggers a fresh exchange of whatever MCP token the current
    request carries, so no refresh-token handling is needed for Token B itself.
    """

    def __init__(self, configs: ServerConfigs) -> None:
        self._configs = configs
        self._metadata: Dict[str, Any] | None = None
        self._metadata_lock = anyio.Lock()

    @cached_property
    def _cache(self) -> TokenStore:
        """Exchanged tokens per user (FHIR_MCP_TOKEN_STORE), created on first use."""
        return create_token_store(self._configs)

    async def exchange(self, subject_token: str, sub: str | None) -> str:
        cache_key = f"exchanged:{sub}" if sub else None
        if cache_key and (cached := await self._cache.get(cache_key)) is not None:
            return cached
        endpoint = await self._token_endpoint()
        form: Dict[str, str] = {
            "grant_type": TOKEN_EXCHANGE_GRANT_TYPE,
            "subject_token": subject_token,
            "subject_token_type": ACCESS_TOKEN_TYPE,
        }
        if self._configs.server_token_resource:
            form["resource"] = self._configs.server_token_resource
        if self._configs.server_scopes:
            form["scope"] = " ".join(self._configs.scopes)
        headers: Dict[str, str] = {
            "Accept": "application/json",
            **client_secret_basic(self._configs.server_client_id, self._configs.server_client_secret),
        }
        async with httpx2.AsyncClient(
            verify=ssl_context(self._configs.mcp_idp_ca_cert), timeout=self._configs.mcp_request_timeout
        ) as client:
            response = await client.post(endpoint, data=form, headers=headers)
        if response.status_code != 200:
            raise BackendAuthError(
                f"The identity provider refused the token exchange (HTTP {response.status_code}): "
                f"{_error_summary(response)}"
            )
        payload: Dict[str, Any] = response.json()
        token = payload.get("access_token")
        if not token:
            raise BackendAuthError("The identity provider's token exchange returned no access_token.")
        if cache_key:
            expires_in = float(payload.get("expires_in") or DEFAULT_EXPIRES_IN_SECONDS)
            margin = min(REFRESH_MARGIN_SECONDS, expires_in / 2)
            await self._cache.put(cache_key, token, max(expires_in - margin, 1))
        logger.info(
            "Exchanged the MCP access token for a FHIR-audienced token (scope: %s, expires in %ss).",
            payload.get("scope", ""),
            int(payload.get("expires_in") or DEFAULT_EXPIRES_IN_SECONDS),
        )
        return token

    async def _token_endpoint(self) -> str:
        async with self._metadata_lock:
            if self._metadata is None:
                self._metadata = await self._discover()
        endpoint = self._metadata.get("token_endpoint")
        if not endpoint:
            raise BackendAuthError(f"{self._configs.mcp_idp_issuer} publishes no token_endpoint.")
        return str(endpoint)

    async def _discover(self) -> Dict[str, Any]:
        issuer = self._configs.mcp_idp_issuer.rstrip("/")
        async with httpx2.AsyncClient(verify=ssl_context(self._configs.mcp_idp_ca_cert), timeout=10) as client:
            for path in DISCOVERY_PATHS:
                response = await client.get(f"{issuer}{path}")
                if response.status_code == 200 and response.json().get("token_endpoint"):
                    return response.json()
        raise BackendAuthError(f"No authorization server metadata found for issuer {issuer!r}.")


def _error_summary(response: httpx2.Response) -> str:
    """The OAuth error code and description, without echoing anything else."""
    try:
        body = response.json()
    except ValueError:
        return "non-JSON error response"
    error = body.get("error") or "unknown_error"
    description = body.get("error_description")
    return f"{error}: {description}" if description else error

