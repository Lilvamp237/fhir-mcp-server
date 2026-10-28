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

import os
from unittest.mock import patch

from fhir_mcp_server.auth import (
    IntrospectionTokenVerifier,
    JwksTokenVerifier,
    create_token_verifier,
    tool_scopes,
)
from fhir_mcp_server.oauth.types import ServerConfigs


class TestIntegration:
    """Integration tests for the FHIR MCP server components."""

    def test_server_configs_integration(self):
        """Test that server configurations combine as expected."""
        with patch.dict(os.environ, {}, clear=True):
            config = ServerConfigs(mcp_host="0.0.0.0", mcp_port=9000, _env_file=None)
            config.server_base_url = "https://custom.fhir.org"
            config.mcp_request_timeout = 60

            assert config.effective_server_url == "http://0.0.0.0:9000"
            assert config.effective_resource_url == "http://0.0.0.0:9000/mcp"
            assert config.server_base_url == "https://custom.fhir.org"
            assert config.mcp_request_timeout == 60

    def test_backend_client_settings(self):
        """FHIR client settings are kept for the backend, separate from MCP login."""
        with patch.dict(os.environ, {}, clear=True):
            config = ServerConfigs(_env_file=None)
            config.server_client_id = "test_client"
            config.server_client_secret = "test_secret"
            config.server_scopes = "system/*.rs, fhir-access"

            assert config.server_client_id == "test_client"
            assert config.scopes == ["system/*.rs", "fhir-access"]

    def test_token_verifier_follows_validation_setting(self):
        with patch.dict(os.environ, {}, clear=True):
            jwks = ServerConfigs(mcp_idp_issuer="https://idp.example.com", _env_file=None)
            introspection = ServerConfigs(
                mcp_idp_issuer="https://idp.example.com/",
                mcp_token_validation="introspection",
                mcp_introspection_client_id="rs",
                mcp_introspection_client_secret="rs-secret",
                _env_file=None,
            )

            assert isinstance(create_token_verifier(jwks), JwksTokenVerifier)
            verifier = create_token_verifier(introspection)
            assert isinstance(verifier, IntrospectionTokenVerifier)
            assert verifier.issuer == "https://idp.example.com"
            assert verifier.resource == "http://localhost:8000/mcp"

    def test_tool_scopes_follow_configured_scope_names(self):
        with patch.dict(os.environ, {}, clear=True):
            config = ServerConfigs(
                mcp_read_scope="user/*.rs",
                mcp_write_scope="user/*.cu",
                mcp_delete_scope="user/*.d",
                _env_file=None,
            )
            assert tool_scopes(config) == {
                "get_capabilities": "user/*.rs",
                "search": "user/*.rs",
                "read": "user/*.rs",
                "get_user": "user/*.rs",
                "create": "user/*.cu",
                "update": "user/*.cu",
                "delete": "user/*.d",
            }

    def test_metadata_url_generation(self):
        with patch.dict(os.environ, {}, clear=True):
            config = ServerConfigs(server_base_url="https://mock.fhir.local/R4/", _env_file=None)
            assert config.metadata_url == "https://mock.fhir.local/R4/metadata?_format=json"
