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

    # FHIR settings
    server_base_url: str = ""
    # The FHIR server's app (for example an Epic app) that each user signs in with, on behalf
    # of the agent they link. Without it, requests to the FHIR server carry no credential
    # (an open or local FHIR server).
    server_client_id: str = ""
    server_client_secret: str = ""  # empty: a public client (PKCE only)
    server_scopes: str = ""
    server_authorize_url: str | None = None  # defaults to the FHIR server's SMART configuration
    server_token_url: str | None = None
    server_ca_cert: str | None = None

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
    def fhir_sign_in(self) -> bool:
        """Whether users sign in at the FHIR server for the agents they link
        (FHIR_SERVER_CLIENT_ID is set). Otherwise the FHIR server gets no credential."""
        return bool(self.server_client_id)

    @property
    def allowed_hosts(self) -> list[str]:
        return _split(self.mcp_allowed_hosts)

    @property
    def allowed_origins(self) -> list[str]:
        return _split(self.mcp_allowed_origins)

    def validate_for_startup(self) -> None:
        """Reject incomplete configurations with a clear message."""
        if self.mcp_auth_mode == "resource_server":
            if not self.mcp_idp_issuer:
                raise ConfigurationError(
                    "FHIR_MCP_IDP_ISSUER is required when FHIR_MCP_AUTH_MODE=resource_server. "
                    "Set it to your identity provider's issuer URL, or set FHIR_MCP_AUTH_MODE=disabled "
                    "for local development."
                )
        if self.fhir_sign_in:
            if self.mcp_auth_mode != "resource_server":
                raise ConfigurationError(
                    "FHIR_SERVER_CLIENT_ID needs FHIR_MCP_AUTH_MODE=resource_server: each user's FHIR "
                    "sign-in is tied to the agent named by its MCP access token."
                )
            if "openid" not in self.scopes:
                raise ConfigurationError(
                    "FHIR_SERVER_SCOPES needs `openid fhirUser`, so the FHIR server says who signed in."
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
