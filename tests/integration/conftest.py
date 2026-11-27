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

"""Fixtures for the integration tests (tools, protocol versions, authorization, agent linking).

The server is built exactly as `main()` builds it, with the FHIR base URL
pointed at an in-process FHIR stub and, for authorization tests, the IdP
pointed at an in-process IdP stub, so every test runs offline.
"""

import socket
import threading
import time

from contextlib import contextmanager
from typing import Any, Callable, Iterator

import pytest
import uvicorn

from fhir_mcp_server import server as server_module
from tests.integration.stubs.fhir_stub import FhirStub
from tests.integration.stubs.idp_stub import IdpStub
from tests.integration.mcp_client import PROTOCOL_MODES


def build_server() -> Any:
    """Build the MCP server object the same way `main()` does."""
    mcp = server_module.configure_mcp_server()
    server_module.register_mcp_tools(mcp=mcp)
    return mcp


@pytest.fixture
def fhir_stub() -> Iterator[FhirStub]:
    stub = FhirStub()
    stub.start()
    try:
        yield stub
    finally:
        stub.stop()


@pytest.fixture
def idp_stub() -> Iterator[IdpStub]:
    stub = IdpStub()
    stub.start()
    try:
        yield stub
    finally:
        stub.stop()


def _point_at_stub(monkeypatch: pytest.MonkeyPatch, fhir_stub: FhirStub, port: int) -> None:
    configs = server_module.configs
    monkeypatch.setattr(configs, "server_base_url", fhir_stub.base_url)
    # Backend credentials from a developer's .env must not leak into the tests.
    for name, value in (
        ("server_client_id", ""),
        ("server_client_secret", ""),
        ("server_token_url", None),
        ("server_authorize_url", None),
        ("server_scopes", ""),
    ):
        monkeypatch.setattr(configs, name, value)
    monkeypatch.setattr(configs, "mcp_host", "127.0.0.1")
    monkeypatch.setattr(configs, "mcp_port", port)
    monkeypatch.setattr(configs, "mcp_server_url", None)
    monkeypatch.setattr(configs, "mcp_resource_url", None)
    monkeypatch.setattr(configs, "mcp_auth_mode", "disabled")


def _use_idp(monkeypatch: pytest.MonkeyPatch, idp: IdpStub) -> None:
    configs = server_module.configs
    monkeypatch.setattr(configs, "mcp_auth_mode", "resource_server")
    monkeypatch.setattr(configs, "mcp_idp_issuer", idp.issuer)
    monkeypatch.setattr(configs, "mcp_idp_ca_cert", None)
    monkeypatch.setattr(configs, "mcp_jwks_uri", None)
    for name, value in (("read", "fhir-read"), ("write", "fhir-write"), ("delete", "fhir-delete")):
        monkeypatch.setattr(configs, f"mcp_{name}_scope", value)


@pytest.fixture
def mcp_server(fhir_stub: FhirStub, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The server object for in-process tests, with authorization disabled."""
    _point_at_stub(monkeypatch, fhir_stub, _free_port())
    return build_server()


@pytest.fixture(params=PROTOCOL_MODES)
def protocol_mode(request: pytest.FixtureRequest) -> str:
    return request.param


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def serve_http(mcp: Any, port: int) -> Iterator[str]:
    """Serve an MCP server over real Streamable HTTP, as `main()` does; yields the /mcp URL."""
    config = uvicorn.Config(
        server_module.build_http_app(mcp), host="127.0.0.1", port=port, log_level="warning"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started:
        if time.time() > deadline or not thread.is_alive():
            raise RuntimeError("MCP HTTP server did not start")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
def mcp_http_url(fhir_stub: FhirStub, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """The server over HTTP with authorization disabled."""
    port = _free_port()
    _point_at_stub(monkeypatch, fhir_stub, port)
    with serve_http(build_server(), port) as url:
        yield url


@pytest.fixture
def serve_with_idp(
    fhir_stub: FhirStub, idp_stub: IdpStub, monkeypatch: pytest.MonkeyPatch
) -> Callable[..., Any]:
    """Start the server over HTTP in resource-server mode against the IdP stub.

    Call it inside a `with` block: `with serve_with_idp() as url: ...`.
    Extra keyword arguments are set on the server's configuration.
    """

    @contextmanager
    def serve(**settings: Any) -> Iterator[str]:
        port = _free_port()
        _point_at_stub(monkeypatch, fhir_stub, port)
        _use_idp(monkeypatch, idp_stub)
        for name, value in settings.items():
            monkeypatch.setattr(server_module.configs, name, value)
        with serve_http(build_server(), port) as url:
            yield url

    return serve
