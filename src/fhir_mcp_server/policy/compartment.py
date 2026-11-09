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

"""Which patient a FHIR record is about.

A subset of the FHIR R4 Patient CompartmentDefinition, limited to the element
that names the patient the record is about.
Types that are neither listed here nor shared are refused for users limited
to their own patients.
"""

from typing import Any, Dict, Iterable, Set, Tuple

# Resource type -> (search parameter, element paths naming the patient)
PATIENT_LINKS: Dict[str, Tuple[str, Tuple[str, ...]]] = {
    "Account": ("patient", ("subject",)),
    "AdverseEvent": ("subject", ("subject",)),
    "AllergyIntolerance": ("patient", ("patient",)),
    "Appointment": ("patient", ("participant.actor",)),
    "CarePlan": ("patient", ("subject",)),
    "CareTeam": ("patient", ("subject",)),
    "Claim": ("patient", ("patient",)),
    "ClinicalImpression": ("patient", ("subject",)),
    "Communication": ("patient", ("subject",)),
    "Condition": ("patient", ("subject",)),
    "Consent": ("patient", ("patient",)),
    "Coverage": ("beneficiary", ("beneficiary",)),
    "DetectedIssue": ("patient", ("patient",)),
    "DeviceRequest": ("patient", ("subject",)),
    "DiagnosticReport": ("patient", ("subject",)),
    "DocumentReference": ("patient", ("subject",)),
    "Encounter": ("patient", ("subject",)),
    "EpisodeOfCare": ("patient", ("patient",)),
    "ExplanationOfBenefit": ("patient", ("patient",)),
    "FamilyMemberHistory": ("patient", ("patient",)),
    "Flag": ("patient", ("subject",)),
    "Goal": ("patient", ("subject",)),
    "ImagingStudy": ("patient", ("subject",)),
    "Immunization": ("patient", ("patient",)),
    "Media": ("patient", ("subject",)),
    "MedicationAdministration": ("patient", ("subject",)),
    "MedicationDispense": ("patient", ("subject",)),
    "MedicationRequest": ("patient", ("subject",)),
    "MedicationStatement": ("patient", ("subject",)),
    "NutritionOrder": ("patient", ("patient",)),
    "Observation": ("patient", ("subject",)),
    "Person": ("patient", ("link.target",)),
    "Procedure": ("patient", ("subject",)),
    "QuestionnaireResponse": ("patient", ("subject",)),
    "RelatedPerson": ("patient", ("patient",)),
    "RiskAssessment": ("patient", ("subject",)),
    "ServiceRequest": ("patient", ("subject",)),
    "Specimen": ("patient", ("subject",)),
}


def patient_id(reference: str | None) -> str | None:
    """The patient ID in a reference such as `Patient/123` or `https://x/Patient/123/_history/2`."""
    if not reference:
        return None
    parts = reference.rstrip("/").split("/")
    for index, part in enumerate(parts[:-1]):
        if part == "Patient":
            return parts[index + 1] or None
    return None


def search_parameter(resource_type: str) -> str | None:
    """The search parameter that limits `resource_type` to given patients."""
    if resource_type == "Patient":
        return "_id"
    link = PATIENT_LINKS.get(resource_type)
    return link[0] if link else None


def search_values(parameter: str, ids: Iterable[str]) -> str:
    """The parameter value for these patients; several values mean "any of"."""
    typed = parameter == "subject"
    return ",".join(f"Patient/{i}" if typed else i for i in sorted(ids))


def record_patients(resource: Dict[str, Any]) -> Set[str] | None:
    """The patients a record is about, or None if its type is not patient-linked."""
    resource_type = resource.get("resourceType")
    if resource_type == "Patient":
        return {str(resource.get("id"))} if resource.get("id") else set()
    link = PATIENT_LINKS.get(str(resource_type))
    if link is None:
        return None
    patients: Set[str] = set()
    for path in link[1]:
        for value in _values(resource, path.split(".")):
            if isinstance(value, dict) and (pid := patient_id(value.get("reference"))):
                patients.add(pid)
    return patients


def _values(node: Any, path: list[str]) -> Iterable[Any]:
    if isinstance(node, list):
        for item in node:
            yield from _values(item, path)
        return
    if not path:
        yield node
        return
    if isinstance(node, dict) and path[0] in node:
        yield from _values(node[path[0]], path[1:])

