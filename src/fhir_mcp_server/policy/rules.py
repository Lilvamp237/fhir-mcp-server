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

"""The access rules: built-in defaults plus the optional FHIR_POLICY_* settings.

The rules live in one object, so a policy file can fill it later without
changing the enforcement code.
"""

from dataclasses import dataclass
from typing import FrozenSet, Literal

from fhir_mcp_server.oauth.types import ServerConfigs

# Search parameters that can pull in records of other patients.
BLOCKED_SEARCH_PARAMS: FrozenSet[str] = frozenset(
    {"_include", "_revinclude", "_has", "_query", "_filter", "_contained", "_containedType"}
)


@dataclass(frozen=True)
class Rules:
    patient_writes: Literal["deny", "own"] = "deny"
    caregivers: bool = True
    shared_types: FrozenSet[str] = frozenset(
        {"Practitioner", "PractitionerRole", "Organization", "Location", "Medication"}
    )
    blocked_search_params: FrozenSet[str] = BLOCKED_SEARCH_PARAMS
    read_only: bool = False

    @classmethod
    def from_configs(cls, configs: ServerConfigs) -> "Rules":
        return cls(
            patient_writes=configs.policy_patient_writes,
            caregivers=configs.policy_caregivers,
            shared_types=frozenset(configs.policy_shared_types.replace(",", " ").split()),
            read_only=configs.mcp_read_only,
        )

