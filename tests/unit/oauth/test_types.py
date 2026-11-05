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

import pytest
import os
from unittest.mock import patch
from fhir_mcp_server.oauth.types import (
    ConfigurationError,
    ServerConfigs,
)


class TestServerConfigs:
    """Test the ServerConfigs class."""

    def test_default_config(self):
        """Test default server configuration."""
        # Use empty environment and mock file loading to avoid loading existing config
        with patch.dict(os.environ, {}, clear=True):
            config = ServerConfigs(_env_file=None)
            
            assert config.mcp_host == "localhost"
            assert config.mcp_port == 8000
            assert config.mcp_server_url is None
            assert config.server_client_id == ""
            assert config.server_client_secret == ""
            assert config.server_scopes == ""
            assert config.server_base_url == ""
            assert config.mcp_request_timeout == 30
            assert config.server_access_token is None

    def test_effective_server_url_default(self):
        """Test effective server URL with default values."""
        with patch.dict(os.environ, {}, clear=True):
            config = ServerConfigs(_env_file=None)
            assert config.effective_server_url == "http://localhost:8000"

    def test_effective_server_url_custom_host_port(self):
        """Test effective server URL with custom host and port."""
        with patch.dict(os.environ, {}, clear=True):
            config = ServerConfigs(mcp_host="0.0.0.0", mcp_port=9000, _env_file=None)
            assert config.effective_server_url == "http://0.0.0.0:9000"

    def test_effective_server_url_explicit(self):
        """Test effective server URL with explicit server_url."""
        with patch.dict(os.environ, {}, clear=True):
            config = ServerConfigs(mcp_server_url="https://my-server.com", _env_file=None)
            assert config.effective_server_url == "https://my-server.com"

    def test_config_with_server_values(self):
        """Test server configuration with server values."""
        with patch.dict(os.environ, {}, clear=True):
            config = ServerConfigs(
                server_client_id="test_client",
                server_base_url="https://example.com/fhir",
                mcp_request_timeout=120,
                _env_file=None
            )

            assert config.server_client_id == "test_client"
            assert config.server_base_url == "https://example.com/fhir"
            assert config.mcp_request_timeout == 120

    def test_default_authorization_settings(self):
        """MCP authorization defaults to resource-server mode with per-tool scopes."""
        with patch.dict(os.environ, {}, clear=True):
            config = ServerConfigs(_env_file=None)
            assert config.mcp_auth_mode == "resource_server"
            assert config.mcp_token_validation == "jwks"
            assert (config.mcp_read_scope, config.mcp_write_scope, config.mcp_delete_scope) == (
                "fhir-read", "fhir-write", "fhir-delete"
            )

    def test_effective_resource_url_defaults_to_mcp_endpoint(self):
        with patch.dict(os.environ, {}, clear=True):
            config = ServerConfigs(mcp_server_url="https://mcp.example.com/", _env_file=None)
            assert config.effective_resource_url == "https://mcp.example.com/mcp"

    def test_effective_resource_url_explicit(self):
        with patch.dict(os.environ, {}, clear=True):
            config = ServerConfigs(mcp_resource_url="https://mcp.example.com/fhir/mcp/", _env_file=None)
            assert config.effective_resource_url == "https://mcp.example.com/fhir/mcp"

    def test_server_auth_mode_follows_static_token(self):
        with patch.dict(os.environ, {}, clear=True):
            assert ServerConfigs(_env_file=None).effective_server_auth_mode == "none"
            config = ServerConfigs(server_access_token="abc", _env_file=None)
            assert config.effective_server_auth_mode == "static_token"

    def test_allowed_hosts_and_origins_split(self):
        with patch.dict(os.environ, {}, clear=True):
            config = ServerConfigs(
                mcp_allowed_hosts="mcp.example.com, mcp.example.com:443",
                mcp_allowed_origins="https://app.example.com",
                _env_file=None,
            )
            assert config.allowed_hosts == ["mcp.example.com", "mcp.example.com:443"]
            assert config.allowed_origins == ["https://app.example.com"]

    def test_startup_rejects_removed_disable_authorization(self):
        with patch.dict(os.environ, {"FHIR_SERVER_DISABLE_AUTHORIZATION": "True"}, clear=True):
            config = ServerConfigs(_env_file=None)
            with pytest.raises(ConfigurationError, match="FHIR_MCP_AUTH_MODE=disabled"):
                config.validate_for_startup()

    def test_startup_requires_issuer_in_resource_server_mode(self):
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ConfigurationError, match="FHIR_MCP_IDP_ISSUER"):
                ServerConfigs(_env_file=None).validate_for_startup()
            ServerConfigs(mcp_auth_mode="disabled", _env_file=None).validate_for_startup()
            ServerConfigs(mcp_idp_issuer="https://idp.example.com", _env_file=None).validate_for_startup()

    def test_startup_requires_introspection_client(self):
        with patch.dict(os.environ, {}, clear=True):
            config = ServerConfigs(
                mcp_idp_issuer="https://idp.example.com",
                mcp_token_validation="introspection",
                _env_file=None,
            )
            with pytest.raises(ConfigurationError, match="INTROSPECTION_CLIENT"):
                config.validate_for_startup()

    def test_startup_requires_token_for_static_token_mode(self):
        with patch.dict(os.environ, {}, clear=True):
            config = ServerConfigs(
                mcp_auth_mode="disabled", server_auth_mode="static_token", _env_file=None
            )
            with pytest.raises(ConfigurationError, match="FHIR_SERVER_ACCESS_TOKEN"):
                config.validate_for_startup()

    def test_metadata_url_property(self):
        """Test metadata URL property."""
        with patch.dict(os.environ, {}, clear=True):
            config = ServerConfigs(server_base_url="https://custom.fhir.org/R4", _env_file=None)
            assert config.metadata_url == "https://custom.fhir.org/R4/metadata?_format=json"

    def test_metadata_url_with_trailing_slash(self):
        """Test metadata URL property with trailing slash."""
        with patch.dict(os.environ, {}, clear=True):
            config = ServerConfigs(server_base_url="https://custom.fhir.org/R4/", _env_file=None)
            assert config.metadata_url == "https://custom.fhir.org/R4/metadata?_format=json"

    def test_scopes_property_with_string(self):
        """Test scopes property with string scope."""
        with patch.dict(os.environ, {}, clear=True):
            config = ServerConfigs(server_scopes="read write admin", _env_file=None)
            assert config.scopes == ["read", "write", "admin"]

    def test_scopes_property_empty(self):
        """Test scopes property with empty scope."""
        with patch.dict(os.environ, {}, clear=True):
            config = ServerConfigs(server_scopes="", _env_file=None)
            assert config.scopes == []

    def test_scopes_property_with_extra_spaces(self):
        """Test scopes property with extra spaces."""
        with patch.dict(os.environ, {}, clear=True):
            config = ServerConfigs(server_scopes="  read   write   admin  ", _env_file=None)
            assert config.scopes == ["read", "write", "admin"]

