# ----------------------------------------------------------------------------------------
#
# Copyright (c) 2025, Choreo Labs LLC. (https://choreolabs.com)
#
# This software is the property of Choreo Labs LLC. and its suppliers, if any.
# Dissemination of any information or reproduction of any material contained
# herein in any form is strictly forbidden, unless permitted by Choreo Labs expressly.
# You may not alter or remove any copyright or other notice from copies of this content.
#
# ----------------------------------------------------------------------------------------

# Builder
# Build arguments for multi-architecture support
ARG BUILDPLATFORM
ARG TARGETPLATFORM

FROM --platform=$TARGETPLATFORM ghcr.io/astral-sh/uv:0.11-python3.12-trixie-slim AS builder
WORKDIR /app

# Copy source
COPY . .

# Create venv and install in one layer
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="${VIRTUAL_ENV}/bin:${PATH}"
RUN uv venv /opt/venv && \
    uv pip sync requirements.txt && \
    uv pip install .

# Runtime
FROM --platform=$TARGETPLATFORM python:3.12-slim-trixie AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:${PATH}" \
    FHIR_MCP_HOST=0.0.0.0 \
    FHIR_MCP_PORT=8000 \
    FHIR_MCP_REQUEST_TIMEOUT=30 

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app /app

RUN useradd -m -u 10001 appuser
USER 10001

EXPOSE 8000
CMD ["fhir-mcp-server", "--transport", "streamable-http", "--log-level", "INFO"]
