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
    # isMoving is derived from real-time groundSpeedKmph (0.0 → not moving)
    assert v21["isMoving"] is False
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
    # isMoving=True because groundSpeedKmph=62.5 > 0
    assert v34["isMoving"] is True


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
    assert "liveVehicleStatus" not in v
    assert "speedKmph" not in v
    # Non-None values must still be present
    assert v["vehicleId"] == 99
    assert v["registrationNumber"] == "DL01AB0001"
    # isMoving defaults to False when no telemetry is available
    assert v["isMoving"] is False


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


# ─── _format_performance_response ──────────────────────────────────────────


def test_format_performance_aggregate() -> None:
    """No groupBy → totalResults is flattened to a single dict."""
    raw = {
        "vehicles": None,
        "routes": None,
        "depots": None,
        "timeGroups": None,
        "totalResults": [
            {
                "averageMileage": 0.795,
                "kilometerRun": 26844.12,
                "averageKilometerRun": 547.839,
                "energyConsumed": 21126.98,
                "energyRegenerated": 4946.55,
                "performanceStatus": "GREAT",
            }
        ],
    }
    result = NaarniFleetTool._format_performance_response(raw)
    assert result["energyConsumed"] == 21126.98
    assert result["energyRegenerated"] == 4946.55
    assert result["kilometerRun"] == 26844.12
    assert "vehicles" not in result
    assert "timeGroups" not in result


def test_format_performance_by_time() -> None:
    """groupBy=TIME → epoch timeGroups flattened to daily with date strings."""
    raw = {
        "vehicles": None,
        "routes": None,
        "depots": None,
        "timeGroups": [
            {
                "timeGroup": 1775260800.0,  # 2026-04-04 UTC
                "metrics": [
                    {
                        "timeGroup": 1775260800.0,
                        "averageMileage": 0.88,
                        "kilometerRun": 3821.38,
                        "energyConsumed": 3169.02,
                        "energyRegenerated": 715.14,
                        "performanceStatus": "GREAT",
                    }
                ],
            },
            {
                "timeGroup": 1775347200.0,  # 2026-04-05 UTC
                "metrics": [
                    {
                        "timeGroup": 1775347200.0,
                        "averageMileage": 0.747,
                        "kilometerRun": 3861.61,
                        "energyConsumed": 3057.99,
                        "energyRegenerated": 724.58,
                        "performanceStatus": "GREAT",
                    }
                ],
            },
        ],
        "totalResults": None,
    }
    result = NaarniFleetTool._format_performance_response(raw)
    assert "daily" in result
    assert len(result["daily"]) == 2
    day1 = result["daily"][0]
    assert day1["date"] == "2026-04-04"
    assert day1["energyConsumed"] == 3169.02
    assert day1["energyRegenerated"] == 715.14
    assert "timeGroup" not in day1  # epoch must be stripped
    day2 = result["daily"][1]
    assert day2["date"] == "2026-04-05"
    assert day2["energyConsumed"] == 3057.99


def test_format_performance_by_vehicle() -> None:
    """groupBy=VEHICLE → vehicles[] with flattened metrics."""
    raw = {
        "vehicles": [
            {
                "id": 1,
                "registrationNumber": "HR55AY7626",
                "model": "A_12.5_M",
                "status": "NOT_MOVING",
                "metrics": {
                    "id": "1",
                    "averageMileage": 0.827,
                    "kilometerRun": 4193.62,
                    "energyConsumed": 4146.71,
                    "energyRegenerated": 1048.36,
                    "performanceStatus": "GREAT",
                },
            },
        ],
        "routes": None,
        "depots": None,
        "timeGroups": None,
        "totalResults": None,
    }
    result = NaarniFleetTool._format_performance_response(raw)
    assert "vehicles" in result
    v = result["vehicles"][0]
    assert v["vehicleId"] == 1
    assert v["registrationNumber"] == "HR55AY7626"
    assert v["energyConsumed"] == 4146.71
    assert v["energyRegenerated"] == 1048.36
    assert v["kilometerRun"] == 4193.62
    # The nested metrics "id" field should be excluded
    assert "id" not in v


def test_format_performance_by_route() -> None:
    """groupBy=ROUTE → routes[] with flattened metrics and city names."""
    raw = {
        "vehicles": None,
        "routes": [
            {
                "id": 34,
                "name": "Gurgaon to Amritsar",
                "startCityName": "Gurgaon",
                "endCityName": "Amritsar",
                "metrics": {
                    "id": "34",
                    "averageMileage": 0.797,
                    "kilometerRun": 14294.12,
                    "energyConsumed": 9701.69,
                    "energyRegenerated": 2203.75,
                    "performanceStatus": "GREAT",
                },
            },
        ],
        "depots": None,
        "timeGroups": None,
        "totalResults": None,
    }
    result = NaarniFleetTool._format_performance_response(raw)
    assert "routes" in result
    r = result["routes"][0]
    assert r["routeId"] == 34
    assert r["routeName"] == "Gurgaon to Amritsar"
    assert r["startCity"] == "Gurgaon"
    assert r["endCity"] == "Amritsar"
    assert r["energyConsumed"] == 9701.69
    assert r["energyRegenerated"] == 2203.75


def test_format_performance_by_depot() -> None:
    """groupBy=DEPOT → depots[] with flattened metrics."""
    raw = {
        "vehicles": None,
        "routes": None,
        "depots": [
            {
                "id": 93,
                "name": "Gurgaon",
                "metrics": {
                    "id": "93",
                    "averageMileage": 0.795,
                    "kilometerRun": 26844.12,
                    "energyConsumed": 21126.98,
                    "energyRegenerated": 4946.55,
                    "performanceStatus": "GREAT",
                },
            },
        ],
        "timeGroups": None,
        "totalResults": None,
    }
    result = NaarniFleetTool._format_performance_response(raw)
    assert "depots" in result
    d = result["depots"][0]
    assert d["depotId"] == 93
    assert d["depotName"] == "Gurgaon"
    assert d["energyConsumed"] == 21126.98


def test_format_performance_passthrough_non_dict() -> None:
    assert NaarniFleetTool._format_performance_response("error") == "error"
    assert NaarniFleetTool._format_performance_response(None) is None


# ─── _format_vehicle_activity_response ─────────────────────────────────────


def test_format_vehicle_activity_by_time() -> None:
    """groupBy=TIME → daily with date strings and activity counts."""
    raw = {
        "vehicles": None,
        "routes": None,
        "depots": None,
        "timeGroups": [
            {
                "timeGroup": 1775260800.0,  # 2026-04-04
                "metrics": [
                    {
                        "timeGroup": 1775260800.0,
                        "kmsRun": 3821.38,
                        "activeCount": 7,
                        "inactiveCount": 1,
                        "totalCount": 8,
                    }
                ],
            },
        ],
        "totalResults": None,
    }
    result = NaarniFleetTool._format_vehicle_activity_response(raw)
    assert "daily" in result
    day = result["daily"][0]
    assert day["date"] == "2026-04-04"
    assert day["activeCount"] == 7
    assert day["inactiveCount"] == 1
    assert day["totalCount"] == 8
    assert day["kmsRun"] == 3821.38
    assert "timeGroup" not in day


# ─── _format_dashboard_response ────────────────────────────────────────────


def test_format_dashboard_flattens_single_result() -> None:
    raw = {
        "results": [
            {
                "averageMileage": 0.795,
                "kilometerRun": 26844.12,
                "averageKilometerRun": 547.839,
            }
        ],
        "executionDurationMs": 14,
        "fromCache": False,
    }
    result = NaarniFleetTool._format_dashboard_response(raw)
    assert result["averageMileage"] == 0.795
    assert result["kilometerRun"] == 26844.12
    assert "executionDurationMs" not in result
    assert "fromCache" not in result


def test_format_dashboard_passthrough_non_dict() -> None:
    assert NaarniFleetTool._format_dashboard_response("error") == "error"


# ─── _epoch_to_date ───────────────────────────────────────────────────────


def test_epoch_to_date_converts_correctly() -> None:
    assert NaarniFleetTool._epoch_to_date(1775260800.0) == "2026-04-04"
    assert NaarniFleetTool._epoch_to_date(1775347200.0) == "2026-04-05"


# ─── _resolve_route_ids (name → ID resolution) ────────────────────────────


def test_resolve_route_ids_from_route_name() -> None:
    """route_name should fuzzy-match against cached routes."""

    tool = NaarniFleetTool.__new__(NaarniFleetTool)
    tool._routes_cache = [
        {"id": 2, "name": "Gurgaon to Dehradun"},
        {"id": 34, "name": "Gurgaon to Amritsar"},
        {"id": 1, "name": "Bangalore to Chennai"},
    ]
    tool._vehicles_cache = []

    # Exact substring match
    params: dict = {"route_name": "Dehradun"}
    result = tool._resolve_route_ids(params)
    assert result == [2]

    # Multi-word match
    params = {"route_name": "gurgaon amritsar"}
    result = tool._resolve_route_ids(params)
    assert result == [34]

    # Full name
    params = {"route_name": "Bangalore to Chennai"}
    result = tool._resolve_route_ids(params)
    assert result == [1]

    # No match
    params = {"route_name": "Mumbai to Pune"}
    result = tool._resolve_route_ids(params)
    assert result is None

    # route_ids takes precedence over route_name
    params = {"route_ids": [99], "route_name": "Dehradun"}
    result = tool._resolve_route_ids(params)
    assert result == [99]


# ─── _normalize_timestamp ────────────────────────────────────────────────


def test_normalize_timestamp_adds_millis_to_start() -> None:
    """Start timestamps without millis get .000 appended."""
    assert (
        NaarniFleetTool._normalize_timestamp("2026-04-01T00:00:00")
        == "2026-04-01T00:00:00.000"
    )


def test_normalize_timestamp_adds_millis_to_end() -> None:
    """End timestamps without millis get .999 appended."""
    assert (
        NaarniFleetTool._normalize_timestamp("2026-04-10T23:59:59", is_end=True)
        == "2026-04-10T23:59:59.999"
    )


def test_normalize_timestamp_date_only_start() -> None:
    """Bare date for start → add T00:00:00.000."""
    assert (
        NaarniFleetTool._normalize_timestamp("2026-04-05") == "2026-04-05T00:00:00.000"
    )


def test_normalize_timestamp_date_only_end() -> None:
    """Bare date for end → add T23:59:59.999."""
    assert (
        NaarniFleetTool._normalize_timestamp("2026-04-05", is_end=True)
        == "2026-04-05T23:59:59.999"
    )


def test_normalize_timestamp_already_has_millis() -> None:
    """Timestamps that already have milliseconds pass through unchanged."""
    ts = "2026-04-01T00:00:00.000"
    assert NaarniFleetTool._normalize_timestamp(ts) == ts
    ts_end = "2026-04-10T23:59:59.999"
    assert NaarniFleetTool._normalize_timestamp(ts_end, is_end=True) == ts_end


def test_normalize_timestamp_empty_string() -> None:
    """Empty string passes through."""
    assert NaarniFleetTool._normalize_timestamp("") == ""
