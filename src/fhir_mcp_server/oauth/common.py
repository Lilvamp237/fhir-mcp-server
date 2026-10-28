# Copyright (c) 2025, WSO2 LLC. (https://www.wso2.com/) All Rights Reserved.

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
import time
import secrets
import string
import hashlib
import base64

from typing import Dict

from fhir_mcp_server.oauth.types import OAuthMetadata, OAuthToken
from mcp.shared._httpx_utils import create_mcp_http_client

logger = logging.getLogger(__name__)


async def discover_oauth_metadata(
    metadata_url: str, headers: Dict[str, str] = {"Accept": "application/fhir+json"}
) -> OAuthMetadata | None:
    """
    Discover OAuth metadata from server's well-known endpoint.
    """

    async with create_mcp_http_client() as client:
        try:
            response = await client.get(url=metadata_url, headers=headers)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            metadata_json = response.json()
            logger.debug(f"OAuth metadata discovered: {metadata_json}")
            return OAuthMetadata.model_validate(metadata_json)
        except Exception as ex:
            logger.exception("Failed to discover OAuth metadata. Caused by, ", ex)
        return None


def is_token_expired(token) -> bool:
    """Return True if the token is missing or expired."""
    if not token or not getattr(token, "expires_at", None):
        return True
    return time.time() > token.expires_at


def get_endpoint(metadata, endpoint: str) -> str:
    """Get an endpoint URL from OAuthMetadata, raise if missing."""
    value = getattr(metadata, endpoint, None)
    if not value:
        raise Exception(f"{endpoint} not found in metadata")
    return str(value)


def generate_code_verifier(length: int = 128) -> str:
    """Generate a cryptographically random code verifier for PKCE (RFC 7636)."""
    if not (43 <= length <= 128):
        raise ValueError("Code verifier length must be between 43 and 128.")
    return "".join(
        secrets.choice(string.ascii_letters + string.digits + "-._~")
        for _ in range(length)
    )


def generate_code_challenge(code_verifier: str) -> str:
    """Generate a code challenge from a code verifier using SHA256 (RFC 7636)."""
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii")
    return challenge.rstrip("=")


async def perform_token_flow(
    url: str,
    data: Dict[str, str],
    headers: Dict[str, str] = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    },
    timeout: float = 30.0,
) -> OAuthToken:
    try:
        async with create_mcp_http_client() as client:
            response = await client.post(
                url=url,
                data=data,
                headers=headers,
                timeout=timeout,
            )
            logger.debug(
                f"Token endpoint response status: {response.status_code}"
            )

            if response.status_code != 200:
                logger.error(
                    f"Token call failed with status: {response.status_code}: {response.text}"
                )
                raise ValueError(f"Token endpoint call failed")

            # Parse token response
            token_response: OAuthToken = OAuthToken.model_validate(response.json())

            # Calculate token expiry
            if not token_response.expires_at:
                if token_response.expires_in:
                    token_response.expires_at = time.time() + token_response.expires_in
                else:
                    token_response.expires_at = time.time() + 3600

            return token_response

    except Exception as ex:
        logger.exception(
            "Unable to invoke the token endpoint. Caused by, ", exc_info=ex
        )
        raise ValueError("Token endpoint call failed")
