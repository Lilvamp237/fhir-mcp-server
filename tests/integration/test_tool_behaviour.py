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

"""What each tool sends to the FHIR server and what it returns to the client.

These tests were written against SDK 1.27.1 and hold on SDK 2.x, in both the
legacy and the 2026-07-28 protocol.
"""

from typing import Any, Dict

from tests.integration.stubs.fhir_stub import (
    EVERYTHING_BUNDLE,
    PATIENT,
    SEARCH_BUNDLE,
    FhirStub,
)
from tests.integration.mcp_client import ToolCall, connect


def outcome(code: str, diagnostics: str, severity: str = "error") -> Dict[str, Any]:
    return {
        "resourceType": "OperationOutcome",
        "issue": [{"severity": severity, "code": code, "diagnostics": diagnostics}],
    }


async def call(mcp_server: Any, mode: str, tool: str, arguments: Dict[str, Any]) -> Any:
    """Call a tool and return its result, checking structured and text content agree."""
    async with connect(mcp_server, mode) as session:
        result: ToolCall = await session.call_tool(tool, arguments)
    assert not result.is_error, f"{tool} returned a protocol-level error: {result.text}"
    assert result.structured == {"result": result.text}
    return result.text


# get_capabilities


async def test_get_capabilities_returns_trimmed_capabilities(mcp_server, fhir_stub, protocol_mode):
    result = await call(mcp_server, protocol_mode, "get_capabilities", {"type": "Patient"})

    assert result == {
        "type": "Patient",
        "searchParam": [
            {"name": "family", "documentation": "A portion of the family name"},
            {"name": "_id", "documentation": None},
        ],
        "operation": [{"name": "everything", "documentation": "Fetch the patient's record"}],
        "interaction": [{"code": "read"}, {"code": "search-type"}],
        "searchInclude": ["Patient:organization"],
        "searchRevInclude": ["Observation:subject"],
    }
    assert [(r.method, r.path) for r in fhir_stub.requests] == [("GET", "/metadata")]
    assert fhir_stub.requests[0].query == {"_format": ["json"]}


async def test_get_capabilities_unsupported_type(mcp_server, protocol_mode):
    result = await call(mcp_server, protocol_mode, "get_capabilities", {"type": "Observation"})

    assert result == outcome(
        "not-supported",
        "The interaction, operation, resource or profile Observation is not supported.",
    )


async def test_get_capabilities_rejects_invalid_type(mcp_server, fhir_stub, protocol_mode):
    result = await call(mcp_server, protocol_mode, "get_capabilities", {"type": "Pat/ient"})

    assert result["issue"][0]["code"] == "invalid"
    assert fhir_stub.requests == []


# search


async def test_search_returns_bundle(mcp_server, fhir_stub, protocol_mode):
    result = await call(
        mcp_server, protocol_mode, "search", {"type": "Patient", "searchParam": {"family": "Smith"}}
    )

    assert result == SEARCH_BUNDLE
    assert fhir_stub.calls() == [("GET", "/Patient")]
    assert fhir_stub.requests[0].query == {"family": ["Smith"]}


async def test_search_applies_response_filter(mcp_server, protocol_mode):
    result = await call(
        mcp_server,
        protocol_mode,
        "search",
        {
            "type": "Patient",
            "searchParam": {"family": "Smith"},
            "response_filter_fhirpaths": ["Patient.gender"],
        },
    )

    # Filtering also removes the `resource` wrapper from each Bundle entry.
    assert result == {
        "resourceType": "Bundle",
        "type": "searchset",
        "total": 2,
        "entry": [
            {"resourceType": "Patient", "id": "123", "gender": "male"},
            {"resourceType": "Patient", "id": "456", "gender": "female"},
        ],
    }


async def test_search_requires_type(mcp_server, fhir_stub, protocol_mode):
    result = await call(mcp_server, protocol_mode, "search", {"type": "", "searchParam": {}})

    assert result == outcome("required", "A required element type is missing.")
    assert fhir_stub.requests == []


# read


async def test_read_returns_resource(mcp_server, fhir_stub, protocol_mode):
    result = await call(mcp_server, protocol_mode, "read", {"type": "Patient", "id": "123"})

    assert result == PATIENT
    assert fhir_stub.calls() == [("GET", "/Patient/123")]


async def test_read_with_operation_flattens_bundle(mcp_server, fhir_stub, protocol_mode):
    result = await call(
        mcp_server, protocol_mode, "read", {"type": "Patient", "id": "123", "operation": "$everything"}
    )

    assert result == {
        "resourceType": "Bundle",
        "entry": [entry["resource"] for entry in EVERYTHING_BUNDLE["entry"]],
    }
    assert fhir_stub.calls() == [("GET", "/Patient/123/$everything")]


async def test_read_missing_resource(mcp_server, protocol_mode):
    result = await call(mcp_server, protocol_mode, "read", {"type": "Patient", "id": "999"})

    assert result == outcome("not-found", "The resource of type 'Patient' with id '999' was not found.")


async def test_read_rejects_path_traversal_id(mcp_server, fhir_stub, protocol_mode):
    result = await call(mcp_server, protocol_mode, "read", {"type": "Patient", "id": "../Observation"})

    assert result["issue"][0]["code"] == "invalid"
    assert fhir_stub.requests == []


# create


async def test_create_defaults_resource_type_and_returns_stored_resource(
    mcp_server, fhir_stub, protocol_mode
):
    payload = {"gender": "female", "name": [{"family": "Doe"}]}
    result = await call(mcp_server, protocol_mode, "create", {"type": "Patient", "payload": payload})

    assert fhir_stub.calls() == [("POST", "/Patient")]
    assert fhir_stub.requests[-1].body == {"resourceType": "Patient", **payload}
    assert result["resourceType"] == "Patient"
    assert result["id"] == "new-1"
    assert result["gender"] == "female"


async def test_create_fhir_server_error_surfaces_as_tool_error(
    mcp_server: Any, fhir_stub: FhirStub, protocol_mode
):
    # Existing defect, recorded as-is: on a FHIR error the tool returns the
    # OperationOutcome `issue` list, which does not match its dict output
    # schema, so the client gets a validation error instead of the issues.
    async with connect(mcp_server, protocol_mode) as session:
        result = await session.call_tool(
            "create", {"type": "Observation", "payload": {"code": {"text": "x"}}}
        )

    # SDK 1.x appended the validation details to this text; SDK 2.x does not.
    assert fhir_stub.calls() == [("POST", "/Observation")]
    assert result.is_error
    assert result.text.startswith("Error executing tool create")


# update


async def test_update_forces_id_from_argument(mcp_server, fhir_stub, protocol_mode):
    payload = {"resourceType": "Patient", "id": "other", "gender": "male"}
    result = await call(
        mcp_server, protocol_mode, "update", {"type": "Patient", "id": "123", "payload": payload}
    )

    assert fhir_stub.calls() == [("PUT", "/Patient/123")]
    assert fhir_stub.requests[-1].body == {"resourceType": "Patient", "id": "123", "gender": "male"}
    assert result["id"] == "123"
    assert result["meta"]["versionId"] == "1"


# delete


async def test_delete_reports_success(mcp_server, fhir_stub, protocol_mode):
    result = await call(mcp_server, protocol_mode, "delete", {"type": "Patient", "id": "123"})

    assert result == outcome(
        "SUCCESSFUL_DELETE", "Successfully deleted resource(s).", severity="information"
    )
    assert fhir_stub.calls() == [("DELETE", "/Patient/123")]


async def test_delete_requires_id_or_search_params(mcp_server, fhir_stub, protocol_mode):
    result = await call(mcp_server, protocol_mode, "delete", {"type": "Patient"})

    assert result == outcome("required", "A required element id is missing.")
    assert fhir_stub.requests == []


# get_user


async def test_get_user_without_authentication_returns_empty(mcp_server, fhir_stub, protocol_mode):
    result = await call(mcp_server, protocol_mode, "get_user", {})

    assert result == {}
    assert fhir_stub.requests == []


# backend authentication


async def test_no_authorization_header_when_auth_disabled(mcp_server, fhir_stub, protocol_mode):
    await call(mcp_server, protocol_mode, "read", {"type": "Patient", "id": "123"})

    assert [r.authorization for r in fhir_stub.requests] == [None]
