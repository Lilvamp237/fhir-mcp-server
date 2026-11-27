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

"""The FHIR TLS setting and client authentication used by the FHIR sign-in."""

import base64
import datetime
import ipaddress
import os
import ssl

from typing import Any
from unittest.mock import patch

import pytest

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from fhir_mcp_server.backend_auth import client_secret_basic, fhir_tls
from fhir_mcp_server.oauth.types import ServerConfigs


@pytest.fixture
def self_signed_cert(tmp_path) -> str:
    """A self-signed CA certificate for localhost, like a local IdP's or gateway's."""
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
    cert_file = tmp_path / "ca.cert"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return str(cert_file)


def make_configs(**values: Any) -> ServerConfigs:
    with patch.dict(os.environ, {}, clear=True):
        return ServerConfigs(_env_file=None, **values)


class TestFhirTls:
    """FHIR_SERVER_CA_CERT: a FHIR server or token endpoint with a self-signed certificate."""

    def test_adds_to_the_system_trust_store(self, self_signed_cert):
        assert fhir_tls(make_configs()) is True
        system = ssl.create_default_context().cert_store_stats()["x509_ca"]
        context = fhir_tls(make_configs(server_ca_cert=self_signed_cert))
        assert isinstance(context, ssl.SSLContext)
        assert context.cert_store_stats()["x509_ca"] == system + 1


def test_client_secret_basic_encodes_special_characters():
    header = client_secret_basic("app id", "s+e/c=r:et")["Authorization"]
    decoded = base64.b64decode(header.removeprefix("Basic ")).decode()
    assert decoded == "app+id:s%2Be%2Fc%3Dr%3Aet"
