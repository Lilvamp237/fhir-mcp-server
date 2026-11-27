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

from fhir_mcp_server.field_filter import filter_response_fields


class TestFilterResponseFields:
    PATIENT = {
        "resourceType": "Patient",
        "id": "p1",
        "name": [{"family": "Smith"}],
        "gender": "male",
        "birthDate": "1990-01-01",
    }

    OBSERVATION = {
        "resourceType": "Observation",
        "id": "o1",
        "status": "final",
        "valueQuantity": {"value": 7.2, "unit": "mmol/L"},
    }

    def test_no_field_paths_returns_data_unchanged(self):
        assert filter_response_fields(self.PATIENT) is self.PATIENT

    def test_non_dict_returned_unchanged(self):
        assert filter_response_fields("raw-string", ["Patient.name"]) == "raw-string"
        assert filter_response_fields(42, ["Patient.name"]) == 42

    def test_single_resource_matched(self):
        result = filter_response_fields(self.PATIENT, ["Patient.name"])
        assert "name" in result
        assert "gender" not in result
        assert "birthDate" not in result

    # --- Bundle traversal ---

    def test_bundle_entries_filtered(self):
        bundle = {
            "resourceType": "Bundle",
            "id": "b1",
            "entry": [
                {"resource": self.PATIENT},
                {"resource": self.OBSERVATION},
            ],
        }
        result = filter_response_fields(bundle, ["Patient.name"])
        assert len(result["entry"]) == 2
        assert "name" in result["entry"][0]
        assert "gender" not in result["entry"][0]
        assert "name" not in result["entry"][1]
        assert "status" in result["entry"][1]  # Observation unchanged

    def test_bundle_mixed_resource_types_filtered_correctly(self):
        """Each Bundle entry is only filtered by the expression matching its resource type."""
        bundle = {
            "resourceType": "Bundle",
            "entry": [
                {"resource": self.PATIENT},
                {"resource": self.OBSERVATION},
            ],
        }
        result = filter_response_fields(
            bundle, ["Patient.name", "Observation.valueQuantity"]
        )
        assert "name" in result["entry"][0]
        assert "valueQuantity" not in result["entry"][0]
        assert "valueQuantity" in result["entry"][1]
        assert "name" not in result["entry"][1]

    def test_bundle_level_path_included_in_result(self):
        bundle = {
            "resourceType": "Bundle",
            "id": "b1",
            "type": "searchset",
            "entry": [{"resource": self.PATIENT}],
        }
        result = filter_response_fields(bundle, ["Bundle.type", "Patient.name"])
        assert result["type"] == "searchset"
        assert "name" in result["entry"][0]

    def test_bundle_entry_wrapper_preserves_search(self):
        """search metadata on the entry wrapper must be carried through after filtering."""
        bundle = {
            "resourceType": "Bundle",
            "entry": [
                {
                    "fullUrl": "http://example.com/Patient/p1",
                    "resource": self.PATIENT,
                    "search": {"mode": "include"},
                }
            ],
        }
        result = filter_response_fields(bundle, ["Patient.name"])
        assert result["entry"][0]["search"] == {"mode": "include"}
        assert "name" in result["entry"][0]

    def test_bundle_entry_wrapper_drops_full_url(self):
        """fullUrl is intentionally not preserved — only the filtered resource fields and search are kept."""

        bundle = {
            "resourceType": "Bundle",
            "entry": [
                {
                    "fullUrl": "http://example.com/Patient/p1",
                    "resource": self.PATIENT,
                }
            ],
        }
        result = filter_response_fields(bundle, ["Patient.name"])
        assert "fullUrl" not in result["entry"][0]

    def test_read_plain_resource_omits_id_and_resource_type(self):
        """A directly read resource does not automatically inject id and resourceType if not requested."""
        result = filter_response_fields(self.PATIENT, ["Patient.name"])
        assert "id" not in result
        assert "resourceType" not in result

    def test_read_bundle_entries_include_id_and_resource_type(self):
        """Bundle entries always preserve id and resourceType even if not explicitly requested."""
        bundle = {
            "resourceType": "Bundle",
            "entry": [
                {"search": {"mode": "include"}, "resource": self.PATIENT},
            ],
        }
        result = filter_response_fields(bundle, ["Patient.name"])
        assert "id" in result["entry"][0]
        assert "resourceType" in result["entry"][0]
        assert result["entry"][0]["id"] == "p1"
        assert result["entry"][0]["resourceType"] == "Patient"

    def test_bundle_entry_resource_prefix_path(self):
        bundle = {"resourceType": "Bundle", "entry": [{"resource": self.PATIENT}]}
        result = filter_response_fields(bundle, ["Bundle.entry.resource.Patient.name"])
        assert "name" in result["entry"][0]
        assert "gender" not in result["entry"][0]

    def test_nested_bundle_filtering(self):
        nested_bundle = {
            "resourceType": "Bundle",
            "id": "inner_b1",
            "entry": [{"resource": self.PATIENT}, {"resource": self.OBSERVATION}],
        }
        bundle = {"resourceType": "Bundle", "entry": [{"resource": nested_bundle}]}
        result = filter_response_fields(bundle, ["Patient.name"])
        inner_result = result["entry"][0]
        assert inner_result["resourceType"] == "Bundle"
        assert inner_result["id"] == "inner_b1"
        assert "name" in inner_result["entry"][0]
        assert "gender" not in inner_result["entry"][0]
        assert "name" not in inner_result["entry"][1]

    def test_entry_without_resource(self):
        bundle = {
            "resourceType": "Bundle",
            "entry": [{"response": {"status": "200 OK"}}],
        }
        result = filter_response_fields(bundle, ["Patient.name"])
        assert result["entry"][0] == {"response": {"status": "200 OK"}}

    def test_resource_without_resource_type(self):
        data = {"id": "1", "name": "John"}
        result = filter_response_fields(data, ["Patient.name"])
        assert result == data

    def test_nested_bundle_not_matched_stripped(self):
        nested_bundle = {
            "resourceType": "Bundle",
            "id": "inner_b2",
            "entry": [{"resource": self.PATIENT}],
        }
        bundle = {"resourceType": "Bundle", "entry": [{"resource": nested_bundle}]}
        result = filter_response_fields(bundle, ["Patient.non_existent"])
        inner_result = result["entry"][0]
        assert "_not_matched" in inner_result["entry"][0]
        assert "Patient.non_existent" in inner_result["entry"][0]["_not_matched"]

    def test_nested_bundle_entry_without_resource(self):
        nested_bundle = {
            "resourceType": "Bundle",
            "entry": [{"response": {"status": "200"}}],
        }
        bundle = {"resourceType": "Bundle", "entry": [{"resource": nested_bundle}]}
        result = filter_response_fields(bundle, ["Patient.name"])
        inner_result = result["entry"][0]
        assert inner_result["entry"][0] == {"response": {"status": "200"}}

    def test_unwrapped_bundle_entries_filtered(self):
        """Test filtering on a plain dictionary containing entries but no resourceType (e.g. from extract_bundle_resources)."""
        data = {"resourceType": "Bundle", "entry": [self.PATIENT, self.OBSERVATION]}
        result = filter_response_fields(
            data, ["Patient.name", "Observation.valueQuantity"]
        )
        assert "entry" in result
        assert len(result["entry"]) == 2
        # Verify Patient filtered
        assert "name" in result["entry"][0]
        assert "gender" not in result["entry"][0]
        # Verify Observation filtered
        assert "valueQuantity" in result["entry"][1]
        assert "status" not in result["entry"][1]

    def test_unwrapped_bundle_entry_shaped_filtered(self):
        """Test filtering on an unwrapped bundle where entries are entry-shaped wrappers (containing 'resource')."""
        data = {
            "resourceType": "Bundle",
            "entry": [{"resource": self.PATIENT}, {"resource": self.OBSERVATION}],
        }
        result = filter_response_fields(
            data, ["Patient.name", "Observation.valueQuantity"]
        )
        assert "entry" in result
        assert len(result["entry"]) == 2
        # Verify Patient entry filtered
        assert "name" in result["entry"][0]
        assert "gender" not in result["entry"][0]
        assert "id" in result["entry"][0]
        assert "resourceType" in result["entry"][0]
        # Verify Observation entry filtered
        assert "valueQuantity" in result["entry"][1]
        assert "status" not in result["entry"][1]
        assert "id" in result["entry"][1]
        assert "resourceType" in result["entry"][1]
