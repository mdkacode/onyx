"""Unit tests for NaarniFleetTool payload-shaping helpers.

Covers:
  - `_apply_intent_overrides` — enforces training.md §3.3/§3.4 product-intent
    rules (yesterday-only for active_vehicles_count, 6-month window for
    inactive_vehicles, etc.) server-side so the LLM can't send a malformed
    body.
  - `_map_time_granularity` — training.md §3.6 UI period → enum mapping
    (week → DAY, month → WEEK).
  - Response formatters — unit-suffixed metric key renames so the LLM never
    confuses km/kWh with kWh/km.

All functions under test are classmethods/staticmethods so the tests run
without a network, DB, or tool instance.
"""

from datetime import datetime
from datetime import timedelta
from datetime import timezone

import pytest

from onyx.tools.models import ToolCallException
from onyx.tools.tool_implementations.naarni_fleet.naarni_fleet_tool import (
    NaarniFleetTool,
)


# ─── _map_time_granularity ──────────────────────────────────────────────────


def test_ui_period_week_maps_to_day() -> None:
    # "last week" should produce 7 daily bars, not 1 weekly bar.
    assert NaarniFleetTool._map_time_granularity("week", None) == "DAY"


def test_ui_period_month_maps_to_week() -> None:
    assert NaarniFleetTool._map_time_granularity("month", None) == "WEEK"


def test_ui_period_6m_maps_to_month() -> None:
    assert NaarniFleetTool._map_time_granularity("6m", None) == "MONTH"


def test_ui_period_wins_over_fallback() -> None:
    # Even if LLM sends time_granularity=WEEK, ui_period=week wins and maps
    # to DAY (correct for a 7-day bar chart).
    assert NaarniFleetTool._map_time_granularity("week", "WEEK") == "DAY"


def test_time_granularity_passthrough_when_no_ui_period() -> None:
    assert NaarniFleetTool._map_time_granularity(None, "HOUR") == "HOUR"
    assert NaarniFleetTool._map_time_granularity(None, "day") == "DAY"


def test_time_granularity_unknown_falls_back_to_day() -> None:
    assert NaarniFleetTool._map_time_granularity(None, "YEARLY") == "DAY"


def test_time_granularity_none_returns_none() -> None:
    # No ui_period + no fallback → caller should omit the field entirely.
    assert NaarniFleetTool._map_time_granularity(None, None) is None


# ─── _apply_intent_overrides ────────────────────────────────────────────────


def _base_body() -> dict:
    return {
        "timeRange": {
            "start": "2026-01-01T00:00:00",
            "end": "2026-04-01T23:59:59",
        },
        "groupBy": "ROUTE",
        "status": "ACTIVE",
        "depotIds": [],
        "routeIds": [],
        "vehicleIds": [],
        "selectFields": ["KMS_GOAL"],
        "orderBy": [{"field": "AVERAGE_MILEAGE", "direction": "DESC"}],
        "timeGranularity": "WEEK",
    }


def test_active_vehicles_count_forces_yesterday_only_and_strips_filters() -> None:
    body = _base_body()
    out = NaarniFleetTool._apply_intent_overrides("active_vehicles_count", {}, body)

    # timeRange must be yesterday 00:00:00 → 23:59:59
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    assert out["timeRange"]["start"] == f"{yesterday}T00:00:00"
    assert out["timeRange"]["end"] == f"{yesterday}T23:59:59"

    # Every other filter/grouping field must be stripped — the card's
    # product definition is "active yesterday, fleet-wide".
    for stripped in (
        "groupBy",
        "status",
        "depotIds",
        "routeIds",
        "vehicleIds",
        "selectFields",
        "orderBy",
        "timeGranularity",
    ):
        assert stripped not in out, f"expected {stripped} to be removed"


def test_inactive_vehicles_forces_six_month_window_and_defaults() -> None:
    body = {
        "timeRange": {
            "start": "2026-04-01T00:00:00",
            "end": "2026-04-10T23:59:59",
        },
    }
    out = NaarniFleetTool._apply_intent_overrides("inactive_vehicles", {}, body)

    # Window must be 6 months ending at the user's endDate.
    end_dt = datetime.fromisoformat(out["timeRange"]["end"])
    start_dt = datetime.fromisoformat(out["timeRange"]["start"])
    assert end_dt.date().isoformat() == "2026-04-10"
    # 182 days = 6 months
    assert (end_dt - start_dt).days == 182

    assert out["status"] == "INACTIVE"
    assert out["groupBy"] == "VEHICLE"
    assert out["orderBy"] == [{"field": "INACTIVITY_AGING", "direction": "ASC"}]


def test_inactive_vehicles_respects_user_supplied_orderBy() -> None:
    body = {
        "timeRange": {"start": "", "end": "2026-04-10T23:59:59"},
        "orderBy": [{"field": "INACTIVITY_MTD", "direction": "DESC"}],
    }
    out = NaarniFleetTool._apply_intent_overrides("inactive_vehicles", {}, body)
    # User-supplied orderBy wins over the default.
    assert out["orderBy"] == [{"field": "INACTIVITY_MTD", "direction": "DESC"}]


def test_sla_uptime_sets_default_select_fields() -> None:
    body = {"timeRange": {"end": "2026-04-10T23:59:59"}}
    out = NaarniFleetTool._apply_intent_overrides("sla_uptime", {}, body)
    assert out["selectFields"] == ["INACTIVITY_MTD", "INACTIVITY_AGING"]
    assert out["status"] == "INACTIVE"
    assert out["groupBy"] == "VEHICLE"


def test_inactive_vehicles_strips_empty_depot_and_route_arrays() -> None:
    body = {
        "timeRange": {"end": "2026-04-10T23:59:59"},
        "depotIds": [],
        "routeIds": [],
    }
    out = NaarniFleetTool._apply_intent_overrides("inactive_vehicles", {}, body)
    # Backend rejects empty arrays for this intent.
    assert "depotIds" not in out
    assert "routeIds" not in out


def test_kms_per_vehicle_strips_empty_filters_and_sets_groupby() -> None:
    body = {
        "timeRange": {"start": "2026-04-01T00:00:00", "end": "2026-04-10T23:59:59"},
        "depotIds": [],
        "routeIds": [12],
    }
    out = NaarniFleetTool._apply_intent_overrides("kms_per_vehicle", {}, body)
    assert out["groupBy"] == "VEHICLE"
    assert "depotIds" not in out  # empty stripped
    assert out["routeIds"] == [12]  # non-empty preserved


def test_energy_chart_requires_vehicle_ids() -> None:
    body = {
        "timeRange": {"start": "2026-04-01T00:00:00", "end": "2026-04-10T23:59:59"},
    }
    with pytest.raises(ToolCallException):
        NaarniFleetTool._apply_intent_overrides("energy_chart", {}, body)


def test_energy_chart_sets_defaults() -> None:
    body = {
        "timeRange": {"start": "2026-04-01T00:00:00", "end": "2026-04-10T23:59:59"},
        "vehicleIds": [42],
    }
    out = NaarniFleetTool._apply_intent_overrides("energy_chart", {}, body)
    assert out["groupBy"] == "TIME"
    assert out["selectFields"] == ["ENERGY_CONSUMED", "ENERGY_REGENERATED"]
    assert out["orderBy"] == [{"field": "KILOMETER_RUN", "direction": "DESC"}]


def test_kms_goal_chart_forces_active_status() -> None:
    body = {
        "timeRange": {"start": "2026-04-01T00:00:00", "end": "2026-04-10T23:59:59"},
        "vehicleIds": [42],
    }
    out = NaarniFleetTool._apply_intent_overrides("kms_goal_chart", {}, body)
    assert out["groupBy"] == "TIME"
    assert out["status"] == "ACTIVE"
    assert out["selectFields"] == ["KMS_GOAL"]
    assert out["orderBy"] == [{"field": "TIME", "direction": "ASC"}]


def test_depot_dropdown_fills_default_time_range() -> None:
    body: dict = {}
    out = NaarniFleetTool._apply_intent_overrides("depot_dropdown", {}, body)
    assert out["groupBy"] == "DEPOT"
    # "week ending yesterday" — a valid 7-day window.
    start = datetime.fromisoformat(out["timeRange"]["start"])
    end = datetime.fromisoformat(out["timeRange"]["end"])
    assert (end - start).days == 6
    # end should be yesterday (UTC).
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    assert end.date() == yesterday


def test_route_dropdown_preserves_user_time_range() -> None:
    body = {
        "timeRange": {
            "start": "2026-03-15T00:00:00",
            "end": "2026-04-15T23:59:59",
        },
    }
    out = NaarniFleetTool._apply_intent_overrides("route_dropdown", {}, body)
    assert out["groupBy"] == "ROUTE"
    # User-supplied timeRange wins — dropdown default only kicks in when
    # timeRange is empty / missing.
    assert out["timeRange"]["start"] == "2026-03-15T00:00:00"


def test_unknown_intent_is_ignored() -> None:
    body = {"groupBy": "ROUTE"}
    out = NaarniFleetTool._apply_intent_overrides("foo_bar", {}, body)
    assert out == {"groupBy": "ROUTE"}


def test_no_intent_is_identity() -> None:
    body = {"groupBy": "ROUTE"}
    out = NaarniFleetTool._apply_intent_overrides(None, {}, body)
    assert out == {"groupBy": "ROUTE"}


# ─── Metric key rename (unit suffixes) ──────────────────────────────────────


def test_rename_metric_key_adds_unit_suffixes() -> None:
    assert (
        NaarniFleetTool._rename_metric_key("averageMileage")
        == "averageMileage_kmPerKwh"
    )
    assert NaarniFleetTool._rename_metric_key("kilometerRun") == "kilometerRun_km"
    assert NaarniFleetTool._rename_metric_key("energyConsumed") == "energyConsumed_kWh"
    assert NaarniFleetTool._rename_metric_key("idlingTime") == "idlingTime_seconds"


def test_rename_metric_key_passes_through_unknown_keys() -> None:
    # Counts and statuses stay dimensionless.
    assert NaarniFleetTool._rename_metric_key("activeCount") == "activeCount"
    assert NaarniFleetTool._rename_metric_key("vehicleId") == "vehicleId"


def test_format_performance_response_applies_unit_suffixes_for_routes() -> None:
    raw = {
        "routes": [
            {
                "id": 3,
                "name": "Delhi-Dehradun",
                "startCityName": "Delhi",
                "endCityName": "Dehradun",
                "metrics": {
                    "averageMileage": 0.82,
                    "kilometerRun": 1240.5,
                    "energyConsumed": 1510.2,
                },
            }
        ]
    }
    out = NaarniFleetTool._format_performance_response(raw)
    assert "routes" in out
    route = out["routes"][0]
    assert route["averageMileage_kmPerKwh"] == 0.82
    assert route["kilometerRun_km"] == 1240.5
    assert route["energyConsumed_kWh"] == 1510.2
    # Raw (un-suffixed) keys must not appear.
    assert "averageMileage" not in route
    assert "kilometerRun" not in route


def test_format_performance_response_time_group_uses_unit_suffixes() -> None:
    raw = {
        "timeGroups": [
            {
                "timeGroup": 1711929600,  # 2024-04-01
                "metrics": [{"kilometerRun": 180.0, "averageMileage": 0.91}],
            }
        ]
    }
    out = NaarniFleetTool._format_performance_response(raw)
    row = out["daily"][0]
    assert row["kilometerRun_km"] == 180.0
    assert row["averageMileage_kmPerKwh"] == 0.91


def test_format_vehicle_activity_response_time_group_uses_unit_suffixes() -> None:
    raw = {
        "timeGroups": [
            {
                "timeGroup": 1711929600,
                "metrics": [
                    {
                        "activeCount": 12,
                        "inactiveCount": 3,
                        "kmsRun": 450.0,
                    }
                ],
            }
        ]
    }
    out = NaarniFleetTool._format_vehicle_activity_response(raw)
    row = out["daily"][0]
    # Counts stay dimensionless.
    assert row["activeCount"] == 12
    assert row["inactiveCount"] == 3
    # Distance gets the km suffix.
    assert row["kmsRun_km"] == 450.0
