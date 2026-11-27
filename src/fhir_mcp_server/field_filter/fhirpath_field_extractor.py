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

import logging
import re
import fhirpathpy

from typing import Any, Dict, List

logger: logging.Logger = logging.getLogger(__name__)


def extract_fields_by_fhirpath(
    resource: Dict[str, Any],
    field_paths: List[str],
) -> Dict[str, Any]:
    """Evaluates a list of FHIRPath expressions against a resource and returns a dict of only the matched fields.

    Expressions must be prefixed with the resource type (e.g. Patient.name).
    Expressions that match the prefix but yield no value are recorded in _not_matched.
    Expressions that cause evaluation errors are recorded in _errors.
    """

    resource_type = resource.get("resourceType", "")

    result: Dict[str, Any] = {}
    not_matched: List[str] = []
    errors: List[str] = []

    if not field_paths:
        return dict(resource)

    for path in field_paths:
        if not path or "." not in path:
            not_matched.append(path)
            continue

        # Split to separate the resource type (e.g. 'Patient') from the rest of the path,
        # preserving dots inside string literals or URLs (e.g., inside .where() clauses).
        resource_type_prefix, result_key = path.split(".", 1)
        if not resource_type_prefix or not resource_type_prefix[0].isupper():
            not_matched.append(path)
            continue

        if resource_type_prefix != resource_type:
            continue

        try:
            matched = fhirpathpy.evaluate(resource, path)
            if matched:
                # Extract the base property name by splitting on the first dot or opening parenthesis
                # (e.g. "name.family" -> "name", "name.where(...)" -> "name").
                root_field = re.split(r"[.(]", result_key, maxsplit=1)[0]

                # fhirpathpy always returns a list. To keep the output standard, unwrap to a single value
                # only if the field is expected to be a scalar/non-list in the original resource schema.
                result[result_key] = (
                    matched
                    if isinstance(resource.get(root_field), list) or len(matched) > 1
                    else matched[0]
                )
            else:
                not_matched.append(path)
        except Exception as e:
            logger.warning("FHIRPath eval failed for expression %r: %s", path, e)
            errors.append(path)

    logger.debug(
        "%s: matched %s, skipped %s",
        resource_type or "unknown",
        list(result),
        not_matched + errors,
    )
    if not_matched:
        result["_not_matched"] = not_matched
    if errors:
        result["_errors"] = errors
    return result
