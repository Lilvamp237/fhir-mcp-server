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

"""Token exchange at the MCP identity provider, and the FHIR TLS setting."""

import base64
import datetime
import ipaddress
import json
import os
import ssl
import threading

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Tuple
from unittest.mock import patch
from urllib.parse import parse_qs

import pytest

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from fhir_mcp_server.backend_auth import BackendAuthError, TokenExchangeProvider, fhir_tls
from fhir_mcp_server.oauth.types import ConfigurationError, ServerConfigs


class IdpStub:
    """Answers OIDC discovery and a token-exchange grant at the IdP's token endpoint."""

    def __init__(self, status: int = 200, expires_in: int = 300) -> None:
        self.requests: List[Tuple[Dict[str, str], str | None]] = []
        self.status = status
        self.expires_in = expires_in
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == "/.well-known/openid-configuration":
                    self._send(200, {"issuer": stub.base_url, "token_endpoint": f"{stub.base_url}/oauth2/token"})
                else:
                    self._send(404, {})

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
                stub.requests.append((form, self.headers.get("Authorization")))
                if stub.status != 200:
                    self._send(stub.status, {"error": "invalid_client", "error_description": "unknown client"})
                    return
                self._send(
                    200,
                    {
                        "access_token": f"exchanged-token-{len(stub.requests)}",
                        "issued_token_type": "urn:ietf:params:oauth:token-type:access_token",
                        "token_type": "Bearer",
                        "expires_in": stub.expires_in,
                        "scope": form.get("scope", ""),
                    },
                )

            def _send(self, status: int, payload: Dict[str, Any]) -> None:
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args: Any) -> None:
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture
def idp():
    stub = IdpStub()
    try:
        yield stub
    finally:
        stub.stop()


@pytest.fixture
def self_signed_cert(tmp_path):
    """A self-signed certificate for localhost, like a local IdP's: (cert file, key file)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_file, key_file = tmp_path / "idp.cert", tmp_path / "idp.key"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return str(cert_file), str(key_file)


def make_configs(**values: Any) -> ServerConfigs:
    with patch.dict(os.environ, {}, clear=True):
        return ServerConfigs(_env_file=None, **values)


class TestTokenExchange:
    def _configs(self, idp: "IdpStub", **values: Any) -> ServerConfigs:
        return make_configs(
            mcp_idp_issuer=idp.base_url,
            server_auth_mode="token_exchange",
            server_client_id="mcp-server",
            server_client_secret="s3cret",
            server_token_resource="http://localhost:9090/fhir/r4",
            **values,
        )

    async def test_exchanges_the_mcp_token_for_a_fhir_audienced_one(self, idp):
        token = await TokenExchangeProvider(self._configs(idp)).exchange("mcp-access-token", "user-1")

        assert token == "exchanged-token-1"
        form, authorization = idp.requests[0]
        assert form == {
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": "mcp-access-token",
            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "resource": "http://localhost:9090/fhir/r4",
        }
        assert authorization == "Basic " + base64.b64encode(b"mcp-server:s3cret").decode()

    async def test_passes_the_configured_scope(self, idp):
        await TokenExchangeProvider(
            self._configs(idp, server_scopes="fhir.read fhir.write")
        ).exchange("mcp-access-token", "user-1")

        form, _ = idp.requests[0]
        assert form["scope"] == "fhir.read fhir.write"

    async def test_caches_the_exchanged_token_per_subject(self, idp):
        provider = TokenExchangeProvider(self._configs(idp))

        assert await provider.exchange("token-a", "user-1") == "exchanged-token-1"
        assert await provider.exchange("token-a", "user-1") == "exchanged-token-1"
        assert await provider.exchange("token-b", "user-2") == "exchanged-token-2"
        assert len(idp.requests) == 2

    async def test_no_subject_means_no_caching(self, idp):
        provider = TokenExchangeProvider(self._configs(idp))

        await provider.exchange("token-a", None)
        await provider.exchange("token-a", None)

        assert len(idp.requests) == 2

    async def test_refused_exchange_raises_without_echoing_secrets(self):
        stub = IdpStub(status=401)
        try:
            configs = self._configs(stub)
            with pytest.raises(BackendAuthError) as error:
                await TokenExchangeProvider(configs).exchange("mcp-access-token", "user-1")
        finally:
            stub.stop()

        assert "HTTP 401" in str(error.value)
        assert "invalid_client" in str(error.value)
        assert "s3cret" not in str(error.value)


class TestTokenVault:
    """FHIR_SERVER_AUTH_MODE=token_vault: the IdP's vault releases the user's stored FHIR token."""

    def _configs(self, idp: "IdpStub", **values: Any) -> ServerConfigs:
        return make_configs(
            mcp_idp_issuer=idp.base_url,
            server_auth_mode="token_vault",
            server_client_id="fhir-mcp",
            server_client_secret="s3cret",
            server_token_resource="https://fhir.example.org/R4",
            **values,
        )

    async def test_asks_the_vault_with_its_own_grant(self, idp):
        token = await TokenExchangeProvider(self._configs(idp)).exchange("mcp-access-token", "user-1")

        assert token == "exchanged-token-1"
        form, authorization = idp.requests[0]
        assert form == {
            "grant_type": "urn:thunderid:params:oauth:grant-type:upstream-token",
            "subject_token": "mcp-access-token",
            "resource": "https://fhir.example.org/R4",
        }
        assert authorization == "Basic " + base64.b64encode(b"fhir-mcp:s3cret").decode()

    async def test_grant_type_is_configurable(self, idp):
        configs = self._configs(idp, server_vault_grant_type="urn:example:upstream-token")
        await TokenExchangeProvider(configs).exchange("mcp-access-token", "user-1")

        assert idp.requests[0][0]["grant_type"] == "urn:example:upstream-token"

    def test_needs_resource_client_and_mcp_login(self):
        base = {"server_auth_mode": "token_vault", "mcp_idp_issuer": "https://idp.example.com"}
        with pytest.raises(ConfigurationError, match="FHIR_SERVER_TOKEN_RESOURCE"):
            make_configs(**base, server_client_id="a", server_client_secret="b").validate_for_startup()
        with pytest.raises(ConfigurationError, match="FHIR_SERVER_CLIENT_SECRET"):
            make_configs(**base, server_client_id="a", server_token_resource="r").validate_for_startup()
        with pytest.raises(ConfigurationError, match="FHIR_MCP_AUTH_MODE=resource_server"):
            make_configs(
                **base, mcp_auth_mode="disabled", server_client_id="a", server_client_secret="b",
                server_token_resource="r",
            ).validate_for_startup()


class TestFhirTls:
    """FHIR_SERVER_CA_CERT: a FHIR server or token endpoint with a self-signed certificate."""

    def test_adds_to_the_system_trust_store(self, self_signed_cert):
        assert fhir_tls(make_configs()) is True
        system = ssl.create_default_context().cert_store_stats()["x509_ca"]
        context = fhir_tls(make_configs(server_ca_cert=self_signed_cert[0]))
        assert isinstance(context, ssl.SSLContext)
        assert context.cert_store_stats()["x509_ca"] == system + 1


class TestConfiguration:
    def _check(self, **values: Any) -> None:
        values.setdefault("mcp_auth_mode", "disabled")
        make_configs(**values).validate_for_startup()

    def test_token_exchange_requires_resource_server_mode(self):
        with pytest.raises(ConfigurationError, match="FHIR_MCP_AUTH_MODE=resource_server"):
            self._check(
                server_auth_mode="token_exchange",
                mcp_idp_issuer="https://idp.example.com",
                server_client_id="a",
                server_client_secret="b",
                server_token_resource="https://fhir.example.com",
            )

    def test_token_exchange_requires_id_secret_and_resource(self):
        base = {
            "server_auth_mode": "token_exchange",
            "mcp_idp_issuer": "https://idp.example.com",
            "mcp_auth_mode": "resource_server",
        }
        with pytest.raises(ConfigurationError, match="FHIR_SERVER_TOKEN_RESOURCE"):
            self._check(**base, server_client_id="a", server_client_secret="b")
        with pytest.raises(ConfigurationError, match="FHIR_SERVER_CLIENT_ID"):
            self._check(**base, server_token_resource="https://fhir.example.com")
        with pytest.raises(ConfigurationError, match="FHIR_SERVER_CLIENT_SECRET"):
            self._check(**base, server_client_id="a", server_token_resource="https://fhir.example.com")

    def test_complete_configuration_passes(self):
        self._check(
            server_auth_mode="token_exchange",
            mcp_idp_issuer="https://idp.example.com",
            mcp_auth_mode="resource_server",
            server_client_id="a",
            server_client_secret="b",
            server_token_resource="https://fhir.example.com",
        )
