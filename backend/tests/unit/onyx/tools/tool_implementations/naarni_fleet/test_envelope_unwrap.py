"""Unit tests for NaarniFleetTool._unwrap_envelope.

The Naarni backend returns two different response shapes depending on the
endpoint: CRUD endpoints wrap the payload in {body, statusCode, success,
errorMessage, code}, while analytics and alerts endpoints return bare
objects. The unwrap helper must detect the envelope strictly (all three
canonical fields present) and leave bare responses untouched so the LLM
never has to parse a redundant envelope.
"""

import pytest

from onyx.tools.models import ToolCallException
from onyx.tools.tool_implementations.naarni_fleet.naarni_fleet_tool import (
    NaarniFleetTool,
)


# ─── Wrapped (CRUD) responses should be flattened ───────────────────────────


def test_unwrap_flattens_standard_success_envelope() -> None:
    wrapped = {
        "body": {"id": 2, "name": "Fleet Alpha", "organizationId": 2},
        "statusCode": 200,
        "success": True,
        "errorMessage": None,
        "code": None,
    }
    assert NaarniFleetTool._unwrap_envelope(wrapped) == {
        "id": 2,
        "name": "Fleet Alpha",
        "organizationId": 2,
    }


def test_unwrap_handles_paginated_body() -> None:
    # /api/v1/fleets and friends return a paginated content array inside body.
    wrapped = {
        "body": {
            "content": [{"id": 2, "name": "Alpha"}],
            "totalElements": 1,
            "first": True,
            "last": True,
        },
        "statusCode": 200,
        "success": True,
        "errorMessage": None,
        "code": None,
    }
    unwrapped = NaarniFleetTool._unwrap_envelope(wrapped)
    assert unwrapped["content"] == [{"id": 2, "name": "Alpha"}]
    assert unwrapped["totalElements"] == 1


def test_unwrap_handles_null_body() -> None:
    # POST /api/v1/vehicles/associate-device returns body=null on success.
    wrapped = {
        "body": None,
        "statusCode": 201,
        "success": True,
        "errorMessage": None,
        "code": None,
    }
    assert NaarniFleetTool._unwrap_envelope(wrapped) is None


# ─── Bare responses (analytics / alerts / alert-definitions) passthrough ────


def test_unwrap_leaves_analytics_bare_response_untouched() -> None:
    # POST /api/v1/analytics/performance returns a bare shape — no envelope.
    bare = {
        "vehicles": None,
        "routes": [{"id": 1, "name": "Route 101"}],
        "depots": None,
        "timeGroups": None,
        "totalResults": None,
    }
    assert NaarniFleetTool._unwrap_envelope(bare) == bare


def test_unwrap_leaves_spring_pageable_alerts_untouched() -> None:
    # GET /api/v1/alerts returns Spring Pageable directly at the top level.
    # This has `content` but NOT `body`, so we must not unwrap it.
    pageable = {
        "content": [{"id": "abc", "name": "AC Fault Code"}],
        "pageable": {"pageNumber": 0, "pageSize": 20},
        "totalElements": 4,
        "totalPages": 1,
        "last": True,
        "size": 20,
        "number": 0,
        "first": True,
        "empty": False,
    }
    assert NaarniFleetTool._unwrap_envelope(pageable) == pageable


def test_unwrap_leaves_bare_array_untouched() -> None:
    # GET /api/v1/alert-definitions returns a bare JSON array.
    bare_array = [{"id": 1, "name": "Accessory contactor status"}]
    assert NaarniFleetTool._unwrap_envelope(bare_array) == bare_array


def test_unwrap_leaves_scalar_untouched() -> None:
    assert NaarniFleetTool._unwrap_envelope(42) == 42
    assert NaarniFleetTool._unwrap_envelope("hello") == "hello"
    assert NaarniFleetTool._unwrap_envelope(None) is None


# ─── success=false envelopes must raise ─────────────────────────────────────


def test_unwrap_raises_on_success_false_with_message() -> None:
    envelope = {
        "body": None,
        "statusCode": 400,
        "success": False,
        "errorMessage": "Invalid request input. Please check your request parameters and body format.",
        "code": None,
    }
    with pytest.raises(ToolCallException) as exc_info:
        NaarniFleetTool._unwrap_envelope(envelope)
    err = exc_info.value
    assert "Invalid request input" in err.llm_facing_message
    assert "Invalid request input" in str(err)


def test_unwrap_raises_on_success_false_without_message() -> None:
    envelope = {
        "body": None,
        "statusCode": 400,
        "success": False,
        "errorMessage": None,
        "code": None,
    }
    with pytest.raises(ToolCallException):
        NaarniFleetTool._unwrap_envelope(envelope)


def test_unwrap_accepts_snake_case_error_message_alias() -> None:
    # Some Spring handlers (notably the auth/token route) return error_message
    # with a snake_case key instead of errorMessage.
    envelope = {
        "body": None,
        "statusCode": 401,
        "success": False,
        "error_message": "Otp Validation Not Allowed",
        "code": "ERR_1102",
    }
    with pytest.raises(ToolCallException) as exc_info:
        NaarniFleetTool._unwrap_envelope(envelope)
    assert "Otp Validation Not Allowed" in exc_info.value.llm_facing_message


# ─── Edge cases: partial envelope ───────────────────────────────────────────


def test_unwrap_does_not_unwrap_if_only_body_present() -> None:
    # An arbitrary dict that happens to have a `body` key must NOT be
    # mistaken for an envelope. Only unwrap when body+statusCode+success
    # are all present.
    partial = {"body": "hello"}
    assert NaarniFleetTool._unwrap_envelope(partial) == partial


def test_unwrap_does_not_unwrap_if_missing_success() -> None:
    partial = {"body": {"x": 1}, "statusCode": 200}
    assert NaarniFleetTool._unwrap_envelope(partial) == partial


def test_unwrap_does_not_unwrap_if_missing_status_code() -> None:
    partial = {"body": {"x": 1}, "success": True}
    assert NaarniFleetTool._unwrap_envelope(partial) == partial
