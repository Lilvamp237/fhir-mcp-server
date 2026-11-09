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

"""Who is calling, and whose records they may see.

The role and the allowed patients come from the `fhirUser` claim of the
verified MCP access token, which the identity provider sets. Caregivers
(`RelatedPerson`) and persons (`Person`) are looked up on the FHIR server and
the result is cached for a few minutes.
"""

import logging

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, FrozenSet, Set

from fhir_mcp_server.memory_store import MemoryStore
from fhir_mcp_server.oauth.types import IDToken
from fhir_mcp_server.policy.compartment import patient_id
from fhir_mcp_server.policy.rules import Rules

logger: logging.Logger = logging.getLogger(__name__)

ROLES: Dict[str, str] = {
    "Patient": "patient",
    "RelatedPerson": "caregiver",
    "Person": "person",
    "Practitioner": "practitioner",
    "PractitionerRole": "practitioner",
}
LOOKUP_CACHE_SECONDS = 300

# Reads one resource from the FHIR server; returns None when it does not exist.
Fetch = Callable[[str, str], Awaitable[Dict[str, Any] | None]]


class PolicyDenied(Exception):
    """The caller may not make this request. The message is safe to show them."""


def can_gain_scope(claims: Dict[str, Any], rules: Rules) -> bool:
    """Whether signing in again could let this user change records. Practitioners may
    hold a token with fewer scopes than their role allows; patients only when the policy
    lets them write; caregivers, persons and unknown users never."""
    parsed = IDToken(fhirUser=claims.get("fhirUser")).parse_fhir_user()
    role = ROLES.get(parsed[0]) if parsed else None
    return role == "practitioner" or (role == "patient" and rules.patient_writes == "own")


@dataclass(frozen=True)
class Access:
    role: str
    user: str  # the fhirUser reference
    patients: FrozenSet[str] | None

    @property
    def restricted(self) -> bool:
        return self.patients is not None


class AccessResolver:
    def __init__(self, rules: Rules) -> None:
        self._rules = rules
        self._cache = MemoryStore()

    async def resolve(self, claims: Dict[str, Any], fetch: Fetch, fhir_patient: str | None = None) -> Access:
        """The caller's access. `fhir_patient` is the patient a per-user FHIR sign-in
        returned, which takes precedence over the identity provider's record ID."""
        fhir_user = str(claims.get("fhirUser") or "")
        parsed = IDToken(fhirUser=fhir_user or None).parse_fhir_user()
        role = ROLES.get(parsed[0]) if parsed else None
        if parsed is None or role is None:
            raise PolicyDenied("Your account is not linked to a FHIR record that grants access.")
        resource_type, resource_id = parsed
        if role == "practitioner":
            return Access(role, fhir_user, None)
        if role == "patient":
            return Access(role, fhir_user, frozenset({fhir_patient or resource_id}))
        if not self._rules.caregivers:
            raise PolicyDenied("Caregiver access is turned off on this server.")

        cache_key = f"{claims.get('sub')}|{fhir_user}"
        if (patients := self._cache.get(cache_key)) is None:
            try:
                if resource_type == "RelatedPerson":
                    related = await fetch("RelatedPerson", resource_id)
                    patients = {p} if related and (p := _related_patient(related)) else set()
                else:
                    patients = await self._person_patients(resource_id, fetch)
            except Exception as ex:
                logger.warning("Could not look up the patients for %s: %s", fhir_user, ex)
                raise PolicyDenied("Your access could not be verified. Try again later.") from ex
            self._cache.put(cache_key, frozenset(patients), LOOKUP_CACHE_SECONDS)
        if not patients:
            raise PolicyDenied("Your account is not linked to any patient you may access.")
        return Access(role, fhir_user, frozenset(patients))

    async def _person_patients(self, person_id: str, fetch: Fetch) -> Set[str]:
        """Patients reached through Person.link: Patient targets, and the patients of
        RelatedPerson targets. Practitioner links never grant access."""
        person = await fetch("Person", person_id)
        if not person or person.get("active") is False:
            return set()
        patients: Set[str] = set()
        for link in person.get("link") or []:
            reference = str((link.get("target") or {}).get("reference") or "")
            if pid := patient_id(reference):
                patients.add(pid)
            elif "RelatedPerson/" in reference:
                related = await fetch("RelatedPerson", reference.rstrip("/").split("/")[-1])
                if related and (pid := _related_patient(related)):
                    patients.add(pid)
        return patients


def _related_patient(related: Dict[str, Any]) -> str | None:
    """The patient of an active RelatedPerson whose period, if any, covers today."""
    if related.get("active") is False:
        return None
    period = related.get("period") or {}
    now = datetime.now(timezone.utc)
    start, end = _instant(period.get("start")), _instant(period.get("end"))
    if (start and start > now) or (end and end < now):
        return None
    return patient_id((related.get("patient") or {}).get("reference"))


def _instant(value: str | None) -> datetime | None:
    """A FHIR date or dateTime as an aware datetime. Dates count from midnight UTC, and
    partial dates (`2024`, `2024-05`) from their first day."""
    if not value:
        return None
    if len(value) in (4, 7):
        value = f"{value}-01" if len(value) == 7 else f"{value}-01-01"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

