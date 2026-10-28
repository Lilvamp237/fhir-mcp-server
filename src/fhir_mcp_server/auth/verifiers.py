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

"""Validate access tokens issued by the identity provider (IdP).

The MCP server is an OAuth resource server: it never issues tokens. Every
request's bearer token is checked here, either locally against the IdP's
signing keys (JWKS) or by asking the IdP (RFC 7662 introspection).
"""

import logging
import ssl
import time

from typing import Any, Dict, Iterable

import anyio
import httpx2
import jwt

from mcp.server.auth.provider import AccessToken, TokenVerifier

from fhir_mcp_server.oauth.types import ServerConfigs

logger: logging.Logger = logging.getLogger(__name__)

# Asymmetric algorithms only: never "none" or HMAC, which would let a holder of
# public material forge tokens.
ALLOWED_ALGORITHMS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384"]
CLOCK_SKEW_SECONDS = 60
INTROSPECTION_CACHE_SECONDS = 60
DISCOVERY_PATHS = (
    "/.well-known/oauth-authorization-server",
    "/.well-known/openid-configuration",
)


def ssl_context(ca_cert: str | None) -> ssl.SSLContext:
    return ssl.create_default_context(cafile=ca_cert) if ca_cert else ssl.create_default_context()


def _same_url(a: str, b: str) -> bool:
    return a.rstrip("/") == b.rstrip("/")


def _audiences(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Iterable):
        return [str(item) for item in value]
    return []


def _scopes(claims: Dict[str, Any]) -> list[str]:
    scope = claims.get("scope", claims.get("scp", ""))
    if isinstance(scope, str):
        return scope.split()
    return [str(item) for item in scope or []]


class IdpTokenVerifier(TokenVerifier):
    """Shared checks and IdP discovery for both validation methods."""

    def __init__(self, configs: ServerConfigs) -> None:
        self.issuer = configs.mcp_idp_issuer.rstrip("/")
        self.resource = configs.effective_resource_url
        self._configs = configs
        self._tls = ssl_context(configs.mcp_idp_ca_cert)
        self._metadata: Dict[str, Any] | None = None
        self._metadata_lock = anyio.Lock()

    async def metadata(self) -> Dict[str, Any]:
        """The IdP's authorization server metadata (RFC 8414 or OIDC discovery)."""
        async with self._metadata_lock:
            if self._metadata is None:
                self._metadata = await self._discover()
            return self._metadata

    async def _discover(self) -> Dict[str, Any]:
        async with httpx2.AsyncClient(verify=self._tls, timeout=10) as client:
            for path in DISCOVERY_PATHS:
                response = await client.get(f"{self.issuer}{path}")
                if response.status_code == 200:
                    metadata = response.json()
                    if not _same_url(str(metadata.get("issuer", "")), self.issuer):
                        raise ValueError(
                            f"IdP metadata issuer {metadata.get('issuer')!r} does not match "
                            f"FHIR_MCP_IDP_ISSUER {self.issuer!r}"
                        )
                    return metadata
        raise ValueError(f"No authorization server metadata found for issuer {self.issuer!r}")

    def _access_token(self, token: str, claims: Dict[str, Any]) -> AccessToken | None:
        """Apply the checks common to both methods and build the SDK's AccessToken."""
        if not _same_url(str(claims.get("iss", "")), self.issuer):
            logger.info("Rejected token: issuer %r is not the configured IdP", claims.get("iss"))
            return None
        if not any(_same_url(aud, self.resource) for aud in _audiences(claims.get("aud"))):
            logger.info("Rejected token: audience %r is not %s", claims.get("aud"), self.resource)
            return None
        expires_at = claims.get("exp")
        return AccessToken(
            token=token,
            client_id=str(claims.get("client_id") or claims.get("azp") or ""),
            scopes=_scopes(claims),
            expires_at=int(expires_at) if expires_at is not None else None,
            resource=self.resource,
            subject=claims.get("sub"),
            claims=claims,
        )


class JwksTokenVerifier(IdpTokenVerifier):
    """Checks the token's signature against the IdP's published keys, then its claims."""

    def __init__(self, configs: ServerConfigs) -> None:
        super().__init__(configs)
        self._jwks_client: jwt.PyJWKClient | None = None

    async def _keys(self) -> jwt.PyJWKClient:
        if self._jwks_client is None:
            uri = self._configs.mcp_jwks_uri or (await self.metadata()).get("jwks_uri")
            if not uri:
                raise ValueError("The IdP publishes no jwks_uri; set FHIR_MCP_JWKS_URI")
            # Keys are cached and re-fetched when a token names an unknown key ID.
            self._jwks_client = jwt.PyJWKClient(uri, cache_keys=True, ssl_context=self._tls)
        return self._jwks_client

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            keys = await self._keys()
            signing_key = await anyio.to_thread.run_sync(keys.get_signing_key_from_jwt, token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=ALLOWED_ALGORITHMS,
                options={"require": ["exp", "iss", "aud"], "verify_aud": False, "verify_iss": False},
                leeway=CLOCK_SKEW_SECONDS,
            )
        except jwt.PyJWTError as ex:
            logger.info("Rejected token: %s", ex)
            return None
        except Exception as ex:
            logger.error("Unable to validate token against the IdP's keys: %s", ex)
            return None
        return self._access_token(token, claims)


class IntrospectionTokenVerifier(IdpTokenVerifier):
    """Asks the IdP whether the token is active (RFC 7662). Works for opaque tokens
    and sees revocation immediately, at the cost of one IdP call per new token."""

    def __init__(self, configs: ServerConfigs) -> None:
        super().__init__(configs)
        self._auth = (configs.mcp_introspection_client_id, configs.mcp_introspection_client_secret)
        self._cache: Dict[str, tuple[float, Dict[str, Any]]] = {}

    async def verify_token(self, token: str) -> AccessToken | None:
        now = time.time()
        cached = self._cache.get(token)
        if cached and cached[0] > now:
            claims = cached[1]
        else:
            try:
                claims = await self._introspect(token)
            except Exception as ex:
                logger.error("Unable to introspect token with the IdP: %s", ex)
                return None
            if not claims.get("active"):
                logger.info("Rejected token: the IdP reports it inactive")
                return None
            expiry = float(claims.get("exp") or now + INTROSPECTION_CACHE_SECONDS)
            self._cache[token] = (min(expiry, now + INTROSPECTION_CACHE_SECONDS), claims)
            self._cache = {k: v for k, v in self._cache.items() if v[0] > now}

        if claims.get("exp") is not None and float(claims["exp"]) + CLOCK_SKEW_SECONDS < now:
            logger.info("Rejected token: expired")
            return None
        return self._access_token(token, claims)

    async def _introspect(self, token: str) -> Dict[str, Any]:
        endpoint = (await self.metadata()).get("introspection_endpoint")
        if not endpoint:
            raise ValueError("The IdP publishes no introspection_endpoint")
        async with httpx2.AsyncClient(verify=self._tls, timeout=10) as client:
            response = await client.post(
                endpoint,
                data={"token": token, "token_type_hint": "access_token"},
                auth=self._auth,
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            return response.json()


def create_token_verifier(configs: ServerConfigs) -> IdpTokenVerifier:
    if configs.mcp_token_validation == "introspection":
        return IntrospectionTokenVerifier(configs)
    return JwksTokenVerifier(configs)
