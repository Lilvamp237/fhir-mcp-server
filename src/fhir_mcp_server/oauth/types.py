# Copyright (c) 2025-2026, WSO2 LLC. (https://www.wso2.com/) All Rights Reserved.

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

import logging

from typing import Literal
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class ConfigurationError(ValueError):
    """Raised at startup when the server's settings are invalid or outdated."""


def _split(value: str) -> list[str]:
    """Split a space- or comma-separated setting into its non-empty items."""
    return [item for item in value.replace(",", " ").split() if item]


class ServerConfigs(BaseSettings):
    """Contains environment configurations of the MCP server."""

    model_config = SettingsConfigDict(
        env_prefix="FHIR_",
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    # MCP Server settings
    mcp_host: str = "localhost"
    mcp_port: int = 8000
    mcp_server_url: str | None = None
    mcp_request_timeout: int = 30  # in secs
    mcp_allowed_hosts: str = ""
    mcp_allowed_origins: str = ""
    mcp_max_request_body_bytes: int | None = None

    # MCP authorization: the MCP server is an OAuth resource server.
    mcp_auth_mode: Literal["resource_server", "disabled"] = "resource_server"
    mcp_idp_issuer: str = ""
    mcp_idp_ca_cert: str | None = None  # CA bundle for an IdP with a self-signed certificate
    mcp_resource_url: str | None = None  # canonical MCP endpoint URL and expected token audience
    mcp_jwks_uri: str | None = None  # defaults to the IdP's discovered jwks_uri
    mcp_read_scope: str = "fhir-read"
    mcp_write_scope: str = "fhir-write"
    mcp_delete_scope: str = "fhir-delete"
    # Where per-user FHIR tokens and pending sign-ins are kept: "memory" (default, lost on
    # restart) or "module:function" returning a custom store (see fhir_mcp_server.token_store).
    mcp_token_store: str = "memory"

    # FHIR settings
    server_base_url: str = ""
    # none: no credential (open or local FHIR server)
    # token_exchange: exchange the caller's MCP token at the same IdP (RFC 8693)
    # user_smart_login: each user signs in at the FHIR server (SMART App Launch); the IdP
    #   federates to the FHIR server, and the FHIR sign-in must match the MCP token's fhirUser
    # token_vault: the IdP keeps the user's FHIR token (e.g. from a federated sign-in) and
    #   releases it to the MCP server for the caller's MCP token
    server_auth_mode: Literal["none", "token_exchange", "user_smart_login", "token_vault"] = "none"
    server_authorize_url: str | None = None  # user_smart_login; defaults to the FHIR server's SMART configuration
    server_token_url: str | None = None
    server_token_resource: str | None = None  # token_exchange: the FHIR API's audience; token_vault: its resource
    server_vault_grant_type: str = "urn:thunderid:params:oauth:grant-type:upstream-token"
    server_client_id: str = ""
    server_client_secret: str = ""
    server_scopes: str = ""
    server_ca_cert: str | None = None

    server_disable_authorization: str | None = None

    @property
    def metadata_url(self) -> str:
        return f"{self.server_base_url.rstrip('/')}/metadata?_format=json"

    @property
    def scopes(self) -> list[str]:
        return _split(self.server_scopes)

    @property
    def effective_server_url(self) -> str:
        return self.mcp_server_url or f"http://{self.mcp_host}:{self.mcp_port}"

    @property
    def effective_resource_url(self) -> str:
        """The canonical URL of the MCP endpoint; tokens must be issued for it."""
        return (self.mcp_resource_url or f"{self.effective_server_url.rstrip('/')}/mcp").rstrip("/")

    @property
    def effective_server_auth_mode(self) -> str:
        return self.server_auth_mode

    @property
    def allowed_hosts(self) -> list[str]:
        return _split(self.mcp_allowed_hosts)

    @property
    def allowed_origins(self) -> list[str]:
        return _split(self.mcp_allowed_origins)

    def validate_for_startup(self) -> None:
        """Reject removed settings and incomplete configurations with a clear message."""
        if self.mcp_token_store.strip() != "memory":
            from fhir_mcp_server.token_store import store_factory  # avoids a circular import

            store_factory(self.mcp_token_store.strip())  # raises ConfigurationError if it cannot load
        if self.server_disable_authorization is not None:
            raise ConfigurationError(
                "FHIR_SERVER_DISABLE_AUTHORIZATION is no longer supported. Use "
                "FHIR_MCP_AUTH_MODE=disabled (MCP login) and FHIR_SERVER_AUTH_MODE "
                "(how the server calls the FHIR server) instead."
            )
        if self.mcp_auth_mode == "resource_server":
            if not self.mcp_idp_issuer:
                raise ConfigurationError(
                    "FHIR_MCP_IDP_ISSUER is required when FHIR_MCP_AUTH_MODE=resource_server. "
                    "Set it to your identity provider's issuer URL, or set FHIR_MCP_AUTH_MODE=disabled "
                    "for local development."
                )
        mode = self.effective_server_auth_mode
        if mode == "user_smart_login":
            if self.mcp_auth_mode != "resource_server":
                raise ConfigurationError(
                    "FHIR_SERVER_AUTH_MODE=user_smart_login needs FHIR_MCP_AUTH_MODE=resource_server, "
                    "because each user's FHIR sign-in is tied to their MCP login."
                )
            if "openid" not in self.scopes:
                raise ConfigurationError(
                    "FHIR_SERVER_AUTH_MODE=user_smart_login needs `openid fhirUser` in FHIR_SERVER_SCOPES, "
                    "so the FHIR server says who signed in."
                )
        if mode in ("token_exchange", "token_vault"):
            if self.mcp_auth_mode != "resource_server":
                raise ConfigurationError(
                    f"FHIR_SERVER_AUTH_MODE={mode} needs FHIR_MCP_AUTH_MODE=resource_server: "
                    "it sends the caller's MCP access token back to the IdP that issued it."
                )
            if not self.server_token_resource:
                raise ConfigurationError(
                    f"FHIR_SERVER_TOKEN_RESOURCE is required when FHIR_SERVER_AUTH_MODE={mode}: "
                    "the FHIR API to get the user's token for."
                )
        if mode in ("token_exchange", "token_vault", "user_smart_login") and not self.server_client_id:
            raise ConfigurationError(
                f"FHIR_SERVER_CLIENT_ID is required when FHIR_SERVER_AUTH_MODE={mode}."
            )
        if mode in ("token_exchange", "token_vault") and not self.server_client_secret:
            raise ConfigurationError(
                f"FHIR_SERVER_CLIENT_SECRET is required when FHIR_SERVER_AUTH_MODE={mode}."
            )


class IDToken(BaseModel):
    fhirUser: str | None = None

    def parse_fhir_user(self) -> tuple[str, str] | None:
        """
        Parse the fhirUser URL to extract resource type and resource ID.

        The fhirUser URL MAY be absolute (e.g., https://ehr.example.org/Practitioner/123),
        or it MAY be relative to the FHIR server base URL (e.g., Practitioner/123).

        Returns:
            A tuple of (resource_type, resource_id) if fhirUser is valid,
            None otherwise.
        """
        if not self.fhirUser:
            return None

        logger.debug(f"Parsing fhirUser: {self.fhirUser}")
        parts: list[str] = self.fhirUser.rstrip('/').split("/")

        if len(parts) < 2:
            return None

        return parts[len(parts) - 2], parts[len(parts) - 1]

    @property
    def resource_type(self) -> str | None:
        """Get the FHIR resource type from fhirUser URL."""
        parsed = self.parse_fhir_user()
        return parsed[0] if parsed else None

    @property
    def resource_id(self) -> str | None:
        """Get the FHIR resource ID from fhirUser URL."""
        parsed = self.parse_fhir_user()
        return parsed[1] if parsed else None
