# Copyright (c) 2026, Choreo Labs LLC. (https://choreolabs.com)

# Choreo Labs licenses this file to you under the Apache License,
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

"""Conformance with the MCP 2026-07-28 authorization rules the server owns.

The server is an OAuth resource server; tokens come from the IdP stub:
- 401 with a `resource_metadata` challenge when the token is missing or invalid
- Protected Resource Metadata (RFC 9728) naming the IdP
- only tokens issued for this server's /mcp endpoint are accepted
- 403 `insufficient_scope` naming the scope a tool needs
- the MCP token is never passed through to the FHIR server
"""

import json
import time
import urllib.error
import urllib.request

from typing import Any, Dict, Tuple

import jwt
import pytest

from cryptography.hazmat.primitives.asymmetric import rsa

from tests.integration.stubs.fhir_stub import PATIENT
from tests.integration.mcp_client import LEGACY, connect

READ = "fhir-read"
PRACTITIONER_SCOPES = "fhir-read fhir-write fhir-delete"


def http(
    method: str, url: str, body: Dict[str, Any] | None = None, token: str | None = None
) -> Tuple[int, Dict[str, str], str]:
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, {k.lower(): v for k, v in error.headers.items()}, error.read().decode()


def call_tool_body(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}


def metadata_url(mcp_url: str) -> str:
    base, path = mcp_url.rsplit("/mcp", 1)
    return f"{base}/.well-known/oauth-protected-resource/mcp{path}"


# Discovery and challenges


def test_request_without_token_gets_401_challenge(serve_with_idp, fhir_stub) -> None:
    with serve_with_idp() as url:
        status, headers, _ = http("POST", url, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

    assert status == 401
    challenge = headers["www-authenticate"]
    assert challenge.startswith("Bearer ")
    assert f'resource_metadata="{metadata_url(url)}"' in challenge
    assert 'scope="fhir-read fhir-write fhir-delete"' in challenge
    assert fhir_stub.requests == []


def test_protected_resource_metadata_names_the_idp(serve_with_idp, idp_stub) -> None:
    with serve_with_idp() as url:
        status, _, body = http("GET", metadata_url(url))

    assert status == 200
    metadata = json.loads(body)
    assert metadata["resource"] == url
    assert metadata["authorization_servers"] == [idp_stub.issuer]
    assert metadata["scopes_supported"] == PRACTITIONER_SCOPES.split()
    assert "offline_access" not in metadata["scopes_supported"]


# Token validation (JWKS)


def _foreign_key_token(idp_stub, audience: str) -> str:
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    claims = jwt.decode(idp_stub.token(audience, READ), options={"verify_signature": False})
    return jwt.encode(claims, other_key, algorithm="RS256", headers={"kid": idp_stub.kid})


@pytest.mark.parametrize(
    "make_token",
    [
        pytest.param(lambda idp, url: idp.token("http://other-server.example/mcp", READ), id="wrong-audience"),
        pytest.param(lambda idp, url: idp.token(url, READ, iss="https://other-idp.example"), id="wrong-issuer"),
        pytest.param(lambda idp, url: idp.token(url, READ, exp=int(time.time()) - 3600), id="expired"),
        pytest.param(lambda idp, url: idp.token(url, READ, nbf=int(time.time()) + 3600), id="not-yet-valid"),
        pytest.param(_foreign_key_token, id="foreign-signing-key"),
        pytest.param(lambda idp, url: "not-a-jwt", id="malformed"),
        pytest.param(
            lambda idp, url: jwt.encode({"iss": idp.issuer, "aud": url, "scope": READ, "exp": int(time.time()) + 300},
                                        key=None, algorithm="none"),
            id="unsigned-alg-none",
        ),
    ],
)
def test_invalid_token_is_rejected(serve_with_idp, idp_stub, fhir_stub, make_token) -> None:
    with serve_with_idp() as url:
        token = make_token(idp_stub, url)
        status, headers, _ = http("POST", url, call_tool_body("read", {"type": "Patient", "id": "123"}), token)

    assert status == 401
    assert 'error="invalid_token"' in headers["www-authenticate"]
    assert fhir_stub.requests == []


async def test_valid_token_reaches_tools_without_token_passthrough(
    serve_with_idp, idp_stub, fhir_stub, protocol_mode
) -> None:
    with serve_with_idp() as url:
        token = idp_stub.token(url, READ)
        async with connect(url, protocol_mode, token=token) as session:
            result = await session.call_tool("read", {"type": "Patient", "id": "123"})

    assert not result.is_error
    assert result.text == PATIENT
    assert fhir_stub.calls() == [("GET", "/Patient/123")]
    # The MCP token is never forwarded to the FHIR server.
    assert [r.authorization for r in fhir_stub.requests] == [None]


# Scopes


def test_tool_without_its_scope_gets_403_challenge(serve_with_idp, idp_stub, fhir_stub) -> None:
    with serve_with_idp() as url:
        token = idp_stub.token(url, READ)  # a patient: read only
        status, headers, body = http(
            "POST", url, call_tool_body("create", {"type": "Patient", "payload": {"gender": "male"}}), token
        )

    assert status == 403
    challenge = headers["www-authenticate"]
    assert 'error="insufficient_scope"' in challenge
    assert 'scope="fhir-write"' in challenge
    assert f'resource_metadata="{metadata_url(url)}"' in challenge
    assert json.loads(body)["error"] == "insufficient_scope"
    assert fhir_stub.requests == []


def test_delete_needs_the_delete_scope(serve_with_idp, idp_stub, fhir_stub) -> None:
    with serve_with_idp() as url:
        token = idp_stub.token(url, "fhir-read fhir-write")
        status, headers, _ = http("POST", url, call_tool_body("delete", {"type": "Patient", "id": "123"}), token)

    assert status == 403
    assert 'scope="fhir-delete"' in headers["www-authenticate"]
    assert fhir_stub.requests == []


def test_token_without_read_scope_is_refused(serve_with_idp, idp_stub, fhir_stub) -> None:
    with serve_with_idp() as url:
        token = idp_stub.token(url, "openid")
        status, headers, _ = http("POST", url, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, token)

    assert status == 403
    assert 'error="insufficient_scope"' in headers["www-authenticate"]
    assert fhir_stub.requests == []


async def test_practitioner_scopes_allow_writes(serve_with_idp, idp_stub, fhir_stub) -> None:
    with serve_with_idp() as url:
        token = idp_stub.token(url, PRACTITIONER_SCOPES)
        async with connect(url, LEGACY, token=token) as session:
            result = await session.call_tool("create", {"type": "Patient", "payload": {"gender": "female"}})

    assert not result.is_error
    assert result.text["id"] == "new-1"
    assert fhir_stub.calls() == [("POST", "/Patient")]


async def test_custom_scope_names_are_used(serve_with_idp, idp_stub, fhir_stub) -> None:
    with serve_with_idp(mcp_read_scope="user/*.rs") as url:
        allowed = idp_stub.token(url, "user/*.rs")
        refused = idp_stub.token(url, READ)
        async with connect(url, LEGACY, token=allowed) as session:
            result = await session.call_tool("read", {"type": "Patient", "id": "123"})
        status, headers, _ = http("POST", url, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, refused)

    assert result.text["id"] == "123"
    assert status == 403
    assert "user/*.rs" in headers["www-authenticate"]


# User identity


async def test_get_user_reads_fhir_user_claim(serve_with_idp, idp_stub, fhir_stub, protocol_mode) -> None:
    with serve_with_idp() as url:
        token = idp_stub.token(url, READ, fhirUser="Patient/123")
        async with connect(url, protocol_mode, token=token) as session:
            result = await session.call_tool("get_user", {})

    assert result.text == {key: PATIENT[key] for key in ("id", "resourceType", "name", "gender", "birthDate")}
    assert fhir_stub.calls() == [("GET", "/Patient/123")]


async def test_get_user_without_fhir_user_claim_returns_empty(serve_with_idp, idp_stub, fhir_stub) -> None:
    with serve_with_idp() as url:
        token = idp_stub.token(url, READ, fhirUser=None)
        async with connect(url, LEGACY, token=token) as session:
            result = await session.call_tool("get_user", {})

    assert result.text == {}
    assert fhir_stub.requests == []
