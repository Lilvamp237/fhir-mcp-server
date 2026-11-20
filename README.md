# Model Context Protocol (MCP) Server for Fast Healthcare Interoperability Resources (FHIR) APIs

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://github.com/wso2/fhir-mcp-server/blob/main/LICENSE)
[![Get Support on Stack Overflow](https://img.shields.io/badge/stackoverflow-wso2-orange)](https://stackoverflow.com/questions/tagged/wso2)
[![Join the community on Discord](https://img.shields.io/badge/Join%20us%20on-Discord-%23e01563.svg)](https://discord.com/invite/wso2)
[![X](https://img.shields.io/twitter/follow/wso2.svg?style=social&label=Follow)](https://twitter.com/intent/follow?screen_name=wso2)
[![Listed on Spark](https://spark.entire.vc/badges/listed.svg)](https://spark.entire.vc/assets/vb-fhir?utm_source=github&utm_medium=readme)
[![Install via Spark](https://spark.entire.vc/badges/vb-fhir/install.svg)](https://spark.entire.vc/assets/vb-fhir?utm_source=github&utm_medium=readme)

## Table of Contents
- [Model Context Protocol (MCP) Server for Fast Healthcare Interoperability Resources (FHIR) APIs](#model-context-protocol-mcp-server-for-fast-healthcare-interoperability-resources-fhir-apis)
  - [Table of Contents](#table-of-contents)
  - [Overview](#overview)
  - [Demo](#demo)
    - [Demo with HAPI FHIR server](#demo-with-hapi-fhir-server)
    - [Demo with EPIC Sandbox](#demo-with-epic-sandbox)
  - [Core Features](#core-features)
  - [Prerequisites](#prerequisites)
  - [Installation](#installation)
    - [Installing using PyPI Package](#installing-using-pypi-package)
    - [Installing from Source](#installing-from-source)
    - [Installing using Docker](#installing-using-docker)
        - [Running the MCP Server with Docker](#running-the-mcp-server-with-docker)
        - [Using Docker Compose with WSO2 FHIR Server](#using-docker-compose-with-wso2-fhir-server)
          - [Configuring any FHIR Server](#configuring-any-fhir-server)
  - [Integration with MCP Clients](#integration-with-mcp-clients)
    - [VS Code](#vs-code)
    - [Claude Desktop](#claude-desktop)
    - [MCP Inspector](#mcp-inspector)
  - [Configuration](#configuration)
    - [CLI Options](#cli-options)
    - [Environment Variables](#environment-variables)
    - [Authorization](#authorization)
  - [Tools](#tools)
  - [Development \& Testing](#development--testing)
    - [Installing Development Dependencies](#installing-development-dependencies)
    - [Running Tests](#running-tests)


## Overview

The FHIR MCP Server is a Model Context Protocol (MCP) server that provides seamless integration with FHIR APIs. Designed for developers, integrators, and healthcare innovators, this server acts as a bridge between modern AI/LLM tools and healthcare data, making it easy to search, retrieve, and analyze clinical information.

## Demo

### Demo with HAPI FHIR server

This video showcases the MCP server's functionality when connected to a public [HAPI FHIR server](https://hapi.fhir.org/). This example showcases direct interaction with an open FHIR server that does not require an authorization flow.

https://github.com/user-attachments/assets/cc6ac87e-8329-4da4-a090-2d76564a3abf

### Demo with EPIC Sandbox

This video showcases the MCP server's capabilities within the [Epic EHR ecosystem](https://open.epic.com/). It demonstrates the complete OAuth 2.0 Authorization Code Grant flow.

https://github.com/user-attachments/assets/96b433f1-3e53-4564-8466-65ab48d521de

## Core Features

- **MCP-compatible transport**: Serves FHIR via stdio, SSE, or streamable HTTP

- **IdP-based authorization (MCP 2026-07-28)**: Acts as an OAuth resource server. Clients sign in with your identity provider (for example ThunderID, WSO2 Identity Server or Keycloak), and every request's token is checked for issuer, audience and the scope each tool needs

- **Response Filtering using FHIRPath**: Filter resources and bundles returned by `read` and `search` operations using custom FHIRPath expressions to retrieve only the fields needed for the task, reducing payload sizes.

- **Tool integration**: Integratable with any MCP client such as VS Code, Claude Desktop, and MCP Inspector

## Prerequisites

- Python 3.8+
- [uv](https://github.com/astral-sh/uv) (for dependency management)
- An accessible FHIR API server.

## Installation

You can use the FHIR MCP Server by installing our Python package, or by cloning this repository.

### Installing using PyPI Package

1. **Configure Environment Variables:**
    
    To run the server, you must set `FHIR_SERVER_BASE_URL`.

    * **Authorization (default):** Set `FHIR_MCP_IDP_ISSUER` to your identity provider's issuer URL. Clients sign in with the IdP, and the server validates their tokens. See [Authorization](#authorization).
    * **Local development without sign-in:** Set `FHIR_MCP_AUTH_MODE` to `disabled`.

    By default, the MCP server runs on **[http://localhost:8000](http://localhost:8000)**, and you can customize the host and port using `FHIR_MCP_HOST` and `FHIR_MCP_PORT`.


    You can set these by exporting them as environment variables like below or by creating a `.env` file (referencing `.env.example`).

    ```bash 
    export FHIR_SERVER_BASE_URL=""
    export FHIR_MCP_IDP_ISSUER=""

    export FHIR_MCP_HOST="localhost"
    export FHIR_MCP_PORT="8000"
    ```

2. **Install the PyPI package and run the server**

    ```bash
    uvx fhir-mcp-server
    ```

### Installing from Source

1. **Clone the repository:**
    ```bash
    git clone <repository_url>
    cd <repository_directory>
    ```

2. **Create a virtual environment and install dependencies:**
    ```bash
    uv venv
    source .venv/bin/activate
    uv pip sync requirements.txt
    ```
    Or with pip:
    ```bash
    python -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt
    ```

3. **Configure Environment Variables:**
    Copy the example file and customize if needed:
    ```bash
    cp .env.example .env
    ```

4. **Run the server:**
    ```bash
    uv run fhir-mcp-server
    ```

### Installing using Docker

#### Running the MCP Server with Docker

You can run the MCP server using Docker for a consistent, isolated environment. 

>Note on **Authorization**: For a local Docker or Docker Compose run without an identity provider, set `FHIR_MCP_AUTH_MODE=disabled`. Otherwise set `FHIR_MCP_IDP_ISSUER` (see [Authorization](#authorization)); from inside a container, use an IdP address the container can reach, such as `host.docker.internal`.

1. Build the Docker Image or pull the docker image from the container registry:

    * Build from source:
        ```bash
        docker build -t fhir-mcp-server .
        ```
    * Pull from GitHub Container Registry:
        ```bash
        docker pull ghcr.io/wso2/fhir-mcp-server:latest
        ```
        `latest` is built from the `main` branch. To use a release, pull its version tag instead, for example `ghcr.io/wso2/fhir-mcp-server:0.11.0`.

2. Configure Environment Variables

    Copy the example environment file and edit as needed:

    ```bash
    cp .env.example .env
    # Edit .env to set your FHIR server, client credentials, etc.
    ```

    Alternatively, you can pass environment variables directly with `-e` flags or use Docker secrets for sensitive values. See the [Configuration](#configuration) section for details on available environment variables.

3. Run the Container

    ```bash
    docker run --env-file .env -p 8000:8000 ghcr.io/wso2/fhir-mcp-server:latest
    ```

    This will start the server and expose it on port 8000. Adjust the port mapping as needed.

#### Using Docker Compose with WSO2 FHIR Server

For a quick setup that includes both the FHIR MCP server and the [WSO2 FHIR Server](https://github.com/wso2/fhir-server) (with PostgreSQL), use the provided `docker-compose.yml`. This sets up an instant development environment for testing FHIR operations.

1. **Prerequisites:**
   - Docker and Docker Compose installed.

2. **Set the Database Credentials:**

   Copy the example environment file and set `DB_PASSWORD` in `.env`. If you already have a `.env`, add the `DB_USER`, `DB_PASSWORD` and `DB_NAME` lines from `.env.example` to it instead.

   ```bash
   cp .env.example .env
   ```

   Docker Compose reads `.env` automatically. `DB_USER` and `DB_NAME` default to `postgres` if not set. `.env` is excluded from Git; use a secrets manager for production deployments.

3. **Run the Stack:**

   ```bash
   docker compose up -d
   ```

   This command will:
   - Start a PostgreSQL database container.
   - Launch the FHIR Server (connected to PostgreSQL) listening on http://localhost:8080/fhir/r4.
   - Build and run the FHIR MCP server container listening on http://localhost:8000, with `FHIR_SERVER_BASE_URL` set to http://wso2-fhir-server:8080/fhir/r4.

4. **Access the Services:**
   - FHIR MCP Server: http://localhost:8000
   - FHIR Server: http://localhost:8080/fhir/r4
   - To stop run `docker compose down`.

   `docker compose down` removes the database data; use `docker compose stop` to keep it between runs.

5. **Configure Additional Environment Variables:**

   If you need to customize OAuth or other settings, adjust the env variables in the `docker-compose.yml`. The compose file sets basic configuration; refer to the [Configuration](#configuration) section for full options.

#### Configuring any FHIR Server

To use a different FHIR server, run the FHIR MCP server container with `FHIR_SERVER_BASE_URL` pointing to that server. The example below uses the [HAPI FHIR server](https://hapi.fhir.org/). Stop the default stack first if it's already running (`docker compose down`), as both use ports 8000 and 8080.

1. **Create a network and start the HAPI FHIR server:**

   ```bash
   docker network create fhir-network
   docker run -d --name hapi-fhir-server --network fhir-network -p 127.0.0.1:8080:8080 docker.io/hapiproject/hapi:v8.4.0-2
   ```

   HAPI FHIR listens on http://localhost:8080/fhir. This uses its built-in database, so data is lost when the container is removed.

2. **Build and run the FHIR MCP server container:**

   ```bash
   docker build -t fhir-mcp-server .
   docker run -d --name fhir-mcp-server-hapi --network fhir-network -p 127.0.0.1:8000:8000      -e FHIR_SERVER_BASE_URL=http://hapi-fhir-server:8080/fhir      -e FHIR_MCP_HOST=0.0.0.0      -e FHIR_MCP_AUTH_MODE=disabled      fhir-mcp-server
   ```

   The FHIR MCP server listens on http://localhost:8000.

3. **Stop and clean up:**

   ```bash
   docker rm -f fhir-mcp-server-hapi hapi-fhir-server
   docker network rm fhir-network
   ```

For any other FHIR server, set `FHIR_SERVER_BASE_URL` to its base URL. If the FHIR server runs on your host rather than in Docker, use `http://host.docker.internal:<port>/...` instead of `localhost`. 
On Linux, also add `--add-host=host.docker.internal:host-gateway` to the `docker run` command, and make sure the FHIR server listens on an address reachable from containers (for example `0.0.0.0`, not only `127.0.0.1`).

## Integration with MCP Clients

The FHIR MCP Server is designed for seamless integration with various MCP clients.

### VS Code

[![Install in VS Code](https://img.shields.io/badge/VS_Code-Install_Server-0098FF?style=flat-square&logo=visualstudiocode&logoColor=white)](https://insiders.vscode.dev/redirect/mcp/install?name=fhir&config=%7B%22type%22%3A%22http%22%2C%22url%22%3A%22http%3A%2F%2Flocalhost%3A8000%2Fmcp%2F%22%7D)
[![Install in VS Code Insiders](https://img.shields.io/badge/VS_Code_Insiders-Install_Server-24bfa5?style=flat-square&logo=visualstudiocode&logoColor=white)](https://insiders.vscode.dev/redirect/mcp/install?name=fhir&config=%7B%22type%22%3A%22http%22%2C%22url%22%3A%22http%3A%2F%2Flocalhost%3A8000%2Fmcp%2F%22%7D)

Add the following JSON block to your MCP configuration file in VS Code (> V1.104). You can do this by pressing Ctrl + Shift + P and typing `MCP: Open User Configuration`.

<table>
<tr><th>Streamable HTTP</th><th>STDIO</th><th>SSE</th></tr>
<tr valign=top>
<td>

```json
"servers": {
    "fhir": {
        "type": "http",
        "url": "http://localhost:8000/mcp",
    }
}
```
</td>

<td>

```json
"servers": {
    "fhir": {
        "command": "uv",
        "args": [
            "--directory",
            "/path/to/fhir-mcp-server",
            "run",
            "fhir-mcp-server",
            "--transport",
            "stdio"
        ],
        "env": {
            "FHIR_SERVER_BASE_URL": "https://hapi.fhir.org/baseR4"
        }
    }
}
```
</td>

<td>

```json
"servers": {
    "fhir": {
        "type": "sse",
        "url": "http://localhost:8000/sse",
    }
}
```
</td>
</tr>
</table>

### Claude Desktop
Add the following JSON block to your Claude Desktop settings to connect to your local MCP server. 
 - Launch the Claude Desktop app, click on the Claude menu in the top bar, and select "Settings…".
 - In the Settings pane, click “Developer” in the left sidebar. Then click "Edit Config". This will open your configuration file in your file system. If it doesn’t exist yet, Claude will create one automatically at:
    - macOS: ~/Library/Application Support/Claude/claude_desktop_config.json
    - Windows: %APPDATA%\Claude\claude_desktop_config.json
 - Open the claude_desktop_config.json file in any text editor. Replace its contents with the following JSON block to register the MCP server:

<table>
<tr><th>Streamable HTTP</th><th>STDIO</th><th>SSE</th></tr>
<tr valign=top>
<td>

```json
{
    "mcpServers": {
        "fhir": {
            "command": "npx",
            "args": [
                "-y",
                "mcp-remote",
                "http://localhost:8000/mcp"
            ]
        }
    }
}
```
</td>
<td>

```json
{
    "mcpServers": {
        "fhir": {
            "command": "uv",
            "args": [
                "--directory",
                "/path/to/fhir-mcp-server",
                "run",
                "fhir-mcp-server",
                "--transport",
                "stdio"
            ],
            "env": {
                "FHIR_SERVER_BASE_URL": "https://hapi.fhir.org/baseR4"
            }
        }
    }
}
```
</td>

<td>

```json
{
    "mcpServers": {
        "fhir": {
            "command": "npx",
            "args": [
                "-y",
                "mcp-remote",
                "http://localhost:8000/sse"
            ]
        }
    }
}
```
</td>
</tr>
</table>

### MCP Inspector
Follow these steps to get the MCP Inspector up and running:

- Open a terminal and run the following command:
    
    `npx -y @modelcontextprotocol/inspector`

- In the MCP Inspector interface:
<table>
<tr><th>Streamable HTTP</th><th>STDIO</th><th>SSE</th></tr>
<tr valign=top>
<td>

- Transport Type: `Streamable HTTP`
- URL: `http://localhost:8000/mcp`
</td>

<td>

- Transport Type: `STDIO`
- Command: `uv`
- Arguments: `--directory /path/to/fhir-mcp-server run fhir-mcp-server --transport stdio`
</td>

<td>

- Transport Type: `SSE`
- URL: `http://localhost:8000/sse`
</td>
</tr>
</table>

Make sure your MCP server is already running and listening on the above endpoint.

Once connected, MCP Inspector will allow you to visualize tool invocations, inspect request/response payloads, and debug your tool implementations easily.

## Configuration

### CLI Options

You can customize the behavior of the MCP server using the following command-line flags:

- **--transport**
    - Description: Specifies the transport protocol used by the MCP server to communicate with clients.
    - Accepted values: stdio, sse, streamable-http
    - Default: streamable-http

- **--log-level**
    - Description: Sets the logging verbosity level for the server.
    - Accepted values: DEBUG, INFO, WARN, ERROR (case-insensitive)
    - Default: INFO

- **--help**
    - Description: Displays a help message with available server options and exits.
    - Usage: Automatically provided by the command-line interface.

Sample Usages:

```shell
uv run fhir-mcp-server --transport streamable-http --log-level DEBUG
uv run fhir-mcp-server --help
```

### Environment Variables

**MCP Server Configurations:**
- `FHIR_MCP_HOST`: The hostname or IP address the MCP server should bind to (e.g., `localhost` for local-only access, or `0.0.0.0` for all interfaces).
- `FHIR_MCP_PORT`: The port on which the MCP server will listen for incoming client requests (e.g., `8000`).
- `FHIR_MCP_SERVER_URL`: If set, this value will be used as the server's base URL instead of generating it from host and port. Useful for custom URL configurations or when behind a proxy.
- `FHIR_MCP_REQUEST_TIMEOUT`: Timeout duration in seconds for requests from the MCP server to the FHIR server (default: `30`).

- `FHIR_MCP_ALLOWED_HOSTS` / `FHIR_MCP_ALLOWED_ORIGINS`: Space- or comma-separated `Host` and `Origin` header values to accept on HTTP transports. Set these when the server is reachable by a hostname other than `localhost`, so DNS-rebinding protection lets your clients through.
- `FHIR_MCP_MAX_REQUEST_BODY_BYTES`: Largest request body accepted (SDK default: 4 MiB). Raise it if you create or update large FHIR resources.

**MCP Authorization (MCP Client ↔ MCP Server):**
The MCP server is an OAuth 2.1 resource server. Tokens are issued by your identity provider (IdP); the server only validates them. See [Authorization](#authorization).

- `FHIR_MCP_AUTH_MODE`: `resource_server` (default) or `disabled` (local development only; anyone who can reach the server can call every tool). The `stdio` transport never uses this flow.
- `FHIR_MCP_IDP_ISSUER`: The IdP's issuer URL, exactly as it appears in its tokens' `iss` claim (e.g., `https://localhost:8090`). Required in `resource_server` mode.
- `FHIR_MCP_IDP_CA_CERT`: Path to a CA certificate for an IdP with a self-signed TLS certificate (local development).
- `FHIR_MCP_RESOURCE_URL`: The canonical URL of the MCP endpoint. Tokens must carry it as their audience (`aud`). Defaults to `FHIR_MCP_SERVER_URL` (or `http://<host>:<port>`) followed by `/mcp`.
- `FHIR_MCP_JWKS_URI`: Overrides the JWKS URL discovered from the IdP. Tokens are validated as JWTs against the IdP's published keys.
- `FHIR_MCP_READ_SCOPE` / `FHIR_MCP_WRITE_SCOPE` / `FHIR_MCP_DELETE_SCOPE`: Scope names required by the read tools (`get_capabilities`, `search`, `read`, `get_user`), the write tools (`create`, `update`) and `delete`. Defaults: `fhir-read`, `fhir-write`, `fhir-delete`.

**FHIR Server Configuration (MCP Server ↔ FHIR Server):**
- `FHIR_SERVER_BASE_URL`: The base URL of the FHIR server (e.g., `https://hapi.fhir.org/baseR4`). This is used to generate tool URIs and to route FHIR requests.
- `FHIR_SERVER_AUTH_MODE`: How the server authenticates to the FHIR server. The MCP client's token is never forwarded to the FHIR server.
    - `none` (default): no credential, e.g. a local or open FHIR server.
    - `token_exchange`: the server exchanges the caller's MCP token (RFC 8693) at the same IdP for a token audienced to the FHIR API (`FHIR_SERVER_TOKEN_RESOURCE`), authenticating with `FHIR_SERVER_CLIENT_ID` / `FHIR_SERVER_CLIENT_SECRET`. Use it when the same IdP protects both the MCP server and the FHIR server.
    - `user_smart_login`: each user signs in to the FHIR server (SMART App Launch) through a link the server sends. See [Authorization](#authorization).
- `FHIR_SERVER_CLIENT_ID` / `FHIR_SERVER_CLIENT_SECRET`: The server's client at the IdP (`token_exchange`) or at the FHIR server (`user_smart_login`).
- `FHIR_SERVER_SCOPES`: Scopes requested at the FHIR server sign-in (`user_smart_login`); must include `openid fhirUser`.
- `FHIR_SERVER_TOKEN_RESOURCE`: The FHIR API's audience (`token_exchange`).
- `FHIR_SERVER_AUTHORIZE_URL` / `FHIR_SERVER_TOKEN_URL`: Override the endpoints from `<FHIR_SERVER_BASE_URL>/.well-known/smart-configuration`.
- `FHIR_SERVER_CA_CERT`: Extra CA certificate for the FHIR server and its token endpoints.

For local development without any sign-in, set `FHIR_MCP_AUTH_MODE=disabled` and `FHIR_SERVER_AUTH_MODE=none`.

> [!IMPORTANT]
> `FHIR_SERVER_DISABLE_AUTHORIZATION` has been removed. The server refuses to start while it is set; use `FHIR_MCP_AUTH_MODE` and `FHIR_SERVER_AUTH_MODE` instead.

### Authorization

The server follows the [MCP 2026-07-28 authorization specification](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization):

1. A client calls `/mcp` without a token and receives `401` with a `WWW-Authenticate` header pointing to `/.well-known/oauth-protected-resource/mcp`.
2. That metadata names your IdP. The client signs the user in there (authorization code with PKCE) and asks for a token for the MCP endpoint.
3. The client calls `/mcp` with `Authorization: Bearer <token>`. The server checks the signature, the issuer, the audience, the expiry and the tool's scope.
4. A token without a tool's scope gets `403` with `error="insufficient_scope"` and the scope it needs, so the client can ask the user for more access.

To set up an IdP:
- Register the MCP server as a resource server (API) whose identifier is `FHIR_MCP_RESOURCE_URL`, e.g. `http://localhost:8000/mcp`.
- Define the read, write and delete permissions (scopes) and grant them to users through roles.
- Register each MCP client (MCP Inspector, Claude, VS Code) with its redirect URI, or enable Client ID Metadata Documents or Dynamic Client Registration if your IdP supports them.
- To let `get_user` find the signed-in user's FHIR record, add a `fhirUser` claim (e.g. `Patient/123` or `Practitioner/456`) to the access token.

#### Each user connects their own Epic account (WSO2 Identity Server federated to Epic + `user_smart_login`)

Users exist only in Epic. WSO2 Identity Server signs them in through Epic (Epic added as a federated connection, just-in-time provisioning off) and issues the MCP token, which carries the Epic user's `fhirUser`.

- **WSO2 Identity Server** decides who may use the MCP server and issues the MCP token. The MCP client keeps it; the server checks it on every request and never stores it.
- **Epic** decides who may see the health records. The first time a tool needs data, the server sends the user a link (URL-mode elicitation; an `InputRequiredResult` on 2026-07-28 clients). They sign in to Epic, and Epic gives the server their own FHIR token. The server keeps it under the MCP client's ID and the user's `sub`; it never reaches the client. By default that store is in memory and is lost on restart (see **Token storage** below).

The Epic sign-in at the link must name the same person as the MCP token: the `fhirUser` claim of Epic's ID token, or else its `patient`, must match the MCP token's `fhirUser`, otherwise the connection is refused with a plain 403. A short-lived cookie also ties the Epic sign-in to the browser that opened the link. Users sign in at Epic twice (once through WSO2 Identity Server, once at the link), because Epic ends its session after each sign-in.

This is not token exchange: Epic does not accept tokens from WSO2 Identity Server, so the server links the two sign-ins instead.

Setup in WSO2 Identity Server (verified with 7.3):
1. **Connections → New Connection**: Epic as an OpenID Connect connection, using a separate Epic app whose redirect URI is `https://<IS host>/commonauth`, with scopes `openid fhirUser` plus a FHIR read scope and the `aud` parameter set to the FHIR base URL. Map Epic's `fhirUser` claim and keep just-in-time provisioning off.
2. **Resources → API Resources → New API Resource**: identifier `FHIR_MCP_RESOURCE_URL` (e.g. `http://localhost:8000/mcp`), scopes `fhir-read`, `fhir-write`, `fhir-delete`, and **Requires authorization** unticked.
3. **Applications → New Application → MCP Client Application**: one per MCP client (public client, its redirect URI), signing in with the Epic connection, with `fhirUser` added to the access token's claims.
4. Authorize the API resource on that application with the **No Authorization** policy, through the API: `POST /api/server/v1/applications/<app ID>/authorized-apis` with `{"id": "<API resource ID>", "policyIdentifier": "NO POLICY", "scopes": ["fhir-read", "fhir-write", "fhir-delete"]}`.
5. **Token audience**: WSO2 Identity Server 7.3 ignores the `resource` parameter. Fetch `GET /api/server/v1/applications/<app ID>/inbound-protocols/oidc`, add `FHIR_MCP_RESOURCE_URL` to `idToken.audience`, and `PUT` the result back.
6. Keep **Skip login consent** off so users see which scopes they grant.

In Epic on FHIR, register a patient-facing R4 app for the MCP server with redirect URI `<server URL>/connect/fhir/callback`, the FHIR APIs your tools need, and refresh tokens enabled (without them users reconnect when the token expires). Make it a confidential client with a sandbox secret (**Generate Secret**, then **Store Hash**, then save) and set `FHIR_SERVER_CLIENT_SECRET`. Leave **Can Register Dynamic Clients** off.

```env
FHIR_MCP_IDP_ISSUER="https://localhost:9443/oauth2/token"
FHIR_MCP_RESOURCE_URL="http://localhost:8000/mcp"
FHIR_MCP_IDP_CA_CERT="./wso2is.pem"

FHIR_SERVER_BASE_URL="https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4"
FHIR_SERVER_AUTH_MODE="user_smart_login"
FHIR_SERVER_CLIENT_ID="<Epic client ID>"
FHIR_SERVER_CLIENT_SECRET="<Epic client secret>"
FHIR_SERVER_SCOPES="openid fhirUser patient/Patient.read patient/Observation.read"
```

#### One identity provider for both the MCP server and the FHIR server (`token_exchange`)

When the FHIR server (or a gateway in front of it) trusts the same IdP that issues MCP tokens, the server exchanges the caller's MCP token at that IdP (RFC 8693) for a token audienced to the FHIR API. The FHIR server sees the real user, and the IdP caps the exchanged token's scope to what the MCP token was granted. Exchanged tokens are cached per user until shortly before they expire.

```env
FHIR_MCP_IDP_ISSUER="https://localhost:9443/oauth2/token"
FHIR_SERVER_BASE_URL="<FHIR API URL>"
FHIR_SERVER_AUTH_MODE="token_exchange"
FHIR_SERVER_CLIENT_ID="<MCP server's client at the IdP, allowed the token exchange grant>"
FHIR_SERVER_CLIENT_SECRET="<its secret>"
FHIR_SERVER_TOKEN_RESOURCE="<FHIR API audience>"
```

#### Token storage

Users' FHIR tokens, exchanged tokens (`token_exchange`), connect links and pending sign-ins are kept in a token store. The default keeps them in the server's memory: nothing is written to disk, and a restart forgets every connection, so users connect again. To keep connections across restarts or share them between replicas, plug in your own store (Redis, a database, a token vault) without changing the server:

```env
FHIR_MCP_TOKEN_STORE="my_package.stores:create_store"
```

`create_store(configs)` receives the server's settings and returns an object with four async methods: `get(key)`, `put(key, value, ttl_seconds)`, `pop(key)` and `delete(key)`. Values are JSON-compatible. The store must expire entries after `ttl_seconds`, make `pop` atomic (read and delete in one step, because links and sign-in states are single-use), and encrypt values at rest, because they include FHIR access and refresh tokens. Read the store's own settings, such as a Redis URL, from the environment. See `src/fhir_mcp_server/token_store.py`.

## Tools

- `get_capabilities`: Retrieves metadata about a specified FHIR resource type, including its supported search parameters and custom operations.
    - `type`: The FHIR resource type name (e.g., "Patient", "Observation", "Encounter")

- `search`: Executes a standard FHIR search interaction on a given resource type, returning a bundle or list of matching resources.
    - `type`: The FHIR resource type name (e.g., "MedicationRequest", "Condition", "Procedure").
    - `searchParam`: A mapping of FHIR search parameter names to their desired values (e.g., {"family":"Simpson","birthdate":"1956-05-12"}).
    - `response_filter_fhirpaths`: (Optional) An array of FHIRPath expressions (e.g., `["Patient.name", "Patient.birthDate", "Bundle.link.where(relation='next').url"]`) to apply to the resources in the response bundle.

- `read`: Performs a FHIR "read" interaction to retrieve a single resource instance by its type and resource ID, optionally refining the response with search parameters or custom operations.
    - `type`: The FHIR resource type name (e.g., "DiagnosticReport", "AllergyIntolerance", "Immunization").
    - `id`: The logical ID of a specific FHIR resource instance.
    - `searchParam`: A mapping of FHIR search parameter names to their desired values (e.g., {"device-name":"glucometer"}).
    - `operation`: The name of a custom FHIR operation or extended query defined for the resource (e.g., "$everything").
    - `response_filter_fhirpaths`: (Optional) An array of FHIRPath expressions (e.g., `["Patient.name", "Observation.valueQuantity"]`) to filter the returned single resource (or entries when using custom operations like `$everything`).

- `create`: Executes a FHIR "create" interaction to persist a new resource of the specified type.
    - `type`: The FHIR resource type name (e.g., "Device", "CarePlan", "Goal").
    - `payload`: A JSON object representing the full FHIR resource body to be created.
    - `searchParam`: A mapping of FHIR search parameter names to their desired values (e.g., {"address-city":"Boston"}).
    - `operation`: The name of a custom FHIR operation or extended query defined for the resource (e.g., "$evaluate").

- `update`: Performs a FHIR "update" interaction by replacing an existing resource instance's content with the provided payload.
    - `type`: The FHIR resource type name (e.g., "Location", "Organization", "Coverage").
    - `id`: The logical ID of a specific FHIR resource instance.
    - `payload`: The complete JSON representation of the FHIR resource, containing all required elements and any optional data.
    - `searchParam`: A mapping of FHIR search parameter names to their desired values (e.g., {"patient":"Patient/54321","relationship":"father"}).
    - `operation`: The name of a custom FHIR operation or extended query defined for the resource (e.g., "$lastn").

- `delete`: Execute a FHIR "delete" interaction on a specific resource instance.
    - `type`: The FHIR resource type name (e.g., "ServiceRequest", "Appointment", "HealthcareService").
    - `id`: The logical ID of a specific FHIR resource instance.
    - `searchParam`: A mapping of FHIR search parameter names to their desired values (e.g., {"category":"laboratory","issued:"2025-05-01"}).
    - `operation`: The name of a custom FHIR operation or extended query defined for the resource (e.g., "$expand").

- `get_user`: Retrieves the currently authenticated user's FHIR resource (for example the linked `Patient` resource) and returns a concise profile containing available demographic fields such as `id`, `name`, and `birthDate`.

## Development & Testing

### Installing Development Dependencies

To run tests and contribute to development, install the test dependencies:

**Using pip:**
```bash
# Install project in development mode with test dependencies
pip install -e '.[test]'

# Or install from requirements file
pip install -r requirements-dev.txt
```

**Using uv:**
```bash
# Install development dependencies
uv sync --dev
```

### Running Tests

The project includes a comprehensive test suite covering all major functionality:

```bash
# Simple test runner
python run_tests.py

# Or direct pytest usage
PYTHONPATH=src python -m pytest tests/ -v --cov=src/fhir_mcp_server
```
**Using pytest:**
```bash
pytest tests/
```
This will discover and run all tests in the `tests/` directory.


**Test Features:**
- **100+ tests** with comprehensive coverage
- **Full async/await support** using pytest-asyncio
- **Complete mocking** of HTTP requests and external dependencies
- **Coverage reporting** with terminal and HTML output
- **Fast execution** with no real network calls

The test suite includes:
- **Unit tests**: Core functionality testing
- **Integration tests**: Component interaction validation
- **Edge case coverage**: Error handling and validation scenarios
- **Mocked OAuth flows**: Realistic authentication testing

Coverage reports are generated in `htmlcov/index.html` for detailed analysis.

<!-- mcp-name: io.github.wso2/fhir-mcp-server -->
