"""Unit tests for NaarniFleetTool._sanitize_response.

The Naarni analytics backend leaks several data-quality issues into its
JSON payloads that confuse the LLM when it tries to answer a user's
question about the fleet. These tests pin the sanitizer's behavior
against a real sample response captured from
`POST /v1/analytics/vehicle-analytics`.
"""

from onyx.tools.tool_implementations.naarni_fleet.naarni_fleet_tool import (
    NaarniFleetTool,
)


# ─── Bytes literal stripping ─────────────────────────────────────────────────


def test_sanitize_string_strips_single_quoted_bytes_literal() -> None:
    assert NaarniFleetTool._sanitize_string("b'Start'") == "Start"
    assert NaarniFleetTool._sanitize_string("b'Stop'") == "Stop"


def test_sanitize_string_strips_double_quoted_bytes_literal() -> None:
    assert NaarniFleetTool._sanitize_string('b"Start"') == "Start"


def test_sanitize_string_leaves_normal_strings_untouched() -> None:
    assert NaarniFleetTool._sanitize_string("Start") == "Start"
    assert NaarniFleetTool._sanitize_string("") == ""
    assert NaarniFleetTool._sanitize_string("idling") == "idling"


def test_sanitize_string_does_not_touch_mid_string_b() -> None:
    # Only strips the b'...' wrapper, not any `b` in the middle.
    assert NaarniFleetTool._sanitize_string("running b'test' result") == (
        "running b'test' result"
    )


# ─── Float rounding ──────────────────────────────────────────────────────────


def test_round_floats_rounds_to_three_decimals() -> None:
    assert NaarniFleetTool._round_floats(0.7259090909090911) == 0.726
    assert NaarniFleetTool._round_floats(1.0) == 1.0
    assert NaarniFleetTool._round_floats(0.0) == 0.0


def test_round_floats_leaves_non_floats_untouched() -> None:
    assert NaarniFleetTool._round_floats(5895) == 5895
    assert NaarniFleetTool._round_floats("hello") == "hello"
    assert NaarniFleetTool._round_floats(None) is None


# ─── recentInfo normalization ────────────────────────────────────────────────


def test_sanitize_recent_info_renames_vehicleId_to_deviceId() -> None:
    recent = {
        "vehicleId": "16",
        "odometerReading": 144463.75,
    }
    out = NaarniFleetTool._sanitize_recent_info(recent)
    # The key that used to be `vehicleId` (actually a Trinity device id)
    # is renamed to `deviceId` so the LLM doesn't confuse it with the
    # parent Naarni vehicle id.
    assert "vehicleId" not in out
    assert out["deviceId"] == "16"


def test_sanitize_recent_info_strips_bytes_from_ac_status() -> None:
    recent = {"acStatus": "b'Start'", "vehicleStatus": "Idling AC OFF"}
    out = NaarniFleetTool._sanitize_recent_info(recent)
    assert out["acStatus"] == "Start"
    assert out["vehicleStatus"] == "Idling AC OFF"


def test_sanitize_recent_info_converts_timestamp_to_iso() -> None:
    # Pick a known, stable epoch so the output is deterministic.
    # 1700000000 = 2023-11-14T22:13:20+00:00
    recent = {"timestamp": 1700000000.0}
    out = NaarniFleetTool._sanitize_recent_info(recent)
    assert out["timestamp"] == "2023-11-14T22:13:20+00:00"
    assert isinstance(out["secondsAgo"], int)
    assert out["secondsAgo"] >= 0


def test_sanitize_recent_info_drops_null_values() -> None:
    recent = {"odometerReading": None, "batSoc": 70.8}
    out = NaarniFleetTool._sanitize_recent_info(recent)
    assert "odometerReading" not in out
    assert out["batSoc"] == 70.8


def test_sanitize_recent_info_rounds_floats() -> None:
    recent = {
        "latitude": 29.298817123456,
        "longitude": 77.72057098765,
        "batSoc": 70.8312,
    }
    out = NaarniFleetTool._sanitize_recent_info(recent)
    assert out["latitude"] == 29.299
    assert out["longitude"] == 77.721
    assert out["batSoc"] == 70.831


def test_sanitize_recent_info_handles_non_dict_gracefully() -> None:
    assert NaarniFleetTool._sanitize_recent_info(None) is None
    assert NaarniFleetTool._sanitize_recent_info("not a dict") == "not a dict"


# ─── Full response sanitization against the real sample ─────────────────────


def _real_sample_response() -> dict:
    """The exact shape captured from the user's curl against
    https://dashboard.naarni.com → POST /v1/analytics/vehicle-analytics.
    Only trimmed to two vehicles for test speed."""
    return {
        "routes": [
            {
                "id": 2,
                "name": "Gurgaon to Dehradun",
                "description": "",
                "startCityId": 93,
                "endCityId": 175,
                "startCityName": "Gurgaon",
                "endCityName": "Dehradun",
                "distance": None,
                "estimatedDuration": 0,
                "routeType": "TO_FRO",
                "oneWay": False,
                "toFro": True,
            }
        ],
        "depots": [
            {
                "id": 93,
                "cityId": 93,
                "name": "Gurgaon",
                "latitude": 28.4595,
                "longitude": 77.0266,
                "isActive": True,
                "description": "Cyber City",
                "activeDepot": True,
            }
        ],
        "vehicles": [
            {
                "id": 21,
                "registrationNumber": "HR55AY9237",
                "model": "AZAD 12.5 M Luxury intercity AC Coach",
                "make": "AZAD",
                "capacity": None,
                "status": "NOT_MOVING",
                "isActive": True,
                "fleetId": 1,
                "metrics": {
                    "id": "21",
                    "averageMileage": 0.726,
                    "kilometerRun": 5895.0,
                    "averageKilometerRun": 589.5,
                    "kilometersRunMtd": 5895.0,
                    "kilometersRunGoal": 4000.0,
                    "performanceStatus": "GREAT",
                },
                "recentInfo": {
                    "vehicleId": "16",
                    "timestamp": 1700000000.0,
                    "odometerReading": 144463.75,
                    "batSoc": 70.8,
                    "groundSpeedKmph": 0.0,
                    "latitude": 29.298817,
                    "longitude": 77.72057,
                    "acStatus": "b'Start'",
                    "vehicleStatus": "Idling AC OFF",
                    "id": "16",
                },
                "operational": True,
                "moving": False,
                "charging": False,
                "idle": False,
            },
            {
                "id": 35,
                "registrationNumber": "DL1PD8677",
                "metrics": None,
                "recentInfo": None,
            },
        ],
        "vehicleToRouteIds": {"21": 2, "35": 2},
        "vehicleToDepotIds": {"21": 93, "35": 93},
    }


def test_full_response_sanitization_fixes_all_real_bugs() -> None:
    sanitized = NaarniFleetTool._sanitize_response(_real_sample_response())

    # 1. The sparse `distance: null` field on the route is dropped.
    route = sanitized["routes"][0]
    assert "distance" not in route
    assert route["name"] == "Gurgaon to Dehradun"

    # 2. The first vehicle's recentInfo is fully cleaned.
    v21 = sanitized["vehicles"][0]
    assert v21["id"] == 21
    ri = v21["recentInfo"]
    # vehicleId has been renamed to deviceId
    assert "vehicleId" not in ri
    assert ri["deviceId"] == "16"
    # `id: "16"` also becomes deviceId (collides and overwrites — which is
    # fine because both pointed at the same device)
    # acStatus bytes literal is stripped
    assert ri["acStatus"] == "Start"
    # Timestamp converted to ISO + secondsAgo
    assert ri["timestamp"] == "2023-11-14T22:13:20+00:00"
    assert isinstance(ri["secondsAgo"], int)
    # Floats rounded to 3 decimals
    assert ri["latitude"] == 29.299
    assert ri["batSoc"] == 70.8

    # 3. The second vehicle has metrics=null and recentInfo=null. Both
    # should be dropped entirely from the sanitized payload.
    v35 = sanitized["vehicles"][1]
    assert v35["id"] == 35
    assert "metrics" not in v35
    assert "recentInfo" not in v35

    # 4. Metrics with verbose floats are rounded to 3 decimals (preserves
    # enough precision for kWh/km mileage to stay meaningful to a fleet
    # manager, while still compacting the 15-digit float noise).
    metrics = v21["metrics"]
    assert metrics["averageMileage"] == 0.726
    assert metrics["kilometerRun"] == 5895.0
    assert metrics["performanceStatus"] == "GREAT"

    # 5. The existing vehicleToRouteIds / vehicleToDepotIds mappings
    # survive the sanitization.
    assert sanitized["vehicleToRouteIds"] == {"21": 2, "35": 2}


def test_sanitize_response_compacts_payload_size() -> None:
    """The sanitized payload should be smaller than the raw one."""
    import json

    raw = _real_sample_response()
    sanitized = NaarniFleetTool._sanitize_response(raw)
    raw_size = len(json.dumps(raw))
    sanitized_size = len(json.dumps(sanitized))
    assert sanitized_size < raw_size, (
        f"sanitized payload ({sanitized_size}) should be smaller than "
        f"raw ({raw_size})"
    )


def test_sanitize_response_preserves_non_analytics_shapes() -> None:
    # Spring Pageable alerts shape (from /alerts) should pass through
    # cleanly without corruption.
    alerts = {
        "content": [
            {
                "id": "abc",
                "alertId": "AC Fault Code",
                "criticality": "CRITICAL",
                "details": "Alert activated",
            }
        ],
        "totalElements": 1,
        "totalPages": 1,
        "last": True,
        "size": 20,
    }
    out = NaarniFleetTool._sanitize_response(alerts)
    assert out["content"][0]["alertId"] == "AC Fault Code"
    assert out["totalElements"] == 1
    assert out["last"] is True


def test_sanitize_response_handles_bare_array() -> None:
    # /alert-definitions returns a bare array.
    defs = [
        {"id": 1, "name": "AC Fault Code", "criticality": "CRITICAL"},
        {"id": 2, "name": "Battery Temp High", "criticality": "WARNING"},
    ]
    out = NaarniFleetTool._sanitize_response(defs)
    assert len(out) == 2
    assert out[0]["name"] == "AC Fault Code"


def test_sanitize_response_handles_primitives() -> None:
    assert NaarniFleetTool._sanitize_response(42) == 42
    assert NaarniFleetTool._sanitize_response("hello") == "hello"
    assert NaarniFleetTool._sanitize_response(None) is None
    assert NaarniFleetTool._sanitize_response(True) is True


def test_sanitize_response_does_not_mutate_input() -> None:
    raw = _real_sample_response()
    import copy

    raw_copy = copy.deepcopy(raw)
    NaarniFleetTool._sanitize_response(raw)
    # The original should be unchanged
    assert raw == raw_copy
