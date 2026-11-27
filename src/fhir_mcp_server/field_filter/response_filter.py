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

from collections.abc import Mapping
from typing import Any, Dict, List
import logging

from fhir_mcp_server.field_filter.fhirpath_field_extractor import (
    extract_fields_by_fhirpath,
)

logger: logging.Logger = logging.getLogger(__name__)


def _group_paths_by_resource_type(entry_paths: List[str]) -> Dict[str, List[str]]:
    """Group entry_paths by their resource type prefix."""

    paths_by_resource_type: Dict[str, List[str]] = {}
    for path in entry_paths:
        if not path or "." not in path:
            continue

        # Support FHIRPaths that start with Bundle.entry.resource.
        # eg: Bundle.entry.resource.Patient.name -> Patient.name
        if path.startswith("Bundle.entry.resource."):
            path = path.replace("Bundle.entry.resource.", "")

        # Extract the resource prefix (e.g. 'Patient' from 'Patient.name.given')
        resource_type_prefix = path.split(".", 1)[0]
        if not resource_type_prefix or not resource_type_prefix[0].isupper():
            continue

        if resource_type_prefix not in paths_by_resource_type:
            paths_by_resource_type[resource_type_prefix] = []

        paths_by_resource_type[resource_type_prefix].append(path)

    return paths_by_resource_type


def _with_preserved_fields(
    original: Mapping[str, Any], filtered: Dict[str, Any]
) -> Dict[str, Any]:
    """Return a new dictionary with id and resourceType preserved at the beginning."""
    result = {}

    for field in ["id", "resourceType"]:
        if field in original:
            result[field] = original[field]

    result.update(filtered)

    return result


def _filter_single_resource(
    resource: Mapping[str, Any],
    paths_by_resource_type: Dict[str, List[str]],
) -> Dict[str, Any]:
    """Filter a single, non-Bundle FHIR resource (e.g., Patient, Observation)."""
    resource_type = resource.get("resourceType", "")
    paths = paths_by_resource_type.get(resource_type, [])
    return extract_fields_by_fhirpath(resource, paths)


def _filter_nested_bundle(
    bundle: Mapping[str, Any],
    paths_by_resource_type: Dict[str, List[str]],
) -> Dict[str, Any]:
    """Filter a Bundle that is nested inside another Bundle's entries."""
    logger.debug("Filtering nested Bundle with ID: %s", bundle.get("id"))
    bundle_fields = paths_by_resource_type.get("Bundle", [])

    # Filter the nested Bundle wrapper
    if bundle_fields:
        result = extract_fields_by_fhirpath(bundle, bundle_fields)
    else:
        result = {}
    result = _with_preserved_fields(bundle, result)

    # Filter each inner entry inside the nested Bundle
    nested_entries = []
    for entry in bundle.get("entry", []):
        if not isinstance(entry, Mapping) or "resource" not in entry:
            nested_entries.append(entry)
            continue

        inner_res = entry["resource"]
        if not isinstance(inner_res, Mapping):
            nested_entries.append(entry)
            continue

        # Filter the inner resource and preserve its required fields
        filtered_inner = _filter_single_resource(inner_res, paths_by_resource_type)
        filtered_inner = _with_preserved_fields(inner_res, filtered_inner)
        nested_entries.append(filtered_inner)

    result["entry"] = nested_entries
    return result


def _filter_bundle_entry(
    entry: Mapping[str, Any],
    paths_by_resource_type: Dict[str, List[str]],
) -> Dict[str, Any]:
    """Filter a single Bundle entry, handling nested Bundles iteratively."""
    resource = entry.get("resource")
    
    # Handles resources extracted by extract_bundle_resources()
    if resource is None:
        return _filter_single_resource(entry, paths_by_resource_type)

    if not isinstance(resource, Mapping):
        return dict(entry)

    resource_type = resource.get("resourceType", "")
    logger.debug("Filtering entry resource type: %s", resource_type)

    if resource_type == "Bundle":
        filtered = _filter_nested_bundle(resource, paths_by_resource_type)
    else:
        filtered = _filter_single_resource(resource, paths_by_resource_type)
        filtered = _with_preserved_fields(resource, filtered)

    # Carry search metadata forward
    if entry.get("search", {}).get("mode") == "include":
        filtered["search"] = entry.get("search")

    return filtered


def filter_response_fields(
    data: Any,
    field_paths: List[str] | None = None,
) -> Any:
    """Filter a response with a single FHIR resource or Bundle of resources to only the matched fields."""
    if not field_paths:
        return data

    if not isinstance(data, Mapping):
        return data

    paths_by_resource_type = _group_paths_by_resource_type(field_paths)
    resource_type = data.get("resourceType", "")

    if resource_type == "Bundle" and isinstance(data.get("entry"), list):
        bundle_fields = paths_by_resource_type.get("Bundle", [])

        # Filter the top-level Bundle wrapper
        if bundle_fields:
            result = extract_fields_by_fhirpath(data, bundle_fields)
        else:
            result = dict(data)

        result["entry"] = [
            _filter_bundle_entry(entry, paths_by_resource_type)
            if isinstance(entry, Mapping)
            else entry
            for entry in data.get("entry", [])
        ]

        return result

    elif resource_type:
        return _filter_single_resource(data, paths_by_resource_type)

    return data
