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

"""System tokens for the FHIR server: client secret and private_key_jwt flows."""

import base64
import datetime
import ipaddress
import json
import os
import ssl
import threading
import time

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Tuple
from unittest.mock import patch
from urllib.parse import parse_qs

import jwt
import pytest

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from fhir_mcp_server.backend_auth import BackendAuthError, SystemTokenProvider, fhir_tls
from fhir_mcp_server.oauth.types import ConfigurationError, ServerConfigs


class TokenEndpointStub:
    """Answers SMART discovery and the token endpoint, recording token requests."""

    def __init__(self, status: int = 200, expires_in: int = 300, tls: Tuple[str, str] | None = None) -> None:
        self.requests: List[Tuple[Dict[str, str], str | None]] = []
        self.status = status
        self.expires_in = expires_in
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path.endswith("/.well-known/smart-configuration"):
                    self._send(200, {"token_endpoint": f"{stub.base_url}/oauth2/token"})
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
                        "access_token": f"system-token-{len(stub.requests)}",
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
        if tls:  # HTTPS with a self-signed certificate (cert file, key file)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(*tls)
            self._httpd.socket = context.wrap_socket(self._httpd.socket, server_side=True)
            self.base_url = f"https://localhost:{self._httpd.server_address[1]}"
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture
def token_endpoint():
    stub = TokenEndpointStub()
    try:
        yield stub
    finally:
        stub.stop()


@pytest.fixture
def rsa_key(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path / "backend-key.pem"
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return key, str(path)


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


class TestClientCredentials:
    async def test_uses_client_secret_and_discovered_endpoint(self, token_endpoint):
        configs = make_configs(
            server_base_url=f"{token_endpoint.base_url}/fhir/r4",
            server_auth_mode="client_credentials",
            server_client_id="mcp-server",
            server_client_secret="s3cret",
            server_scopes="system/Patient.rs system/Observation.rs",
            server_token_resource="http://localhost:9090/fhir/r4",
        )

        token = await SystemTokenProvider(configs).get_token()

        assert token == "system-token-1"
        form, authorization = token_endpoint.requests[0]
        assert form == {
            "grant_type": "client_credentials",
            "scope": "system/Patient.rs system/Observation.rs",
            "resource": "http://localhost:9090/fhir/r4",
        }
        assert authorization == "Basic " + base64.b64encode(b"mcp-server:s3cret").decode()

    async def test_secret_with_special_characters_is_form_encoded(self, token_endpoint):
        configs = make_configs(
            server_token_url=f"{token_endpoint.base_url}/oauth2/token",
            server_auth_mode="client_credentials",
            server_client_id="mcp server",
            server_client_secret="a+b/c=",
        )

        await SystemTokenProvider(configs).get_token()

        # RFC 6749 section 2.3.1: form-urlencode the ID and secret, then Base64.
        _, authorization = token_endpoint.requests[0]
        assert authorization == "Basic " + base64.b64encode(b"mcp+server:a%2Bb%2Fc%3D").decode()

    async def test_reuses_token_until_shortly_before_expiry(self, token_endpoint):
        configs = make_configs(
            server_token_url=f"{token_endpoint.base_url}/oauth2/token",
            server_auth_mode="client_credentials",
            server_client_id="mcp-server",
            server_client_secret="s3cret",
        )
        provider = SystemTokenProvider(configs)

        assert await provider.get_token() == "system-token-1"
        assert await provider.get_token() == "system-token-1"
        assert len(token_endpoint.requests) == 1

        with patch("fhir_mcp_server.backend_auth.time.time", return_value=time.time() + 250):
            assert await provider.get_token() == "system-token-2"

    async def test_refused_request_raises_without_echoing_secrets(self):
        stub = TokenEndpointStub(status=401)
        try:
            configs = make_configs(
                server_token_url=f"{stub.base_url}/oauth2/token",
                server_auth_mode="client_credentials",
                server_client_id="mcp-server",
                server_client_secret="s3cret",
            )
            with pytest.raises(BackendAuthError) as error:
                await SystemTokenProvider(configs).get_token()
        finally:
            stub.stop()

        assert "HTTP 401" in str(error.value)
        assert "invalid_client" in str(error.value)
        assert "s3cret" not in str(error.value)


class TestPrivateKeyJwt:
    async def test_sends_signed_client_assertion(self, token_endpoint, rsa_key):
        key, key_file = rsa_key
        endpoint = f"{token_endpoint.base_url}/oauth2/token"
        configs = make_configs(
            server_token_url=endpoint,
            server_auth_mode="private_key_jwt",
            server_client_id="epic-backend-client",
            server_private_key_file=key_file,
            server_private_key_id="key-1",
            server_scopes="system/Patient.read",
        )

        assert await SystemTokenProvider(configs).get_token() == "system-token-1"

        form, authorization = token_endpoint.requests[0]
        assert authorization is None
        assert form["grant_type"] == "client_credentials"
        assert form["scope"] == "system/Patient.read"
        assert form["client_assertion_type"] == "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"

        assertion = form["client_assertion"]
        header = jwt.get_unverified_header(assertion)
        assert header["alg"] == "RS384"
        assert header["kid"] == "key-1"
        assert header["typ"] == "JWT"
        claims = jwt.decode(assertion, key.public_key(), algorithms=["RS384"], audience=endpoint)
        assert claims["iss"] == claims["sub"] == "epic-backend-client"
        assert claims["exp"] - claims["iat"] <= 300
        assert claims["jti"]

    async def test_each_assertion_has_a_new_jti(self, token_endpoint, rsa_key):
        _, key_file = rsa_key
        configs = make_configs(
            server_token_url=f"{token_endpoint.base_url}/oauth2/token",
            server_auth_mode="private_key_jwt",
            server_client_id="epic-backend-client",
            server_private_key_file=key_file,
        )
        provider = SystemTokenProvider(configs)
        endpoint = configs.server_token_url

        first = jwt.decode(provider._client_assertion(endpoint), options={"verify_signature": False})
        second = jwt.decode(provider._client_assertion(endpoint), options={"verify_signature": False})

        assert first["jti"] != second["jti"]


class TestSelfSignedTokenEndpoint:
    """FHIR_SERVER_CA_CERT: a token endpoint with a self-signed certificate (e.g. a local IdP)."""

    def _configs(self, stub: TokenEndpointStub, **values: Any) -> ServerConfigs:
        return make_configs(
            server_base_url="http://localhost:8080/fhir/r4",
            server_auth_mode="client_credentials",
            server_token_url=f"{stub.base_url}/oauth2/token",
            server_client_id="mcp-server",
            server_client_secret="s3cret",
            **values,
        )

    async def test_untrusted_without_the_setting(self, self_signed_cert):
        stub = TokenEndpointStub(tls=self_signed_cert)
        try:
            with pytest.raises(Exception):
                await SystemTokenProvider(self._configs(stub)).get_token()
        finally:
            stub.stop()
        assert stub.requests == []

    async def test_trusted_with_the_setting(self, self_signed_cert):
        stub = TokenEndpointStub(tls=self_signed_cert)
        try:
            token = await SystemTokenProvider(self._configs(stub, server_ca_cert=self_signed_cert[0])).get_token()
        finally:
            stub.stop()
        assert token == "system-token-1"

    def test_adds_to_the_system_trust_store(self, self_signed_cert):
        assert fhir_tls(make_configs()) is True
        system = ssl.create_default_context().cert_store_stats()["x509_ca"]
        context = fhir_tls(make_configs(server_ca_cert=self_signed_cert[0]))
        assert isinstance(context, ssl.SSLContext)
        assert context.cert_store_stats()["x509_ca"] == system + 1


class TestConfiguration:
    def _check(self, **values: Any) -> None:
        make_configs(mcp_auth_mode="disabled", **values).validate_for_startup()

    def test_client_credentials_requires_id_and_secret(self):
        with pytest.raises(ConfigurationError, match="FHIR_SERVER_CLIENT_ID"):
            self._check(server_auth_mode="client_credentials")
        with pytest.raises(ConfigurationError, match="FHIR_SERVER_CLIENT_SECRET"):
            self._check(server_auth_mode="client_credentials", server_client_id="mcp-server")

    def test_private_key_jwt_requires_key_file(self):
        with pytest.raises(ConfigurationError, match="FHIR_SERVER_PRIVATE_KEY_FILE"):
            self._check(server_auth_mode="private_key_jwt", server_client_id="epic-backend-client")

    def test_complete_configurations_pass(self):
        self._check(server_auth_mode="client_credentials", server_client_id="a", server_client_secret="b")
        self._check(server_auth_mode="private_key_jwt", server_client_id="a", server_private_key_file="k.pem")

