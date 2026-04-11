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


# ─── _format_vehicle_analytics_response ──────────────────────────────────────

# Real-world sample (condensed) matching the actual analytics-service response.
_REAL_ANALYTICS_RESPONSE = {
    "routes": [
        {"id": 2, "name": "Gurgaon to Dehradun"},
        {"id": 34, "name": "Gurgaon to Amritsar"},
    ],
    "depots": [{"id": 93, "name": "Gurgaon"}],
    "vehicles": [
        {
            "id": 21,
            "registrationNumber": "HR55AY9237",
            "status": "NOT_MOVING",
            "metrics": {
                "averageMileage": 0.726,
                "kilometerRun": 5895.0,
                "performanceStatus": "GREAT",
            },
            "recentInfo": {
                "batSoc": 70.8,
                "groundSpeedKmph": 0.0,
                "vehicleStatus": "Idling AC OFF",
            },
        },
        {
            "id": 34,
            "registrationNumber": "HR55AY1234",
            "status": "MOVING",
            "metrics": {
                "averageMileage": 1.1,
                "kilometerRun": 3200.0,
                "performanceStatus": "GOOD",
            },
            "recentInfo": {
                "batSoc": 55.0,
                "groundSpeedKmph": 62.5,
                "vehicleStatus": "Running",
            },
        },
    ],
    # vehicle IDs are string keys in JSON
    "vehicleToRouteIds": {"21": 2, "34": 2},
    "vehicleToDepotIds": {"21": 93, "34": 93},
}


def test_format_vehicle_analytics_denormalizes_route_and_depot() -> None:
    """Each enriched vehicle must carry the resolved route/depot name."""
    result = NaarniFleetTool._format_vehicle_analytics_response(
        _REAL_ANALYTICS_RESPONSE
    )

    assert result["totalVehicles"] == 2

    vehicles_by_id = {v["vehicleId"]: v for v in result["vehicles"]}

    v21 = vehicles_by_id[21]
    assert v21["registrationNumber"] == "HR55AY9237"
    assert v21["assignedRoute"] == "Gurgaon to Dehradun"
    assert v21["assignedDepot"] == "Gurgaon"
    assert v21["operationalStatus"] == "NOT_MOVING"
    assert v21["averageMileage_kmPerKwh"] == 0.726
    assert v21["kilometerRun"] == 5895.0
    assert v21["performanceStatus"] == "GREAT"
    assert v21["batterySOC_percent"] == 70.8
    assert v21["speedKmph"] == 0.0
    assert v21["liveVehicleStatus"] == "Idling AC OFF"

    # Vehicle 34 is on route 2 (Gurgaon to Dehradun), NOT route 34.
    # This cross-reference is the key correctness check.
    v34 = vehicles_by_id[34]
    assert v34["assignedRoute"] == "Gurgaon to Dehradun"
    assert v34["assignedDepot"] == "Gurgaon"
    assert v34["speedKmph"] == 62.5


def test_format_vehicle_analytics_returns_route_and_depot_lists() -> None:
    """Top-level routes/depots lists should be preserved for context."""
    result = NaarniFleetTool._format_vehicle_analytics_response(
        _REAL_ANALYTICS_RESPONSE
    )
    route_names = [r["name"] for r in result["routes"]]
    assert "Gurgaon to Dehradun" in route_names
    assert "Gurgaon to Amritsar" in route_names
    depot_names = [d["name"] for d in result["depots"]]
    assert "Gurgaon" in depot_names


def test_format_vehicle_analytics_drops_none_values() -> None:
    """Vehicles with missing optional fields should not have None entries."""
    sparse = {
        "routes": [],
        "depots": [],
        "vehicles": [
            {
                "id": 99,
                "registrationNumber": "DL01AB0001",
                "status": "INACTIVE",
                "metrics": None,
                "recentInfo": None,
            }
        ],
        "vehicleToRouteIds": {},
        "vehicleToDepotIds": {},
    }
    result = NaarniFleetTool._format_vehicle_analytics_response(sparse)
    v = result["vehicles"][0]
    # None values should be excluded
    assert "assignedRoute" not in v
    assert "assignedDepot" not in v
    assert "averageMileage_kmPerKwh" not in v
    assert "batterySOC_percent" not in v
    # Non-None values must still be present
    assert v["vehicleId"] == 99
    assert v["registrationNumber"] == "DL01AB0001"


def test_format_vehicle_analytics_passthrough_on_non_dict() -> None:
    """Non-dict inputs (e.g. error string) should pass through unchanged."""
    assert NaarniFleetTool._format_vehicle_analytics_response("error") == "error"
    assert NaarniFleetTool._format_vehicle_analytics_response(None) is None
    assert NaarniFleetTool._format_vehicle_analytics_response([1, 2]) == [1, 2]


def test_format_vehicle_analytics_empty_response() -> None:
    """An empty-but-valid API response should return zero vehicles."""
    empty = {
        "routes": [],
        "depots": [],
        "vehicles": [],
        "vehicleToRouteIds": {},
        "vehicleToDepotIds": {},
    }
    result = NaarniFleetTool._format_vehicle_analytics_response(empty)
    assert result["totalVehicles"] == 0
    assert result["vehicles"] == []
