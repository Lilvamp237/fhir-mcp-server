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

"""Claude Code `headersHelper` for an agent that signs in with client credentials.

Claude Code's MCP OAuth signs a user in through the browser; it has no client
credentials grant. Its `headersHelper` runs a command to produce the request
headers instead: on every connection, and again after a 401, before it retries.
This helper gets a client credentials token from the identity provider and
reuses it until shortly before it expires.

Settings: the JSON file named by FHIR_AGENT_CONFIG, by default
`~/.fhir-agent/agent.json` (see agent.example.json). Keep it outside any
repository: it holds the agent's secret. The cached token is kept next to it.

Standard library only. It prints only the headers, and errors without the secret.
"""

import base64
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from pathlib import Path

CONFIG = Path(os.environ.get("FHIR_AGENT_CONFIG", Path.home() / ".fhir-agent" / "agent.json"))
CACHE = CONFIG.with_name("token.json")
RENEW_BEFORE_SECONDS = 60
DEFAULT_EXPIRES_IN_SECONDS = 300


def fetch_token(config: dict) -> dict:
    """A new client credentials token, with the time it expires."""
    credentials = f"{urllib.parse.quote_plus(config['client_id'])}:{urllib.parse.quote_plus(config['client_secret'])}"
    form = {"grant_type": "client_credentials", "resource": config["resource"]}
    if config.get("scope"):
        form["scope"] = config["scope"]
    request = urllib.request.Request(
        config["token_endpoint"],
        data=urllib.parse.urlencode(form).encode(),
        headers={
            "Authorization": "Basic " + base64.b64encode(credentials.encode()).decode(),
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    # With ca_cert, trust only that CA (an IdP with a self-signed certificate); else the system's CAs.
    tls = ssl.create_default_context(cafile=config.get("ca_cert") or None)
    with urllib.request.urlopen(request, context=tls, timeout=8) as response:
        payload = json.load(response)
    expires_in = float(payload.get("expires_in") or DEFAULT_EXPIRES_IN_SECONDS)
    return {"access_token": payload["access_token"], "expires_at": time.time() + expires_in}


def main() -> None:
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    try:
        cached = json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cached = None
    if not cached or cached["expires_at"] - RENEW_BEFORE_SECONDS <= time.time():
        cached = fetch_token(config)
        CACHE.write_text(json.dumps(cached), encoding="utf-8")
    print(json.dumps({"Authorization": f"Bearer {cached['access_token']}"}))


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as ex:
        try:
            body = json.load(ex)
            detail = f"{body.get('error', '')}: {body.get('error_description', '')}"
        except ValueError:
            detail = "non-JSON error response"
        print(f"token helper failed: HTTP {ex.code} ({detail})", file=sys.stderr)
        sys.exit(1)
    except Exception as ex:
        print(f"token helper failed: {type(ex).__name__}: {ex}", file=sys.stderr)
        sys.exit(1)
