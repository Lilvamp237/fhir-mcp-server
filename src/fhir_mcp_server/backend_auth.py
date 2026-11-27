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

"""Helpers for calls from the MCP server to the FHIR server: TLS verification and
client authentication, used by the per-agent FHIR sign-in (fhir_mcp_server.fhir_connect).
"""

import base64
import ssl

from typing import Dict
from urllib.parse import quote_plus

from fhir_mcp_server.oauth.types import ServerConfigs


class BackendAuthError(RuntimeError):
    """No FHIR credential could be obtained for this request."""


def fhir_tls(configs: ServerConfigs) -> ssl.SSLContext | bool:
    """TLS verification for calls to the FHIR server and its token endpoints.

    Without FHIR_SERVER_CA_CERT this is the client default. With it, the system's
    trusted CAs plus that certificate, so public servers such as Epic still verify.
    """
    if not configs.server_ca_cert:
        return True
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=configs.server_ca_cert)
    return context


def client_secret_basic(client_id: str, client_secret: str) -> Dict[str, str]:
    """The `Authorization` header for client_secret_basic (RFC 6749 section 2.3.1).

    The ID and secret are form-urlencoded before Base64, so secrets containing
    characters such as `+`, `/` or `=` reach the server unchanged.
    """
    credentials = f"{quote_plus(client_id)}:{quote_plus(client_secret)}"
    return {"Authorization": "Basic " + base64.b64encode(credentials.encode()).decode()}
