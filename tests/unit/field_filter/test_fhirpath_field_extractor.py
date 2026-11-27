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

from fhir_mcp_server.field_filter.fhirpath_field_extractor import (
    extract_fields_by_fhirpath,
)


class TestEvaluateFhirpath:
    PATIENT = {
        "resourceType": "Patient",
        "id": "p1",
        "name": [{"family": "Smith"}],
        "gender": "male",
        "birthDate": "1990-01-01",
    }

    PATIENT_MULTI_NAME = {
        "resourceType": "Patient",
        "id": "p2",
        "name": [
            {"use": "official", "family": "Smith"},
            {"use": "nickname", "family": "Smitty"},
        ],
    }

    PATIENT_WITH_URL_IDENTIFIER = {
        "resourceType": "Patient",
        "id": "p3",
        "identifier": [
            {"system": "http://hl7.org/fhir/sid/us-npi", "value": "1234567890"},
            {"system": "http://other-system.org", "value": "999"},
        ],
    }

    def test_wrong_resource_type_prefix_silently_skipped(self):
        """Expressions for a different resource type are skipped — not in result or _not_matched."""
        result = extract_fields_by_fhirpath(self.PATIENT, ["Observation.valueQuantity"])
        assert result == {}

    def test_unprefixed_expression_in_not_matched(self):
        """Expressions without an uppercase resource-type prefix go to _not_matched."""
        result = extract_fields_by_fhirpath(self.PATIENT, ["gender"])
        assert "gender" not in result
        assert "gender" in result.get("_not_matched", [])

    def test_matched_prefix_no_value_in_not_matched(self):
        """Correct prefix but field absent from resource goes to _not_matched."""
        result = extract_fields_by_fhirpath(self.PATIENT, ["Patient.deceased"])
        assert "deceased" not in result
        assert "Patient.deceased" in result.get("_not_matched", [])

    def test_empty_expression_does_not_raise(self):
        """Empty string expression is handled without raising — lands in _not_matched, not _errors."""
        result = extract_fields_by_fhirpath(self.PATIENT, [""])
        assert "_errors" not in result

    def test_where_clause_filters_by_condition(self):
        result = extract_fields_by_fhirpath(
            self.PATIENT_MULTI_NAME, ["Patient.name.where(use='official')"]
        )
        matched = result.get("name.where(use='official')")
        assert matched is not None
        assert len(matched) == 1
        assert matched[0]["family"] == "Smith"

    def test_scalar_field_unwrapped_from_list(self):
        """fhirpathpy always returns a list; scalar fields like birthDate must be unwrapped."""
        result = extract_fields_by_fhirpath(self.PATIENT, ["Patient.birthDate"])
        assert result["birthDate"] == "1990-01-01"

    def test_scalar_string_field_unwrapped(self):
        result = extract_fields_by_fhirpath(self.PATIENT, ["Patient.gender"])
        assert result["gender"] == "male"

    def test_array_field_stays_as_list_single_item(self):
        """Array fields must stay as lists even when there is only one item."""
        result = extract_fields_by_fhirpath(self.PATIENT, ["Patient.name"])
        assert isinstance(result["name"], list)

    def test_array_field_stays_as_list_multiple_items(self):
        result = extract_fields_by_fhirpath(self.PATIENT_MULTI_NAME, ["Patient.name"])
        assert isinstance(result["name"], list)
        assert len(result["name"]) == 2

    def test_where_clause_result_stays_as_list(self):
        """A where() filter on an array field should still return a list."""
        result = extract_fields_by_fhirpath(
            self.PATIENT_MULTI_NAME, ["Patient.name.where(use='official')"]
        )
        assert isinstance(result["name.where(use='official')"], list)

    def test_url_with_dot_in_fhirpath_expression(self):
        """FHIRPath expressions containing dots inside strings/URLs must evaluate correctly without getting split."""
        result = extract_fields_by_fhirpath(
            self.PATIENT_WITH_URL_IDENTIFIER,
            ["Patient.identifier.where(system='http://hl7.org/fhir/sid/us-npi').value"],
        )
        key = "identifier.where(system='http://hl7.org/fhir/sid/us-npi').value"
        assert key in result
        assert result[key] == ["1234567890"]
