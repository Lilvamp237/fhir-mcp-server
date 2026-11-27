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

"""A SMART App Launch authorization server stand-in (the EHR's own login, e.g. Epic).

`/authorize` signs the user in immediately and redirects back with a code;
`/token` checks PKCE and issues per-user access and refresh tokens.
"""

import base64
import hashlib
import json
import logging
import threading
import uuid

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List
from urllib.parse import parse_qs, urlencode

import jwt

logger: logging.Logger = logging.getLogger(__name__)

EHR_CLIENT = ("ehr-user-app", "ehr-secret")


class SmartAuthStub:
    def __init__(self, expires_in: int = 3600) -> None:
        self.expires_in = expires_in
        self.authorize_requests: List[Dict[str, str]] = []
        self.grants: List[str] = []
        self.client_auth: List[str] = []
        self.assertion_key: Any = None  # public key that verifies client assertions, if set
        self.patient: str | None = None  # SMART `patient` launch context, as a patient (MyChart) login returns
        self.fhir_user: str | None = None  # fhirUser in an ID token, returned when set and `openid` was asked for
        self.issued = 0
        self._codes: Dict[str, Dict[str, str]] = {}
        self._refresh_tokens: set[str] = set()
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
                if not self.path.startswith("/authorize?"):
                    self._json(404, {"error": "not_found"})
                    return
                query = {k: v[0] for k, v in parse_qs(self.path.split("?", 1)[1]).items()}
                stub.authorize_requests.append(query)
                code = uuid.uuid4().hex
                stub._codes[code] = query
                location = f"{query['redirect_uri']}?{urlencode({'code': code, 'state': query['state']})}"
                self.send_response(302)
                self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self) -> None:
                if self.path != "/token":
                    self._json(404, {"error": "not_found"})
                    return
                length = int(self.headers.get("Content-Length") or 0)
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
                expected = base64.b64encode(":".join(EHR_CLIENT).encode()).decode()
                if form.get("client_assertion") and stub.assertion_key is not None:
                    try:
                        claims = jwt.decode(
                            form["client_assertion"],
                            stub.assertion_key,
                            algorithms=["RS384"],
                            audience=f"{stub.base_url}/token",
                        )
                    except jwt.PyJWTError:
                        self._json(401, {"error": "invalid_client"})
                        return
                    if claims.get("iss") != EHR_CLIENT[0] or claims.get("sub") != EHR_CLIENT[0]:
                        self._json(401, {"error": "invalid_client"})
                        return
                    stub.client_auth.append("private_key_jwt")
                elif self.headers.get("Authorization") == f"Basic {expected}":
                    stub.client_auth.append("client_secret_basic")
                else:
                    self._json(401, {"error": "invalid_client"})
                    return
                stub.grants.append(form.get("grant_type", ""))
                if form.get("grant_type") == "authorization_code":
                    request = stub._codes.pop(form.get("code", ""), None)
                    challenge = (
                        base64.urlsafe_b64encode(hashlib.sha256(form.get("code_verifier", "").encode()).digest())
                        .rstrip(b"=")
                        .decode()
                    )
                    if request is None or request.get("code_challenge") != challenge:
                        self._json(400, {"error": "invalid_grant"})
                        return
                elif form.get("grant_type") == "refresh_token":
                    if form.get("refresh_token") not in stub._refresh_tokens:
                        self._json(400, {"error": "invalid_grant"})
                        return
                    stub._refresh_tokens.discard(form["refresh_token"])
                else:
                    self._json(400, {"error": "unsupported_grant_type"})
                    return
                stub.issued += 1
                refresh = f"ehr-refresh-{stub.issued}"
                stub._refresh_tokens.add(refresh)
                response: Dict[str, Any] = {
                    "access_token": f"ehr-user-token-{stub.issued}",
                    "token_type": "Bearer",
                    "expires_in": stub.expires_in,
                    "refresh_token": refresh,
                    "scope": "openid fhirUser user/Patient.read",
                }
                if stub.patient and form.get("grant_type") == "authorization_code":
                    response["patient"] = stub.patient
                if stub.fhir_user and form.get("grant_type") == "authorization_code":
                    response["id_token"] = jwt.encode(
                        {"iss": stub.base_url, "aud": EHR_CLIENT[0], "sub": "ehr-user", "fhirUser": stub.fhir_user},
                        "id-token-signing-key-for-tests-only-32b",
                        algorithm="HS256",
                    )
                self._json(200, response)

            def log_message(self, format: str, *args: Any) -> None:
                logger.debug("[SMART STUB] " + format, *args)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
