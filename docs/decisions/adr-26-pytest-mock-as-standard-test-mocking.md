---
area: process
accepted: by 2026-02-24
---
# ADR #26 — pytest-mock as Standard Test Mocking Library

**Decision:** All test files use `pytest-mock` (`mocker` fixture) for patching and mocking. `unittest.mock` is not used directly in tests.

**Rationale:**
- Auto-cleanup: patches reverted when test ends (fixture lifecycle), no `with patch()` context managers needed
- Less boilerplate: `mocker.patch('x.y')` vs `with patch('x.y') as mock:` + indentation
- Consistency: one pattern across all 448 tests instead of mixed approaches
- `mocker.spy()` for partial mocking when needed
- Better pytest integration: fixtures compose naturally with other fixtures

**Migration (v69.3):** 12 test files + 1 conftest migrated from `unittest.mock`. Patterns:
- `from unittest.mock import MagicMock, patch` → `mocker` fixture param
- `MagicMock()` → `mocker.MagicMock()`
- `with patch('x.y', mock):` → `mocker.patch('x.y', mock)`
- `@patch('x.y')` → `mocker.patch('x.y')` inside test body
- `patch.dict('os.environ', {...})` → `mocker.patch.dict('os.environ', {...})`
- `yield mock` in fixtures → `return mock` (mocker handles teardown)

**Exceptions (stdlib `unittest.mock` allowed):**
- `polyris/validation.py` — runtime module spoofing (not a test)
- `test_registration_provider.py` fixture — `sys.modules` bootstrap for Pulumi mock

**Dependency:** `pytest-mock>=3.0.0` in `pyproject.toml` `[project.optional-dependencies] dev`.
