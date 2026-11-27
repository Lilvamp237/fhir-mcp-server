# FHIR MCP Server Test Suite

This directory contains comprehensive test cases for the FHIR MCP server, including unit tests and integration tests with proper mocking to avoid external dependencies.

## Test Structure

```
tests/
├── unit/                             # Unit tests: single modules, no servers
│   ├── test_utils.py                 # FHIR client and helper functions
│   ├── test_backend_auth.py          # FHIR TLS and client authentication helpers
│   ├── test_call_fhir.py             # 401 retry and 401/403 outcomes
│   ├── field_filter/                 # FHIRPath response filtering
│   └── oauth/test_types.py           # Settings and startup validation
├── integration/                      # The server over real HTTP, against local stubs (offline)
│   ├── conftest.py                   # Builds and serves the server as main() does
│   ├── mcp_client.py                 # One MCP client API for both protocol versions
│   ├── stubs/                        # In-process FHIR server, IdP and SMART (Epic) sign-in
│   ├── snapshots/tools.json          # Expected tool schemas
│   ├── test_integration.py           # Settings and token verifier wiring
│   ├── test_authorization.py         # MCP login: 401, protected resource metadata, scopes
│   ├── test_agent_link.py            # Agent linking: consent page, FHIR sign-in, allowed tools, revoke
│   ├── test_protocol_eras.py         # 2026-07-28 and earlier protocol versions
│   ├── test_tool_behaviour.py        # What each tool sends to the FHIR server
│   └── test_tool_schemas.py          # Tool schemas against the snapshot
└── e2e/                              # End-to-end tests against a live FHIR server
    ├── conftest.py
    └── test_tools.py
```

## Running Tests

### Option 1: Using the test runner script (Recommended)
```bash
python run_tests.py
```

### Option 2: Using pytest directly
```bash
# Set the Python path and run tests
PYTHONPATH=src python -m pytest tests/ -v --cov=src/fhir_mcp_server --cov-report=term-missing --cov-report=html:htmlcov
```

### Option 3: Running specific test files
```bash
# Run only unit tests
PYTHONPATH=src python -m pytest tests/unit/ -v

# Run only integration tests
PYTHONPATH=src python -m pytest tests/integration/ -v

# Run specific test file
PYTHONPATH=src python -m pytest tests/unit/test_utils.py -v
```

## Test Categories

- **Unit tests** check one module at a time.
- **Integration tests** start the MCP server over Streamable HTTP, as `main()` does, with local stubs for the FHIR server, the identity provider and the FHIR server's sign-in, so they need no network. They cover the MCP login, agent linking and consent, both protocol versions, and every tool. After an intended change to a tool's schema, update the snapshot with `UPDATE_TOOL_SNAPSHOT=1 pytest tests/integration/test_tool_schemas.py`.
- **End-to-end tests** run the server against a live public FHIR server.

## Test Features

### Comprehensive Mocking
- All external HTTP requests are mocked
- No real network calls during testing
- Isolated testing of individual components

### Async Testing Support
- Full support for async/await patterns
- Proper async test fixtures
- Mock async functions and coroutines

### Edge Case Coverage
- Error conditions and exception handling
- Invalid input validation
- Network failure scenarios
- Configuration edge cases

### Fixtures and Utilities
- Reusable test fixtures
- Mock data generators
- Common test utilities

## Dependencies

Test dependencies are installed via:
```bash
pip install pytest pytest-asyncio pytest-mock pytest-cov
```

## Configuration

Test configuration is managed through `pytest.ini`:
- Test discovery patterns
- Coverage settings
- Async mode configuration
- Test markers

## Best Practices

1. **Isolation**: Each test is isolated and doesn't depend on others
2. **Mocking**: External dependencies are properly mocked
3. **Coverage**: High test coverage with meaningful assertions
4. **Documentation**: Each test is well-documented with clear purpose
5. **Performance**: Tests run quickly with minimal overhead

## Contributing

When adding new tests:
1. Follow the existing naming conventions
2. Add appropriate docstrings
3. Mock external dependencies
4. Test both success and failure scenarios
5. Update this README if adding new test categories