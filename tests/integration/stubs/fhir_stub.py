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

"""A scripted FHIR server stand-in that records every request it receives."""

import json
import logging
import threading

from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Tuple
from urllib.parse import parse_qs, urlsplit

logger: logging.Logger = logging.getLogger(__name__)

CAPABILITY_STATEMENT: Dict[str, Any] = {
    "resourceType": "CapabilityStatement",
    "status": "active",
    "fhirVersion": "4.0.1",
    "rest": [
        {
            "mode": "server",
            "resource": [
                {
                    "type": "Patient",
                    "interaction": [{"code": "read"}, {"code": "search-type"}],
                    "searchParam": [
                        {
                            "name": "family",
                            "type": "string",
                            "documentation": "A portion of the family name",
                        },
                        {"name": "_id", "type": "token"},
                    ],
                    "operation": [
                        {
                            "name": "everything",
                            "definition": "http://hl7.org/fhir/OperationDefinition/Patient-everything",
                            "documentation": "Fetch the patient's record",
                        }
                    ],
                    "searchInclude": ["Patient:organization"],
                    "searchRevInclude": ["Observation:subject"],
                }
            ],
        }
    ],
}

PATIENT: Dict[str, Any] = {
    "resourceType": "Patient",
    "id": "123",
    "gender": "male",
    "birthDate": "1980-04-12",
    "name": [{"family": "Smith", "given": ["John"]}],
}

SEARCH_BUNDLE: Dict[str, Any] = {
    "resourceType": "Bundle",
    "type": "searchset",
    "total": 2,
    "entry": [
        {"resource": PATIENT},
        {
            "resource": {
                "resourceType": "Patient",
                "id": "456",
                "gender": "female",
                "name": [{"family": "Smith", "given": ["Jane"]}],
            }
        },
    ],
}

EVERYTHING_BUNDLE: Dict[str, Any] = {
    "resourceType": "Bundle",
    "type": "searchset",
    "entry": [
        {"resource": PATIENT},
        {
            "resource": {
                "resourceType": "Observation",
                "id": "obs-1",
                "status": "final",
                "subject": {"reference": "Patient/123"},
            }
        },
    ],
}


def operation_outcome(code: str, diagnostics: str) -> Dict[str, Any]:
    return {
        "resourceType": "OperationOutcome",
        "issue": [{"severity": "error", "code": code, "diagnostics": diagnostics}],
    }


@dataclass
class RecordedRequest:
    method: str
    path: str
    query: Dict[str, List[str]]
    body: Any
    authorization: str | None


@dataclass
class FhirStub:
    """In-process FHIR server with fixed responses.

    Responses are keyed by (method, path). Unknown routes return 404 with an
    OperationOutcome, which matches how a real FHIR server answers.
    """

    routes: Dict[Tuple[str, str], Tuple[int, Any]] = field(default_factory=dict)
    requests: List[RecordedRequest] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.routes = {
            ("GET", "/metadata"): (200, CAPABILITY_STATEMENT),
            ("GET", "/Patient"): (200, SEARCH_BUNDLE),
            ("GET", "/Patient/123"): (200, PATIENT),
            ("GET", "/Patient/123/$everything"): (200, EVERYTHING_BUNDLE),
            ("POST", "/Patient"): (201, None),  # echo the request body
            ("PUT", "/Patient/123"): (200, None),  # echo the request body
            ("DELETE", "/Patient/123"): (204, None),
            ("POST", "/Observation"): (
                400,
                operation_outcome("required", "Observation.status is required"),
            ),
            **self.routes,
        }
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self) -> None:
                url = urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw) if raw else None
                stub.requests.append(
                    RecordedRequest(
                        method=self.command,
                        path=url.path,
                        query=parse_qs(url.query),
                        body=body,
                        authorization=self.headers.get("Authorization"),
                    )
                )
                # fhirpy adds a trailing slash to instance URLs (/Patient/123/).
                status, payload = stub.routes.get(
                    (self.command, url.path.rstrip("/") or "/"),
                    (404, operation_outcome("not-found", f"{url.path} not found")),
                )
                if payload is None and body is not None:
                    payload = {
                        **body,
                        "id": body.get("id", "new-1"),
                        "meta": {"versionId": "1", "lastUpdated": "2026-01-01T00:00:00Z"},
                    }
                data = json.dumps(payload).encode() if payload is not None else b""
                self.send_response(status)
                if data:
                    self.send_header("Content-Type", "application/fhir+json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if data:
                    self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_DELETE = _handle

            def log_message(self, format: str, *args: Any) -> None:
                logger.debug("[FHIR STUB] " + format, *args)

        # 127.0.0.1 rather than "localhost" avoids an IPv6 mismatch on Windows.
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def calls(self) -> List[Tuple[str, str]]:
        """(method, path without trailing slash) of every request except
        CapabilityStatement fetches."""
        return [
            (r.method, r.path.rstrip("/")) for r in self.requests if r.path != "/metadata"
        ]
