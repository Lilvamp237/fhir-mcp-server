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

"""The access policy's building blocks, without a server."""

from typing import Any, Dict

import pytest

from fhir_mcp_server.policy import (
    Access,
    AccessResolver,
    PolicyDenied,
    Rules,
    check_search,
    check_write,
    filter_result,
)
from fhir_mcp_server.policy.compartment import patient_id, record_patients
from fhir_mcp_server.policy.identity import _related_patient

PATIENT = Access("patient", "Patient/1", frozenset({"1"}))
CAREGIVER = Access("caregiver", "RelatedPerson/r", frozenset({"1", "2"}))
PRACTITIONER = Access("practitioner", "Practitioner/p", None)
RULES = Rules()


class TestCompartment:
    @pytest.mark.parametrize(
        "reference, expected",
        [
            ("Patient/1", "1"),
            ("https://ehr.example.org/fhir/Patient/1/_history/3", "1"),
            ("Group/1", None),
            ("", None),
            (None, None),
        ],
    )
    def test_patient_id(self, reference, expected):
        assert patient_id(reference) == expected

    def test_record_patients_follows_nested_lists(self):
        appointment = {
            "resourceType": "Appointment",
            "participant": [{"actor": {"reference": "Patient/1"}}, {"actor": {"reference": "Practitioner/p"}}],
        }
        assert record_patients(appointment) == {"1"}

    def test_unlisted_types_have_no_patient_link(self):
        assert record_patients({"resourceType": "Basic", "subject": {"reference": "Patient/1"}}) is None


class TestCheckSearch:
    def test_adds_the_patient_filter(self):
        assert check_search(PATIENT, RULES, "Observation", {"code": "x"}) == {"code": "x", "patient": "1"}

    def test_subject_parameters_use_typed_references(self):
        assert check_search(PATIENT, RULES, "AdverseEvent", {}) == {"subject": "Patient/1"}

    def test_caregiver_may_narrow_to_one_of_their_patients(self):
        assert check_search(CAREGIVER, RULES, "Condition", {"patient": "Patient/2"}) == {"patient": "2"}
        assert check_search(CAREGIVER, RULES, "Condition", {}) == {"patient": "1,2"}

    def test_refuses_patients_outside_the_set(self):
        with pytest.raises(PolicyDenied):
            check_search(CAREGIVER, RULES, "Condition", {"patient": ["1", "3"]})

    def test_practitioners_are_unchanged(self):
        params = {"_include": "Condition:subject", "patient.name": "x"}
        assert check_search(PRACTITIONER, RULES, "Condition", params) is params


class TestFilterResult:
    def test_drops_other_patients_and_the_total(self):
        bundle = {
            "resourceType": "Bundle",
            "total": 2,
            "entry": [
                {"resource": {"resourceType": "Condition", "id": "a", "subject": {"reference": "Patient/1"}}},
                {"resource": {"resourceType": "Condition", "id": "b", "subject": {"reference": "Patient/9"}}},
                {"resource": {"resourceType": "Organization", "id": "o"}},
            ],
        }
        result = filter_result(PATIENT, RULES, bundle)
        assert [e["resource"]["id"] for e in result["entry"]] == ["a", "o"]
        assert "total" not in result

    def test_single_record_of_another_patient_becomes_none(self):
        assert filter_result(PATIENT, RULES, {"resourceType": "Patient", "id": "9"}) is None


class TestCheckWrite:
    def test_writes_are_refused_by_default(self):
        with pytest.raises(PolicyDenied):
            check_write(PATIENT, RULES, "create", {"resourceType": "Observation", "subject": {"reference": "Patient/1"}})

    def test_own_writes_allow_records_of_the_patient_only(self):
        rules = Rules(patient_writes="own")
        check_write(PATIENT, rules, "create", {"resourceType": "Observation", "subject": {"reference": "Patient/1"}})
        with pytest.raises(PolicyDenied):
            check_write(PATIENT, rules, "create", {"resourceType": "Observation", "subject": {"reference": "Patient/2"}})
        with pytest.raises(PolicyDenied):
            check_write(PATIENT, rules, "create", {"resourceType": "Observation"}, conditional=True)

    def test_caregivers_cannot_write_and_nobody_restricted_deletes(self):
        rules = Rules(patient_writes="own")
        with pytest.raises(PolicyDenied):
            check_write(CAREGIVER, rules, "update", {"resourceType": "Observation", "subject": {"reference": "Patient/1"}})
        with pytest.raises(PolicyDenied):
            check_write(PATIENT, rules, "delete", {"resourceType": "Observation", "id": "x"})


class TestIdentity:
    @pytest.mark.parametrize(
        "related, expected",
        [
            ({"patient": {"reference": "Patient/1"}}, "1"),
            ({"active": False, "patient": {"reference": "Patient/1"}}, None),
            ({"period": {"end": "2020"}, "patient": {"reference": "Patient/1"}}, None),
            ({"period": {"start": "2999-01-01"}, "patient": {"reference": "Patient/1"}}, None),
            ({"period": {"start": "2020-05", "end": "2999-12-31T00:00:00Z"}, "patient": {"reference": "Patient/1"}}, "1"),
        ],
    )
    def test_related_person_must_be_active_and_current(self, related, expected):
        assert _related_patient({"resourceType": "RelatedPerson", **related}) == expected

    async def test_lookups_are_cached(self):
        calls = []

        async def fetch(resource_type: str, resource_id: str) -> Dict[str, Any] | None:
            calls.append((resource_type, resource_id))
            return {"resourceType": "RelatedPerson", "patient": {"reference": "Patient/7"}}

        resolver = AccessResolver(Rules())
        claims = {"sub": "u", "fhirUser": "RelatedPerson/r"}
        first = await resolver.resolve(claims, fetch)
        second = await resolver.resolve(claims, fetch)
        assert first.patients == second.patients == frozenset({"7"})
        assert calls == [("RelatedPerson", "r")]

    async def test_failed_lookup_denies(self):
        async def fetch(resource_type: str, resource_id: str) -> Dict[str, Any] | None:
            raise ConnectionError("FHIR server unreachable")

        with pytest.raises(PolicyDenied):
            await AccessResolver(Rules()).resolve({"sub": "u", "fhirUser": "Person/p"}, fetch)

    async def test_patient_from_the_fhir_sign_in_wins(self):
        async def fetch(resource_type: str, resource_id: str) -> Dict[str, Any] | None:
            return None

        access = await AccessResolver(Rules()).resolve({"fhirUser": "Patient/idp-1"}, fetch, fhir_patient="epic-9")
        assert access.patients == frozenset({"epic-9"})
