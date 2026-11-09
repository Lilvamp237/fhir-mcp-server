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

"""The two checkpoints every tool passes through.

1. Before the FHIR request: limit searches to the caller's patients and refuse
   what the caller may not ask for.
2. After the FHIR response: drop every record that does not belong to one of
   the caller's patients and is not a shared type.

Users with access to every patient (practitioners) pass through unchanged.
"""

import logging

from typing import Any, Dict, List

from fhir_mcp_server.policy.compartment import (
    record_patients,
    search_parameter,
    search_values,
    patient_id,
)
from fhir_mcp_server.policy.identity import Access, PolicyDenied
from fhir_mcp_server.policy.rules import Rules

audit_logger: logging.Logger = logging.getLogger("fhir_mcp_server.audit")

SearchParams = Dict[str, str | List[str]]


def check_blocked(access: Access, rules: Rules, params: SearchParams) -> None:
    """Refuse parameters that can pull in records of other patients (includes,
    reverse includes, _has, named queries, chained parameters)."""
    if access.patients is None:
        return
    for name in params:
        if name.split(":")[0] in rules.blocked_search_params or "." in name:
            raise PolicyDenied(f"The search parameter '{name}' is not available to your account.")


def check_search(access: Access, rules: Rules, resource_type: str, params: SearchParams) -> SearchParams:
    """Checkpoint 1 for searches: the parameters to send, limited to the caller's patients."""
    if access.patients is None:
        return params
    check_blocked(access, rules, params)
    if resource_type in rules.shared_types:
        return params
    parameter = search_parameter(resource_type)
    if parameter is None:
        raise PolicyDenied(f"Your account cannot access {resource_type} records.")

    requested: set[str] = set()
    limited: SearchParams = {}
    for name, value in params.items():
        if name.split(":")[0] != parameter:
            limited[name] = value
            continue
        for item in value if isinstance(value, list) else [value]:
            for part in str(item).split(","):
                if part.strip():
                    requested.add(patient_id(part.strip()) or part.strip().split("/")[-1])
    if not requested <= access.patients:
        raise PolicyDenied(f"Your account cannot access {resource_type} records of other patients.")
    limited[parameter] = search_values(parameter, requested or access.patients)
    return limited


def allows(access: Access, rules: Rules, resource: Dict[str, Any]) -> bool:
    """Whether the caller may see this record."""
    if access.patients is None:
        return True
    resource_type = resource.get("resourceType")
    if resource_type in rules.shared_types or resource_type == "OperationOutcome":
        return True
    patients = record_patients(resource)
    return bool(patients and patients & access.patients)


def filter_result(access: Access, rules: Rules, data: Any) -> Any:
    """Checkpoint 2: the result without records the caller may not see.
    A single record the caller may not see becomes None."""
    if access.patients is None:
        return data
    if isinstance(data, list):
        return [item for item in data if allows(access, rules, _resource(item))]
    if not isinstance(data, dict):
        return data
    if data.get("resourceType") != "Bundle":
        return data if allows(access, rules, data) else None
    entries = data.get("entry") or []
    kept = [entry for entry in entries if allows(access, rules, _resource(entry))]
    filtered = {**data, "entry": kept}
    if len(kept) != len(entries):
        filtered.pop("total", None)  # the server's count would include the removed records
    return filtered


def check_write(
    access: Access,
    rules: Rules,
    action: str,
    resource: Dict[str, Any],
    conditional: bool = False,
    operation: str = "",
) -> None:
    """Checkpoint 1 for create, update and delete."""
    if access.patients is None:
        return
    if action == "delete":
        raise PolicyDenied("Your account cannot delete records.")
    if access.role != "patient" or rules.patient_writes != "own":
        raise PolicyDenied("Your account cannot create or change records.")
    if conditional or operation:
        raise PolicyDenied("Your account can only create or change single records by ID.")
    if resource.get("resourceType") == "Patient" and action == "create":
        raise PolicyDenied("Your account cannot create patient records.")
    patients = record_patients(resource)
    if not patients or not patients <= access.patients:
        raise PolicyDenied("Your account can only create or change its own records.")


def audit(access: Access | None, tool: str, resource_type: str, decision: str, detail: str = "") -> None:
    """One line per decision; never contains record contents."""
    if access is None:
        return
    audit_logger.info(
        "user=%s role=%s tool=%s type=%s decision=%s%s",
        access.user, access.role, tool, resource_type or "-", decision, f" {detail}" if detail else "",
    )


def _resource(item: Any) -> Dict[str, Any]:
    """A Bundle entry or a bare resource, as a resource."""
    if isinstance(item, dict) and isinstance(item.get("resource"), dict):
        return item["resource"]
    return item if isinstance(item, dict) else {}

