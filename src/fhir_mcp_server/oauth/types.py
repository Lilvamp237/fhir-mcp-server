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
    # Space- or comma-separated Host and Origin values accepted on HTTP transports.
    # Empty keeps the SDK default (DNS-rebinding protection for localhost only).
    mcp_allowed_hosts: str = ""
    mcp_allowed_origins: str = ""
    mcp_max_request_body_bytes: int | None = None

    # MCP authorization: the MCP server is an OAuth resource server; the IdP issues tokens.
    mcp_auth_mode: Literal["resource_server", "disabled"] = "resource_server"
    mcp_idp_issuer: str = ""
    mcp_idp_ca_cert: str | None = None  # CA bundle for an IdP with a self-signed certificate
    mcp_resource_url: str | None = None  # canonical MCP endpoint URL and expected token audience
    mcp_token_validation: Literal["jwks", "introspection"] = "jwks"
    mcp_jwks_uri: str | None = None  # defaults to the IdP's discovered jwks_uri
    mcp_introspection_client_id: str = ""
    mcp_introspection_client_secret: str = ""
    mcp_read_scope: str = "fhir-read"
    mcp_write_scope: str = "fhir-write"
    mcp_delete_scope: str = "fhir-delete"
    # user_smart_login: confidential web client at the IdP, used to confirm who opens a connect link
    mcp_connect_client_id: str = ""
    mcp_connect_client_secret: str = ""

    # FHIR settings
    server_base_url: str = ""
    server_auth_mode: (
        Literal["none", "static_token", "client_credentials", "private_key_jwt", "user_smart_login"] | None
    ) = None
    server_access_token: str | None = None
    # System credentials (client_credentials, private_key_jwt) and per-user sign-in (user_smart_login)
    server_authorize_url: str | None = None  # user_smart_login; defaults to the FHIR server's SMART configuration
    server_token_url: str | None = None  # defaults to the FHIR server's SMART configuration
    server_token_resource: str | None = None  # RFC 8707 resource for the system token
    server_client_id: str = ""
    server_client_secret: str = ""
    server_private_key_file: str = ""  # PEM private key for private_key_jwt
    server_private_key_id: str | None = None  # `kid` of that key in the registered JWKS
    server_jwt_algorithm: Literal["RS384", "ES384", "RS256", "ES256"] = "RS384"
    server_scopes: str = ""
    # Extra CA certificate trusted for the FHIR server and its token endpoints (e.g. a
    # self-signed IdP or gateway); added to the system trust store, never replacing it.
    server_ca_cert: str | None = None
    # Removed in favour of FHIR_MCP_AUTH_MODE and FHIR_SERVER_AUTH_MODE; rejected at startup.
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
        if self.server_auth_mode:
            return self.server_auth_mode
        return "static_token" if self.server_access_token else "none"

    @property
    def allowed_hosts(self) -> list[str]:
        return _split(self.mcp_allowed_hosts)

    @property
    def allowed_origins(self) -> list[str]:
        return _split(self.mcp_allowed_origins)

    def validate_for_startup(self) -> None:
        """Reject removed settings and incomplete configurations with a clear message."""
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
            if self.mcp_token_validation == "introspection" and not (
                self.mcp_introspection_client_id and self.mcp_introspection_client_secret
            ):
                raise ConfigurationError(
                    "FHIR_MCP_INTROSPECTION_CLIENT_ID and FHIR_MCP_INTROSPECTION_CLIENT_SECRET are "
                    "required when FHIR_MCP_TOKEN_VALIDATION=introspection."
                )
        mode = self.effective_server_auth_mode
        if mode == "static_token" and not self.server_access_token:
            raise ConfigurationError(
                "FHIR_SERVER_ACCESS_TOKEN is required when FHIR_SERVER_AUTH_MODE=static_token."
            )
        if mode == "user_smart_login":
            if self.mcp_auth_mode != "resource_server":
                raise ConfigurationError(
                    "FHIR_SERVER_AUTH_MODE=user_smart_login needs FHIR_MCP_AUTH_MODE=resource_server, "
                    "because each user's FHIR sign-in is tied to their MCP login."
                )
            if not (self.mcp_connect_client_id and self.mcp_connect_client_secret):
                raise ConfigurationError(
                    "FHIR_MCP_CONNECT_CLIENT_ID and FHIR_MCP_CONNECT_CLIENT_SECRET are required when "
                    "FHIR_SERVER_AUTH_MODE=user_smart_login: a web client at your identity provider with "
                    "redirect URI <server URL>/connect/idp/callback."
                )
        if mode in ("client_credentials", "private_key_jwt", "user_smart_login") and not self.server_client_id:
            raise ConfigurationError(
                f"FHIR_SERVER_CLIENT_ID is required when FHIR_SERVER_AUTH_MODE={mode}."
            )
        if mode == "client_credentials" and not self.server_client_secret:
            raise ConfigurationError(
                "FHIR_SERVER_CLIENT_SECRET is required when FHIR_SERVER_AUTH_MODE=client_credentials."
            )
        if mode == "private_key_jwt" and not self.server_private_key_file:
            raise ConfigurationError(
                "FHIR_SERVER_PRIVATE_KEY_FILE is required when FHIR_SERVER_AUTH_MODE=private_key_jwt."
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
