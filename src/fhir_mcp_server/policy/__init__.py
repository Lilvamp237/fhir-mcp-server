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

"""Data access policy for signed-in users.

The FHIR server often sees only the MCP server's own identity (a system token,
or no token at all), so it cannot tell users apart. The MCP server therefore
decides which records each user may see: patients their own, caregivers their
patients', practitioners all.
"""

from .enforce import allows, audit, check_blocked, check_search, check_write, filter_result
from .identity import Access, AccessResolver, Fetch, PolicyDenied, can_gain_scope
from .rules import Rules

__all__ = [
    "Access",
    "AccessResolver",
    "Fetch",
    "PolicyDenied",
    "Rules",
    "allows",
    "audit",
    "can_gain_scope",
    "check_blocked",
    "check_search",
    "check_write",
    "filter_result",
]
