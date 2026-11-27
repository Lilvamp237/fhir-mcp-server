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

"""A minimal identity provider stand-in: discovery and JWKS.

Tokens are real RS256 JWTs signed with a key generated per test run, shaped
like ThunderID's access tokens (`typ: at+jwt`, `scope`, `client_id`,
`fhirUser`).
"""

import json
import logging
import threading
import time
import uuid

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict

import jwt

from cryptography.hazmat.primitives.asymmetric import rsa

logger: logging.Logger = logging.getLogger(__name__)



class IdpStub:
    def __init__(self) -> None:
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "test-key"
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def _json(self, status: int, payload: Dict[str, Any]) -> None:
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                if self.path == "/.well-known/oauth-authorization-server":
                    self._json(200, stub.metadata())
                elif self.path == "/oauth2/jwks":
                    self._json(200, stub.jwks())
                else:
                    self._json(404, {"error": "not_found"})

            def log_message(self, format: str, *args: Any) -> None:
                logger.debug("[IDP STUB] " + format, *args)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.issuer = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def metadata(self) -> Dict[str, Any]:
        return {
            "issuer": self.issuer,
            "authorization_endpoint": f"{self.issuer}/oauth2/authorize",
            "token_endpoint": f"{self.issuer}/oauth2/token",
            "jwks_uri": f"{self.issuer}/oauth2/jwks",
            "code_challenge_methods_supported": ["S256"],
            "authorization_response_iss_parameter_supported": True,
        }

    def jwks(self) -> Dict[str, Any]:
        public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self._key.public_key()))
        return {"keys": [{**public, "kid": self.kid, "use": "sig", "alg": "RS256"}]}

    def token(self, audience: str, scope: str, **claims: Any) -> str:
        """Sign an access token; keyword claims override the defaults. The default user
        is a practitioner, who may see every patient; pass `fhirUser` to test others."""
        now = int(time.time())
        payload = {
            "iss": self.issuer,
            "aud": audience,
            "sub": "user-1",
            "fhirUser": "Practitioner/practitioner-1",
            "client_id": "mcp-client",
            "scope": scope,
            "iat": now,
            "nbf": now,
            "exp": now + 300,
            "jti": str(uuid.uuid4()),
            **claims,
        }
        payload = {k: v for k, v in payload.items() if v is not None}
        return jwt.encode(payload, self._key, algorithm="RS256", headers={"kid": self.kid, "typ": "at+jwt"})
