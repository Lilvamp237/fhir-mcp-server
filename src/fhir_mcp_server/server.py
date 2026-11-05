# Copyright (c) 2025-2026, WSO2 LLC. (https://www.wso2.com/) All Rights Reserved.

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

import click
import logging


from fhir_mcp_server.field_filter import filter_response_fields
from fhir_mcp_server.utils import (
    build_user_profile,
    create_async_fhir_client,
    extract_bundle_resources,
    get_default_headers,
    get_operation_outcome,
    get_operation_outcome_exception,
    get_operation_outcome_required_error,
    get_capability_statement,
    trim_resource_capabilities,
    validate_resource_type,
    validate_inputs,
)
from fhir_mcp_server.auth import ToolScopeMiddleware, create_token_verifier, tool_scopes
from fhir_mcp_server.backend_auth import BackendAuthError, SystemTokenProvider
from fhir_mcp_server.fhir_connect import FhirConnect
from fhir_mcp_server.oauth import IDToken, ServerConfigs
from fhir_mcp_server.oauth.types import ConfigurationError
from fhirpy import AsyncFHIRClient
from fhirpy.base.exceptions import OperationOutcome, ResourceNotFound
from fhirpy.base.searchset import Raw
from typing import Dict, Any, List
from typing_extensions import Annotated
from pydantic import AnyHttpUrl, Field
from starlette.applications import Starlette
from starlette.middleware import Middleware
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.routes import build_resource_metadata_url
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import UrlElicitationRequiredError
from mcp_types import ElicitRequest, ElicitRequestURLParams, InputRequiredResult
from mcp_types.version import is_version_at_least
from importlib.metadata import PackageNotFoundError, version

import secrets

import uvicorn

logger: logging.Logger = logging.getLogger(__name__)

try:
    SERVER_VERSION: str = version("fhir-mcp-server")
except PackageNotFoundError:
    SERVER_VERSION = "0.0.0"

configs: ServerConfigs = ServerConfigs()

system_token_provider: SystemTokenProvider = SystemTokenProvider(configs)

# Per-user FHIR sign-ins (FHIR_SERVER_AUTH_MODE=user_smart_login); replaced by configure_mcp_server().
fhir_connect: FhirConnect = FhirConnect(configs)

FHIR_CONNECT_MESSAGE = (
    "Connect your FHIR server account to continue. Sign in with the link, then retry the request."
)


async def get_backend_access_token() -> str | None:
    """
    The credential sent to the FHIR server, chosen by FHIR_SERVER_AUTH_MODE.
    The MCP access token is never forwarded to the FHIR server.
    """
    mode: str = configs.effective_server_auth_mode
    if mode == "static_token":
        return configs.server_access_token
    if mode in ("client_credentials", "private_key_jwt"):
        return await system_token_provider.get_token()
    if mode == "user_smart_login":
        access_token: AccessToken | None = get_access_token()
        token = await fhir_connect.user_token(access_token.subject) if access_token and access_token.subject else None
        if token is None:
            raise BackendAuthError("The user has not connected their FHIR server account.")
        return token
    return None


async def get_metadata_access_token() -> str | None:
    """
    The credential for the metadata endpoint. A per-user server's metadata is public
    (SMART), so a user who has not connected yet reads it without a token.
    """
    if configs.effective_server_auth_mode == "user_smart_login":
        try:
            return await get_backend_access_token()
        except BackendAuthError:
            return None
    return await get_backend_access_token()


async def get_async_fhir_client() -> AsyncFHIRClient:
    """
    Get an async FHIR client that authenticates with the backend credential.
    Returns an AsyncFHIRClient instance.
    """
    client_kwargs: Dict = {
        "config": configs,
        "extra_headers": get_default_headers(),
    }
    access_token: str | None = await get_backend_access_token()
    if access_token:
        client_kwargs["access_token"] = access_token
    return await create_async_fhir_client(**client_kwargs)


async def check_tool_scope(tool: str) -> Dict[str, Any] | None:
    """
    Return a forbidden OperationOutcome if the caller's token lacks the tool's scope.
    Over Streamable HTTP the same check answers HTTP 403 before the tool runs; this
    covers the other transports. Returns None when authorization is disabled.
    """
    access_token: AccessToken | None = get_access_token()
    if access_token is None:
        return None
    required: str = tool_scopes(configs)[tool]
    if required in access_token.scopes:
        return None
    return await get_operation_outcome(
        code="forbidden",
        diagnostics=f"The access token does not grant the '{required}' scope required by '{tool}'.",
    )


async def check_tool_access(tool: str, ctx: Context) -> Dict[str, Any] | InputRequiredResult | None:
    """
    The tool's scope check, then, with FHIR_SERVER_AUTH_MODE=user_smart_login, whether
    the user has connected their FHIR server account. If not, the client is asked to
    open a sign-in link (URL-mode elicitation): an `InputRequiredResult` on 2026-07-28,
    a URL elicitation error on earlier protocol versions.
    Returns None when the tool may run.
    """
    if outcome := await check_tool_scope(tool):
        return outcome
    if configs.effective_server_auth_mode != "user_smart_login":
        return None
    access_token: AccessToken | None = get_access_token()
    if access_token is None or not access_token.subject:
        return None
    if await fhir_connect.user_token(access_token.subject):
        return None

    link: str = fhir_connect.connect_link(access_token.subject)
    capabilities = ctx.client_capabilities
    supports_url = bool(capabilities and capabilities.elicitation and capabilities.elicitation.url)
    if is_version_at_least(ctx.protocol_version or "", "2026-07-28"):
        # Every 2026-07-28 request carries the client's capabilities.
        if not supports_url:
            return await get_operation_outcome(code="login", diagnostics=f"{FHIR_CONNECT_MESSAGE} {link}")
        return InputRequiredResult(
            input_requests={"fhir-connect": ElicitRequest(params=ElicitRequestURLParams(message=FHIR_CONNECT_MESSAGE, url=link))}
        )
    if capabilities is not None and not supports_url:
        # An earlier-protocol client that declared it cannot open links.
        return await get_operation_outcome(code="login", diagnostics=f"{FHIR_CONNECT_MESSAGE} {link}")
    # Earlier protocols send capabilities only at initialize, so a stateless request
    # usually carries none. The URL elicitation error (-32042) lets clients that support
    # it show the link; its message repeats the link for clients that do not.
    raise UrlElicitationRequiredError(
        [ElicitRequestURLParams(message=FHIR_CONNECT_MESSAGE, url=link, elicitation_id=secrets.token_urlsafe(16))],
        message=f"{FHIR_CONNECT_MESSAGE} {link}",
    )


def configure_mcp_server() -> MCPServer:
    """
    Configure and instantiate the MCPServer instance.
    With FHIR_MCP_AUTH_MODE=resource_server, every request needs an access token
    issued by the configured IdP for this server's MCP endpoint.
    Transport options are applied when the server is run; see `http_transport_options`.
    Returns an MCPServer instance.
    """
    mcp_kwargs: Dict = {
        "name": "FHIR MCP Server",
        "version": SERVER_VERSION,
        "instructions": "This server implements the HL7 FHIR MCP for secure, standards-based access to FHIR resources",
    }
    if configs.mcp_auth_mode == "resource_server":
        logger.debug("Validating MCP access tokens issued by %s.", configs.mcp_idp_issuer)
        # Passed as strings so the issuer and resource keep their exact spelling
        # (no added trailing slash); RFC 8414/9207 issuer comparison is exact.
        mcp_kwargs["auth"] = AuthSettings(
            issuer_url=configs.mcp_idp_issuer,
            resource_server_url=configs.effective_resource_url,
            # Every tool needs at least the read scope; write and delete scopes are
            # checked per tool (see ToolScopeMiddleware).
            required_scopes=[configs.mcp_read_scope],
            validate_token_resource=True,
        )
        mcp_kwargs["token_verifier"] = create_token_verifier(configs)
    else:
        logger.warning("MCP authorization is disabled.")
    if configs.effective_server_auth_mode == "none":
        logger.warning("Requests to the FHIR server carry no credential (FHIR_SERVER_AUTH_MODE=none).")
    mcp: MCPServer = MCPServer(**mcp_kwargs)
    if configs.effective_server_auth_mode == "user_smart_login":
        global fhir_connect
        fhir_connect = FhirConnect(configs)
        fhir_connect.register_routes(mcp)
    return mcp


def http_transport_options(transport: str) -> Dict[str, Any]:
    """Options for `MCPServer.run()` and `streamable_http_app()` on HTTP transports."""
    options: Dict[str, Any] = {"host": configs.mcp_host}
    if configs.allowed_hosts:
        options["transport_security"] = TransportSecuritySettings(
            allowed_hosts=configs.allowed_hosts,
            allowed_origins=configs.allowed_origins,
        )
    if configs.mcp_max_request_body_bytes:
        options["max_request_body_size"] = configs.mcp_max_request_body_bytes
    if transport == "streamable-http":
        options["json_response"] = True
        options["stateless_http"] = True
    return options


def build_http_app(mcp: MCPServer) -> Starlette:
    """The Streamable HTTP application, with per-tool scope checks when auth is on."""
    app: Starlette = mcp.streamable_http_app(**http_transport_options("streamable-http"))
    if configs.mcp_auth_mode == "resource_server":
        # Appended after the SDK's bearer authentication, so the token is already validated.
        app.user_middleware.append(
            Middleware(
                ToolScopeMiddleware,
                scopes=tool_scopes(configs),
                resource_metadata_url=str(
                    build_resource_metadata_url(AnyHttpUrl(configs.effective_resource_url))
                ),
            )
        )
    return app


def register_mcp_tools(mcp: MCPServer) -> None:
    """
    Register tool functions for the MCPServer instance.
    """
    logger.debug("Registering MCP tools.")

    @mcp.tool(
        description=(
            "Retrieves metadata about a specified FHIR resource type, including its supported search parameters and custom operations. "
            "This tool MUST always be invoked before performing any resource operation (such as search, read, create, update, or delete) "
            "to discover the valid searchParams and operations permitted for that resource type. "
            "Do not use this tool to fetch actual resources."
        )
    )
    async def get_capabilities(
        type: Annotated[
            str,
            Field(
                description=(
                    "The FHIR resource type name. Must exactly match one of the core or "
                    "profile-defined resource types as per the FHIR specification."
                ),
                examples=["Patient", "Observation", "Encounter"],
            ),
        ],
    ) -> Annotated[
        Dict[str, Any],
        Field(
            description=(
                "A dictionary containing: "
                "'type': The requested resource type (if supported by the system) or empty. "
                "'searchParam': A mapping of FHIR search parameter names to their descriptions. Each key is a parameter name "
                "(e.g., family, _id, _lastUpdated), and each value is a string describing the parameter's meaning and usage constraints. "
                "'operation': A mapping of custom FHIR operation names to their descriptions. Each key is an operation name "
                "(e.g., $validate), and each value is a string explaining the operation's purpose and usage. "
                "'interaction': A list of supported interactions for the resource type (e.g., read, search-type, create). "
                "'searchInclude': A list of supported _include parameters for the resource type, indicating which related resources can be included. "
                "'searchRevInclude': A list of supported _revinclude parameters for the resource type, indicating which reverse-included resources can be included."
            )
        ),
    ]:
        if outcome := await check_tool_scope("get_capabilities"):
            return outcome
        try:
            logger.debug(f"Invoked with resource_type='{type}'")
            if outcome := await validate_resource_type(type):
                return outcome
            data: Dict[str, Any] = await get_capability_statement(
                configs.metadata_url, await get_metadata_access_token()
            )
            for resource in data["rest"][0]["resource"]:
                if resource.get("type") == type:
                    logger.info(
                        f"Resource type '{type}' found in the CapabilityStatement."
                    )
                    return {
                        "type": resource.get("type"),
                        "searchParam": trim_resource_capabilities(
                            resource.get("searchParam", [])
                        ),
                        "operation": trim_resource_capabilities(
                            resource.get("operation", [])
                        ),
                        "interaction": resource.get("interaction", []),
                        "searchInclude": resource.get("searchInclude", []),
                        "searchRevInclude": resource.get("searchRevInclude", []),
                    }
            logger.info(f"Resource type '{type}' not found in the CapabilityStatement.")
            return await get_operation_outcome(
                code="not-supported",
                diagnostics=f"The interaction, operation, resource or profile {type} is not supported.",
            )
        except Exception as ex:
            logger.exception(
                f"Error while executing the FHIR metadata interaction for resource_type '{type}'. Caused by, ",
                exc_info=ex,
            )
        return await get_operation_outcome_exception()

    @mcp.tool(
        description=(
            "Executes a standard FHIR `search` interaction on a given resource type, returning a bundle or list of matching resources. "
            "Use this when you need to query for multiple resources based on one or more search-parameters. "
            "Do not use this tool for create, update, or delete operations, and be aware that large result sets may be paginated by the FHIR server. "
            "Optionally specify fields to filter the response to only the data your task needs (e.g., Patient.birthDate, Observation.valueQuantity)."
        )
    )
    async def search(
        ctx: Context,
        type: Annotated[
            str,
            Field(
                description="The FHIR resource type name. Must exactly match one of the resource types supported by the server",
                examples=["MedicationRequest", "Condition", "Procedure"],
            ),
        ],
        searchParam: Annotated[
            Dict[str, str | List[str]],
            Field(
                description=(
                    "A mapping of FHIR search parameter names to their values. "
                    "Only include parameters supported for the resource type, as listed by `get_capabilities`."
                ),
                examples=[
                    '{"family": "Smith"}',
                    '{"date": ["ge1970-01-01", "lt2000-01-01"]}',
                ],
            ),
        ],
        response_filter_fhirpaths: Annotated[
            List[str],
            Field(
                description="Specify FHIRPath expressions to filter the response to only the data you need. Omitting returns the full resource.",
                examples=[
                    [
                        "Condition.code",
                        "Condition.clinicalStatus",
                        "Bundle.link.where(relation='next').url",
                    ]
                ],
            ),
        ] = [],
    ) -> Annotated[
        list[Dict[str, Any]] | Dict[str, Any],
        Field(
            description="A dictionary containing the full FHIR resource instance matching the search criteria."
        ),
    ]:
        if outcome := await check_tool_access("search", ctx):
            return outcome
        try:
            logger.debug(f"Invoked with type='{type}' and searchParam={searchParam}")
            if not type:
                logger.error(
                    "Unable to perform search operation: 'type' is a mandatory field."
                )
                return await get_operation_outcome_required_error("type")
            if outcome := await validate_resource_type(type):
                return outcome

            client: AsyncFHIRClient = await get_async_fhir_client()
            async_resources: list[Any] = (
                await client.resources(type).search(Raw(**searchParam)).fetch_raw()
            )
            logger.debug("Async resources fetched: %s", async_resources)
            return filter_response_fields(async_resources, response_filter_fhirpaths)
        except ValueError as ex:
            logger.exception(
                f"User does not have permission to perform FHIR '{type}' resource search operation. Caused by, ",
                exc_info=ex,
            )
            return await get_operation_outcome(
                code="forbidden",
                diagnostics="The user does not have the rights to perform search operation.",
            )
        except OperationOutcome as ex:
            logger.exception(
                f"FHIR server returned an OperationOutcome error while searching the resource: '{type}', Caused by,",
                exc_info=ex,
            )
            return ex.resource["issue"] or await get_operation_outcome_exception()
        except Exception as ex:
            logger.exception(
                f"An unexpected error occurred during the FHIR search operation for resource: '{type}'. Caused by, ",
                exc_info=ex,
            )
        return await get_operation_outcome_exception()

    @mcp.tool(
        description=(
            "Performs a FHIR `read` interaction to retrieve a single resource instance by its type and resource ID, "
            "optionally refining the response with search parameters or custom operations. "
            "Use it when you know the exact resource ID and require that one resource; do not use it for bulk queries. "
            "If additional query-level parameters or operations are needed (e.g., _elements or $validate), include them in searchParam or operation. "
            "Optionally specify fields to filter the response to only the data your task needs (e.g., Patient.birthDate, Observation.valueQuantity)."
        )
    )
    async def read(
        ctx: Context,
        type: Annotated[
            str,
            Field(
                description="The FHIR resource type name. Must exactly match one of the resource types supported by the server.",
                examples=["DiagnosticReport", "AllergyIntolerance", "Immunization"],
            ),
        ],
        id: Annotated[
            str,
            Field(description="The logical ID of a specific FHIR resource instance."),
        ],
        searchParam: Annotated[
            Dict[str, str | List[str]],
            Field(
                description=(
                    "A mapping of FHIR search parameter names to their desired values. "
                    "These parameters refine queries for operation-specific query qualifiers. "
                    "Only parameters exposed by `get_capabilities` for that resource type are valid."
                ),
                examples=['{"device-name": "glucometer", "identifier": ["12345"]}'],
            ),
        ] = {},
        operation: Annotated[
            str,
            Field(
                description=(
                    "The name of a custom FHIR operation or extended query defined for the resource "
                    "must match one of the operation names returned by `get_capabilities`."
                ),
                examples=["$everything"],
            ),
        ] = "",
        response_filter_fhirpaths: Annotated[
            List[str],
            Field(
                description="Specify FHIRPath expressions to filter the response to only the data you need. Omitting returns the full resource. Bundle.* expressions supported with $everything.",
                examples=[
                    ["Patient.name", "Patient.birthDate", "Observation.valueQuantity"]
                ],
            ),
        ] = [],
    ) -> Annotated[
        Dict[str, Any],
        Field(
            description="A dictionary containing the single FHIR resource instance of the requested type and id."
        ),
    ]:
        if outcome := await check_tool_access("read", ctx):
            return outcome
        try:
            logger.debug(
                f"Invoked with type='{type}', id={id}, searchParam={searchParam}, and operation={operation}"
            )
            if not type:
                logger.error(
                    "Unable to perform read operation: 'type' is a mandatory field."
                )
                return await get_operation_outcome_required_error("type")
            if outcome := await validate_inputs(type, id, operation):
                return outcome

            client: AsyncFHIRClient = await get_async_fhir_client()
            bundle: dict = await client.resource(resource_type=type, id=id).execute(
                operation=operation or "", method="GET", params=searchParam
            )

            entries = await extract_bundle_resources(bundle=bundle)
            return filter_response_fields(entries, response_filter_fhirpaths)
        except ResourceNotFound as ex:
            logger.error(
                f"Resource of type '{type}' with id '{id}' not found. Caused by, ",
                exc_info=ex,
            )
            return await get_operation_outcome(
                code="not-found",
                diagnostics=f"The resource of type '{type}' with id '{id}' was not found.",
            )
        except ValueError as ex:
            logger.exception(
                f"User does not have permission to perform FHIR '{type}' resource read operation. Caused by, ",
                exc_info=ex,
            )
            return await get_operation_outcome(
                code="forbidden",
                diagnostics="The user does not have the rights to perform read operation.",
            )
        except OperationOutcome as ex:
            logger.exception(
                f"FHIR server returned an OperationOutcome error while reading the resource: '{type}', Caused by,",
                exc_info=ex,
            )
            return ex.resource["issue"] or await get_operation_outcome_exception()
        except Exception as ex:
            logger.exception(
                f"An unexpected error occurred during the FHIR read operation for resource: '{type}'. Caused by, ",
                exc_info=ex,
            )
        return await get_operation_outcome_exception()

    @mcp.tool(
        description=(
            "Creates a new FHIR resource on the server (FHIR `create` interaction: POST [base]/{type}). "
            "Use it to add a record that does not exist yet, such as a new Patient, Observation or Appointment. "
            "Do not use it to change an existing resource; use `update` for that. "
            "The server assigns a new id on every call, so repeating a call creates duplicates; "
            "use `search` first if the record may already exist. "
            "On success it returns the resource as stored, including the server-assigned `id` and `meta`. "
            "On failure it returns OperationOutcome issues whose `diagnostics` explain what to fix, "
            "such as a missing required element or an invalid code."
        )
    )
    async def create(
        ctx: Context,
        type: Annotated[
            str,
            Field(
                description="The FHIR resource type name. Must exactly match one of the resource types supported by the server.",
                examples=["Device", "CarePlan", "Goal"],
            ),
        ],
        payload: Annotated[
            Dict[str, Any],
            Field(
                description=(
                    "The JSON body of the new resource. Include every element the resource's profile marks as required, "
                    "or the server rejects the request. Set `resourceType` to the same value as `type`; "
                    "if omitted, it defaults to `type`. Leave out `id` and `meta`, which the server assigns."
                ),
                examples=[
                    {
                        "resourceType": "Patient",
                        "name": [{"family": "Smith", "given": ["John"]}],
                        "gender": "male",
                        "birthDate": "1980-04-12",
                    }
                ],
            ),
        ],
        searchParam: Annotated[
            Dict[str, str | List[str]],
            Field(
                description=(
                    "A mapping of FHIR search parameter names to their desired values. "
                    "These parameters refine queries for operation-specific query qualifiers. "
                    "Only parameters exposed by `get_capabilities` for that resource type are valid."
                ),
                examples=['{"address-city": "Boston", "address-state": ["NY"]}'],
            ),
        ] = {},
        operation: Annotated[
            str,
            Field(
                description=(
                    "The name of a custom FHIR operation or extended query defined for the resource"
                    "Must match one of the operation names returned by `get_capabilities`."
                ),
                examples=["$evaluate"],
            ),
        ] = "",
    ) -> Annotated[
        Dict[str, Any],
        Field(
            description=(
                "A dictionary containing the newly created FHIR resource, including server-assigned fields "
                "(id, meta.versionId, meta.lastUpdated, and any server-added extensions). Reflects exactly what was persisted."
            )
        ),
    ]:
        if outcome := await check_tool_access("create", ctx):
            return outcome
        try:
            logger.debug(
                f"Invoked with type='{type}', payload={payload}, searchParam={searchParam}, and operation={operation}"
            )
            if not type:
                logger.error(
                    "Unable to perform create operation: 'type' is a mandatory field."
                )
                return await get_operation_outcome_required_error("type")
            if outcome := await validate_inputs(type, operation=operation):
                return outcome

            client: AsyncFHIRClient = await get_async_fhir_client()
            bundle: dict = await client.resource(resource_type=type).execute(
                operation=operation or "",
                data={"resourceType": type, **payload},
                params=searchParam,
            )

            return await extract_bundle_resources(bundle=bundle)
        except ValueError as ex:
            logger.exception(
                f"User does not have permission to perform FHIR '{type}' resource create operation. Caused by, ",
                exc_info=ex,
            )
            return await get_operation_outcome(
                code="forbidden",
                diagnostics="The user does not have the rights to perform create operation.",
            )
        except OperationOutcome as ex:
            logger.exception(
                f"FHIR server returned an OperationOutcome error while creating the resource: '{type}', Caused by,",
                exc_info=ex,
            )
            return ex.resource["issue"] or await get_operation_outcome_exception()
        except Exception as ex:
            logger.exception(
                f"An unexpected error occurred during the FHIR create operation for resource: '{type}'. Caused by, ",
                exc_info=ex,
            )
        return await get_operation_outcome_exception()

    @mcp.tool(
        description=(
            "Replaces the full content of an existing FHIR resource (FHIR `update` interaction: PUT [base]/{type}/{id}). "
            "Use it to correct or complete a record whose logical id you already know. "
            "Elements left out of the payload are removed from the stored resource, not kept, "
            "so first get the current version with `read`, change what you need, and send the whole resource back. "
            "Do not use it to add new records; use `create` for that. "
            "On success it returns the resource as stored, with an updated `meta.versionId` and `meta.lastUpdated`. "
            "On failure it returns OperationOutcome issues whose `diagnostics` explain what to fix."
        )
    )
    async def update(
        ctx: Context,
        type: Annotated[
            str,
            Field(
                description="The FHIR resource type name. Must exactly match one of the resource types supported by the server.",
                examples=["Location", "Organization", "Coverage"],
            ),
        ],
        id: Annotated[
            str,
            Field(description="The logical ID of a specific FHIR resource instance."),
        ],
        payload: Annotated[
            Dict[str, Any],
            Field(
                description=(
                    "The complete new content of the resource. It replaces the stored version entirely, "
                    "so include every required element and all existing data you want to keep. "
                    "Set `resourceType` to the same value as `type`; if omitted, it defaults to `type`. "
                    "The body `id` is always set to the `id` argument, overriding any value given here."
                ),
                examples=[
                    {
                        "resourceType": "Organization",
                        "id": "org-1001",
                        "active": True,
                        "name": "Boston General Hospital",
                        "telecom": [{"system": "phone", "value": "+1-617-555-0100"}],
                    }
                ],
            ),
        ],
        searchParam: Annotated[
            Dict[str, str | List[str]],
            Field(
                description=(
                    "A mapping of FHIR search parameter names to their desired values. "
                    "These parameters refine queries for operation-specific query qualifiers. "
                    "Only parameters exposed by `get_capabilities` for that resource type are valid. "
                ),
                examples=['{"patient":"Patient/54321","relationship":["father"]}'],
            ),
        ] = {},
        operation: Annotated[
            str,
            Field(
                description=(
                    "The name of a custom FHIR operation or extended query defined for the resource"
                    "Must match one of the operation names returned by `get_capabilities`."
                ),
                examples=["$lastn"],
            ),
        ] = "",
    ) -> Annotated[
        Dict[str, Any],
        Field(description="A dictionary containing the updated FHIR resource"),
    ]:
        if outcome := await check_tool_access("update", ctx):
            return outcome
        try:
            logger.debug(
                f"Invoked with type='{type}', id={id}, payload={payload}, searchParam={searchParam}, and operation={operation}"
            )
            if not type:
                logger.error(
                    "Unable to perform update operation: 'type' is a mandatory field."
                )
                return await get_operation_outcome_required_error("type")
            if outcome := await validate_inputs(type, id, operation):
                return outcome

            client: AsyncFHIRClient = await get_async_fhir_client()
            bundle: dict = await client.resource(resource_type=type, id=id).execute(
                operation=operation or "",
                method="PUT",
                data={"resourceType": type, **payload, "id": id},
                params=searchParam,
            )
            return await extract_bundle_resources(bundle=bundle)
        except ValueError as ex:
            logger.exception(
                f"User does not have permission to perform FHIR '{type}' resource update operation. Caused by, ",
                exc_info=ex,
            )
            return await get_operation_outcome(
                code="forbidden",
                diagnostics="The user does not have the rights to perform update operation.",
            )
        except OperationOutcome as ex:
            logger.exception(
                f"FHIR server returned an OperationOutcome error while updating the resource: '{type}', Caused by,",
                exc_info=ex,
            )
            return ex.resource["issue"] or await get_operation_outcome_exception()
        except Exception as ex:
            logger.exception(
                f"An unexpected error occurred during the FHIR update operation for resource: '{type}'. Caused by, ",
                exc_info=ex,
            )
        return await get_operation_outcome_exception()

    @mcp.tool(
        description=(
            "Execute a FHIR `delete` interaction on a specific resource instance. "
            "Use this tool when you need to remove a single resource identified by its logical ID or optionally filtered by search parameters. "
            "The optional `id` parameter must match an existing resource instance when present. "
            "If you include `searchParam`, the server will perform a conditional delete, deleting the resource only if it matches the given criteria. "
            "If you supply `operation`, it will execute the named FHIR operation (e.g., `$expunge`) on the resource. "
            "This tool returns a FHIR `OperationOutcome` describing success or failure of the deletion."
        )
    )
    async def delete(
        ctx: Context,
        type: Annotated[
            str,
            Field(
                description="The FHIR resource type name. Must exactly match one of the resource types supported by the server.",
                examples=["ServiceRequest", "Appointment", "HealthcareService"],
            ),
        ],
        id: Annotated[
            str,
            Field(description="The logical ID of a specific FHIR resource instance."),
        ] = "",
        searchParam: Annotated[
            Dict[str, str | List[str]],
            Field(
                description=(
                    "A mapping of FHIR search parameter names to their desired values. "
                    "These parameters refine queries for operation-specific query qualifiers. "
                    "Only parameters exposed by `get_capabilities` for that resource type are valid. "
                ),
                examples=['{"category": "laboratory", "status": ["active"]}'],
            ),
        ] = {},
        operation: Annotated[
            str,
            Field(
                description=(
                    "The name of a custom FHIR operation or extended query defined for the resource"
                    "Must match one of the operation names returned by `get_capabilities`."
                ),
                examples=["$expand"],
            ),
        ] = "",
    ) -> Annotated[
        Dict[str, Any],
        Field(
            description="A dictionary containing the confirmation of deletion or details on why deletion failed."
        ),
    ]:
        if outcome := await check_tool_access("delete", ctx):
            return outcome
        try:
            logger.debug(
                f"Invoked with type='{type}', id={id}, searchParam={searchParam}, and operation={operation}"
            )
            if not type:
                logger.error(
                    "Unable to perform delete operation: 'type' is a mandatory field."
                )
                return await get_operation_outcome_required_error("type")
            if outcome := await validate_inputs(type, id, operation):
                return outcome
            if not id and not searchParam:
                logger.error(
                    "Unable to perform delete operation: 'id' or 'searchParam' is required."
                )
                return await get_operation_outcome_required_error("id")

            client: AsyncFHIRClient = await get_async_fhir_client()
            bundle = await client.resource(resource_type=type, id=id).execute(
                operation=operation or "", method="DELETE", params=searchParam
            )
            if isinstance(bundle, Dict):
                return await extract_bundle_resources(bundle=bundle)
            return await get_operation_outcome(
                severity="information",
                code="SUCCESSFUL_DELETE",
                diagnostics="Successfully deleted resource(s).",
            )
        except ValueError as ex:
            logger.exception(
                f"User does not have permission to perform FHIR '{type}' resource delete operation. Caused by, ",
                exc_info=ex,
            )
            return await get_operation_outcome(
                code="forbidden",
                diagnostics="The user does not have the rights to perform delete operation.",
            )
        except OperationOutcome as ex:
            logger.exception(
                f"FHIR server returned an OperationOutcome error while deleting the resource: '{type}', Caused by,",
                exc_info=ex,
            )
            return ex.resource["issue"] or await get_operation_outcome_exception()
        except Exception as ex:
            logger.exception(
                f"An unexpected error occurred during the FHIR delete operation for resource: '{type}'. Caused by, ",
                exc_info=ex,
            )
        return await get_operation_outcome_exception()

    @mcp.tool(
        description=(
            "Retrieves the authenticated user's FHIR profile. "
            "Use this tool when you need to access the current user's demographic and contact details."
        )
    )
    async def get_user(ctx: Context) -> Annotated[
        list[Dict[str, Any]] | Dict[str, Any],
        Field(
            description="A dictionary containing the authenticated user's demographic information such as 'id', 'name', and 'birthDate'."
        ),
    ]:
        if outcome := await check_tool_access("get_user", ctx):
            return outcome
        try:
            logger.debug("Retrieving authenticated user's profile.")

            # The validated access token identifies the user's FHIR resource
            access_token: AccessToken | None = get_access_token()
            if not access_token:
                logger.debug("Unauthorized access attempt to get_user.")
                return {}

            fhir_user = IDToken(fhirUser=(access_token.claims or {}).get("fhirUser"))
            resource_id = fhir_user.resource_id
            resource_type = fhir_user.resource_type

            if not resource_id or not resource_type:
                logger.debug("The access token carries no usable fhirUser claim.")
                return {}
            if outcome := await validate_inputs(resource_type, resource_id):
                return outcome

            logger.debug(f"Fetching FHIR resource: {resource_type}/{resource_id}")

            # Fetch user's FHIR resource
            client: AsyncFHIRClient = await get_async_fhir_client()
            resource: Dict[str, Any] = await client.get(
                resource_type_or_resource_or_ref=resource_type, id_or_ref=resource_id
            )

            # Build response with only available fields
            profile: Dict[str, Any] = build_user_profile(resource)

            logger.debug(
                f"Successfully retrieved profile for user: {resource_type}/{resource_id}"
            )
            return profile

        except ValueError as ex:
            logger.exception(
                "Authorization error occurred while reading user resource. Caused by, ",
                exc_info=ex,
            )
            return await get_operation_outcome(
                code="forbidden",
                diagnostics="The user does not have the rights to perform read operations.",
            )

        except OperationOutcome as ex:
            logger.exception(
                "FHIR server error occurred while reading user resource. Caused by, ",
                exc_info=ex,
            )
            return ex.resource.get("issue") or await get_operation_outcome_exception()

        except Exception as ex:
            logger.exception(
                "Unexpected error occurred while reading user resource. Caused by, ",
                exc_info=ex,
            )
        return await get_operation_outcome_exception()


@click.command()
@click.option(
    "--transport",
    type=click.Choice(["stdio", "sse", "streamable-http"]),
    default="streamable-http",
    show_default=True,
    help="Transport protocol to use",
)
@click.option(
    "--log-level",
    type=click.Choice(["DEBUG", "INFO", "WARN", "ERROR"], case_sensitive=False),
    default="INFO",
    show_default=True,
    help="Log level to use",
)
def main(transport, log_level) -> int:
    """
    FHIR MCP Server - helping you expose any FHIR Server or API as a MCP Server.
    """

    logging.basicConfig(
        level=getattr(logging, log_level.upper()),
        format="[%(asctime)s] %(levelname)s {%(name)s.%(funcName)s:%(lineno)d} - %(message)s",
    )
    try:
        if transport == "stdio" and configs.mcp_auth_mode != "disabled":
            # The MCP authorization flow is for HTTP transports; stdio relies on the
            # local process boundary and takes its FHIR credential from the environment.
            logger.info("MCP authorization does not apply to the stdio transport.")
            configs.mcp_auth_mode = "disabled"
        configs.validate_for_startup()
        mcp: MCPServer = configure_mcp_server()
        register_mcp_tools(mcp=mcp)
        logger.info(f"Starting FHIR MCP server with {transport} transport")
        if transport == "stdio":
            mcp.run(transport=transport)
        elif transport == "streamable-http":
            uvicorn.run(
                build_http_app(mcp),
                host=configs.mcp_host,
                port=configs.mcp_port,
                log_level=log_level.lower().replace("warn", "warning"),
            )
        else:
            mcp.run(
                transport=transport,
                port=configs.mcp_port,
                **http_transport_options(transport),
            )
    except ConfigurationError as ex:
        logger.error("Invalid configuration: %s", ex)
        return 1
    except Exception as ex:
        logger.error(
            "Unable to run the FHIR MCP server. Caused by, %s", ex, exc_info=True
        )
        return 1
    return 0

