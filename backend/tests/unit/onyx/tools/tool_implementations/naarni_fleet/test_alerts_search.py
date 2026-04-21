"""Unit tests for NaarniFleetTool._list_alerts search semantics.

Training.md §3.9: when `search` has a non-empty value, it must be uppercased
with whitespace stripped, and every other filter (except search, startDate,
endDate, page, size) must be dropped. The Naarni backend's free-text search
is an OR across multiple columns; mixing other filters narrows the result
to zero rows — so the web app explicitly drops them and ONYX must too.

These tests stub `_api_get` to capture the outgoing query params without
touching the network.
"""

from typing import Any

from onyx.tools.tool_implementations.naarni_fleet.naarni_fleet_tool import (
    NaarniFleetTool,
)


class _StubTool(NaarniFleetTool):
    """NaarniFleetTool subclass that records `_api_get` calls."""

    def __init__(self) -> None:
        # Skip the real __init__ — we don't need token/user wiring for
        # query-param assembly tests.
        self.captured: dict[str, Any] = {}

    def _api_get(  # type: ignore[override]
        self, path: str, params: dict[str, Any] | None = None
    ) -> Any:
        self.captured = {"path": path, "params": params or {}}
        return {"content": [], "totalElements": 0}


def test_alerts_uppercases_and_strips_whitespace_from_search() -> None:
    tool = _StubTool()
    tool._list_alerts({"search": "  hello world  "})
    assert tool.captured["params"]["search"] == "HELLOWORLD"


def test_alerts_search_drops_unrelated_filters() -> None:
    tool = _StubTool()
    tool._list_alerts(
        {
            "search": "battery",
            "criticality": "CRITICAL",
            "category": "AC",
            "alert_status": "TRIGGERED",
            "vehicleId": 7,
            "registrationNumber": "HR55",
            "start_date": "2026-04-01",
            "end_date": "2026-04-10",
            "page": 2,
            "size": 10,
        }
    )
    params = tool.captured["params"]
    # Allowed fields only
    assert params["search"] == "BATTERY"
    assert params["startDate"] == "2026-04-01"
    assert params["endDate"] == "2026-04-10"
    assert params["page"] == 2
    assert params["size"] == 10
    # Dropped
    for dropped in (
        "criticality",
        "category",
        "alertStatus",
        "alert_status",
        "vehicleId",
        "registrationNumber",
    ):
        assert dropped not in params, f"{dropped} should be dropped in search mode"


def test_alerts_empty_search_preserves_other_filters() -> None:
    tool = _StubTool()
    tool._list_alerts(
        {
            "search": "",
            "criticality": "CRITICAL",
            "alert_status": "TRIGGERED",
        }
    )
    params = tool.captured["params"]
    # Empty search is a no-op: normal filter behavior applies.
    assert "search" not in params
    assert params["criticality"] == "CRITICAL"
    assert params["alertStatus"] == "TRIGGERED"


def test_alerts_no_search_key_preserves_other_filters() -> None:
    tool = _StubTool()
    tool._list_alerts({"criticality": "WARNING"})
    assert tool.captured["params"]["criticality"] == "WARNING"
    assert "search" not in tool.captured["params"]


def test_alerts_whitespace_only_search_is_treated_as_empty() -> None:
    tool = _StubTool()
    tool._list_alerts({"search": "   ", "criticality": "CRITICAL"})
    params = tool.captured["params"]
    # "   " normalizes to "" → not active → other filters preserved.
    assert "search" not in params
    assert params["criticality"] == "CRITICAL"
