"""Naarni Fleet Data Tool — gives the LLM live access to EV fleet data.

The LLM calls this tool when users ask about vehicles, routes, performance,
alerts, or any fleet operational data. It hits the Naarni backend APIs
using the *current user's* linked Naarni credentials and returns structured
JSON that the LLM uses to formulate its answer.

Auth flow:
    1. User links their Naarni account via POST /naarni-auth/request-otp + verify-otp
    2. Encrypted tokens are stored per-user in the naarni_user_token table
    3. This tool reads the user's token at runtime to make authenticated API calls

Environment variables:
    NAARNI_API_BASE_URL  — e.g. https://api.naarni.com
"""

import json
import os
import re
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import cast

import requests
from sqlalchemy.orm import Session
from typing_extensions import override

from onyx.chat.emitter import Emitter
from onyx.db.engine.sql_engine import get_session_with_current_tenant
from onyx.db.models import User
from onyx.db.naarni_auth import get_naarni_token_for_user
from onyx.server.features.naarni_auth.token_refresh import NaarniRefreshFailed
from onyx.server.features.naarni_auth.token_refresh import (
    refresh_user_naarni_token,
)
from onyx.server.query_and_chat.placement import Placement
from onyx.server.query_and_chat.streaming_models import CustomToolDelta
from onyx.server.query_and_chat.streaming_models import CustomToolStart
from onyx.server.query_and_chat.streaming_models import Packet
from onyx.tools.interface import Tool
from onyx.tools.models import CustomToolCallSummary
from onyx.tools.models import ToolCallException
from onyx.tools.models import ToolResponse
from onyx.utils.logger import setup_logger

logger = setup_logger()

# ── Constants ─────────────────────────────────────────────────────────────────

ACTION_FIELD = "action"
PARAMS_FIELD = "parameters"

NAARNI_API_BASE_URL = os.environ.get("NAARNI_API_BASE_URL", "")

VALID_ACTIONS = [
    "get_dashboard",
    "list_vehicles",
    "get_vehicle_details",
    "filter_vehicles",
    "list_fleets",
    "list_routes",
    "get_performance",
    "get_vehicle_activity",
    "get_vehicle_analytics",
    "list_alerts",
    "get_alert_definitions",
]


class NaarniFleetTool(Tool[None]):
    NAME = "naarni_fleet_data"
    DESCRIPTION = (
        "Query live data from the Naarni EV bus fleet management system. "
        "Use this tool when the user asks about vehicles, buses, fleet status, "
        "routes, depots, mileage, kilometers run, battery state of charge (SoC), "
        "energy consumption, vehicle performance, alerts, warnings, "
        "or any operational fleet data. "
        "Choose the appropriate 'action' and pass relevant 'parameters'."
    )
    DISPLAY_NAME = "Fleet Data"

    def __init__(
        self,
        tool_id: int,
        emitter: Emitter,
        user: User,
    ) -> None:
        super().__init__(emitter=emitter)
        self._id = tool_id
        self._user = user
        self._access_token: str | None = None

    def _resolve_token(self) -> str:
        """Look up the current user's Naarni access token from the DB.

        Caches the token for the lifetime of this tool instance (one chat turn).
        """
        if self._access_token:
            return self._access_token

        with get_session_with_current_tenant() as db_session:
            token_record = get_naarni_token_for_user(db_session, self._user.id)

        if not token_record or not token_record.access_token:
            raise ToolCallException(
                message=f"No Naarni token for user {self._user.id}",
                llm_facing_message=(
                    "Your Naarni fleet account is not linked. "
                    "Please go to Settings and connect your Naarni account "
                    "using your phone number first."
                ),
            )

        # Decrypt the SensitiveValue
        raw_token = token_record.access_token
        if hasattr(raw_token, "get_value"):
            self._access_token = raw_token.get_value(apply_mask=False)
        else:
            self._access_token = str(raw_token)

        return self._access_token

    def _force_refresh_token(self) -> str | None:
        """Attempt a server-side refresh of the user's Naarni token.

        Returns the new access token on success, or None if refresh failed
        (in which case the caller should surface the original 401 and tell
        the user to reconnect).
        """
        try:
            with get_session_with_current_tenant() as db_session:
                new_token = refresh_user_naarni_token(
                    db_session=db_session, user_id=self._user.id
                )
        except NaarniRefreshFailed as e:
            logger.warning(
                "Naarni auto-refresh failed for user %s: %s", self._user.id, e
            )
            return None
        self._access_token = new_token
        return new_token

    @property
    def id(self) -> int:
        return self._id

    @property
    def name(self) -> str:
        return self.NAME

    @property
    def description(self) -> str:
        return self.DESCRIPTION

    @property
    def display_name(self) -> str:
        return self.DISPLAY_NAME

    @override
    @classmethod
    def is_available(cls, db_session: Session) -> bool:
        """Available when the Naarni API base URL is configured."""
        return bool(os.environ.get("NAARNI_API_BASE_URL"))

    def tool_definition(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        ACTION_FIELD: {
                            "type": "string",
                            "enum": VALID_ACTIONS,
                            "description": (
                                "The fleet data action to perform. Options:\n"
                                "- get_dashboard: overall fleet summary (vehicle counts, devices)\n"
                                "- list_vehicles: list all vehicles with status\n"
                                "- get_vehicle_details: detailed info for one vehicle by ID\n"
                                "- filter_vehicles: filter vehicles by operator, route, device status\n"
                                "- list_fleets: list all fleets\n"
                                "- list_routes: list all routes\n"
                                "- get_performance: performance metrics (mileage, km run, energy) "
                                "optionally grouped by vehicle/route/depot/time\n"
                                "- get_vehicle_activity: activity metrics (active/inactive counts, "
                                "inactivity aging)\n"
                                "- get_vehicle_analytics: combined vehicle analytics with route/depot mapping\n"
                                "- list_alerts: list triggered alerts with optional filters\n"
                                "- get_alert_definitions: list all alert rule definitions"
                            ),
                        },
                        PARAMS_FIELD: {
                            "type": "object",
                            "description": (
                                "Optional parameters depending on the action. Common params:\n"
                                "- vehicle_id (int): vehicle ID for get_vehicle_details\n"
                                "- vehicle_ids (int[]): filter by specific vehicles\n"
                                "- route_ids (int[]): filter by routes\n"
                                "- fleet_id (int): filter by fleet\n"
                                "- start_date (string): ISO date like '2025-10-01T00:00:00'\n"
                                "- end_date (string): ISO date like '2025-10-31T00:00:00'\n"
                                "- group_by (string): 'VEHICLE', 'ROUTE', 'DEPOT', or 'TIME'\n"
                                "- time_granularity (string): 'DAY', 'WEEK', or 'MONTH' (when group_by=TIME)\n"
                                "- select_fields (string[]): extra fields like 'ENERGY_CONSUMED', "
                                "'ENERGY_REGENERATED', 'KMS_GOAL', 'ENERGY_IDLED', 'KILOMETERS_RUN_MTD'\n"
                                "- alert_status (string): 'TRIGGERED', 'RESOLVED'\n"
                                "- criticality (string): 'CRITICAL', 'WARNING'\n"
                                "- category (string): alert category like 'AC', 'MOST_IMP', 'CHARGING_BATTERY'\n"
                                "- page (int): page number, default 0\n"
                                "- size (int): page size, default 20"
                            ),
                        },
                    },
                    "required": [ACTION_FIELD],
                },
            },
        }

    def emit_start(self, placement: Placement) -> None:
        self.emitter.emit(
            Packet(
                placement=placement,
                obj=CustomToolStart(
                    tool_name=self.name,
                    tool_id=self._id,
                ),
            )
        )

    # ── API helpers ───────────────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        token = self._resolve_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "x-platform": "WEB",
        }

    @staticmethod
    def _unwrap_envelope(data: Any) -> Any:
        """Unwrap Naarni's standard response envelope if present.

        The Naarni backend is inconsistent: CRUD endpoints (dashboard,
        vehicles, fleets, routes, users, organizations) wrap their payload
        in a standard envelope:

            {"body": {...actual data...}, "statusCode": 200,
             "success": true, "errorMessage": null, "code": null}

        Analytics endpoints (/analytics/performance,
        /analytics/vehicle-activity, /analytics/vehicle-analytics) and the
        alerts endpoints (/alerts, /alert-definitions) return data
        BARE — no envelope at all. The Naarni web app at naarni.com
        handles this per-endpoint in dataSourceService.ts; ONYX needs
        the same treatment so the LLM doesn't waste tokens parsing a
        redundant envelope on every CRUD response.

        Detection is strict: we only unwrap when all three canonical
        fields (`body`, `statusCode`, `success`) are present together,
        so bare responses — including Spring Pageable shapes that
        happen to have a `content` field — pass through untouched.

        If the envelope has `success: false`, we surface the server's
        `errorMessage` as a ToolCallException. Upstream 4xx/5xx statuses
        are already handled by `resp.raise_for_status()` before this
        point, but `success: false` can come back with a 200 status
        code from some Spring handlers.
        """
        if not isinstance(data, dict):
            return data
        has_envelope = "body" in data and "statusCode" in data and "success" in data
        if not has_envelope:
            return data
        if not data.get("success", False):
            error_msg = (
                data.get("errorMessage")
                or data.get("error_message")
                or "Naarni API returned success=false"
            )
            raise ToolCallException(
                message=f"Naarni success=false: {error_msg}",
                llm_facing_message=(
                    f"The fleet data service rejected the request: {error_msg}"
                ),
            )
        return data.get("body")

    # ── Response sanitization ────────────────────────────────────────────────
    #
    # The Naarni analytics backend leaks a couple of data-quality issues that
    # confuse the LLM when it tries to answer the user's question:
    #
    #   1. `recentInfo.vehicleId` is actually a Trinity device id, NOT the
    #      Naarni vehicle id (the Naarni vehicle id lives in the parent
    #      object's `id` field). When the LLM sees
    #      `{id: 21, recentInfo: {vehicleId: "16"}}` it writes gibberish
    #      like "vehicle 16 is at location X" to the user.
    #
    #   2. `acStatus` comes back as `"b'Start'"` — a Python bytes literal
    #      leaked from the upstream data pipeline that ingests Trinity
    #      telemetry. The Java DTO expects `"Start"` / `"Stop"` per
    #      `AnalyticsUtils.getVehicleStatus()` but never gets there.
    #
    #   3. Unix-epoch float timestamps: "timestamp": 1775900338.009 is
    #      unhelpful to the LLM. It should see an ISO string + a relative
    #      age hint so it can say "5 minutes ago" instead of reading out
    #      raw epoch seconds.
    #
    #   4. Many fields are unnecessarily verbose floats
    #      (`averageMileage: 0.7259090909090911`). Rounding to 2 decimals
    #      keeps the data just as meaningful to the user and cuts token cost.
    #
    #   5. The envelope is big. A 50-vehicle fleet analytics response can
    #      easily exceed 30KB → the existing llm_facing_response truncation
    #      cuts mid-JSON and the LLM sees invalid data. The sanitizer drops
    #      nulls + empty collections to compact by ~30%.

    # Matches both `b'...'` and `b"..."` Python bytes literals at the start
    # of a string. Captures the inner content so we can preserve it.
    _BYTES_LITERAL_RE = re.compile(r"^b(['\"])(.*)\1$")

    @classmethod
    def _sanitize_string(cls, value: str) -> str:
        """Strip Python bytes literal wrappers from a string.

        `"b'Start'"` -> `"Start"`; regular strings pass through untouched.
        """
        m = cls._BYTES_LITERAL_RE.match(value)
        if m:
            return m.group(2)
        return value

    @staticmethod
    def _round_floats(value: Any) -> Any:
        """Round floats to 2 decimal places; leave other types untouched."""
        if isinstance(value, float):
            return round(value, 2)
        return value

    @classmethod
    def _sanitize_recent_info(cls, recent_info: Any) -> Any:
        """Clean up the `recentInfo` block attached to each vehicle.

        Rewrites to make the LLM's life easier:
          - Rename `vehicleId` -> `deviceId` (it was always a device id)
          - Strip `b'...'` wrappers from `acStatus`
          - Convert `timestamp` epoch -> ISO string + `secondsAgo`
          - Round lat/long/odo/speed/batSoc to 2 decimals
          - Drop keys with null values
        """
        if not isinstance(recent_info, dict):
            return recent_info

        cleaned: dict[str, Any] = {}
        for key, raw_value in recent_info.items():
            if raw_value is None:
                continue
            # The "vehicleId" inside recentInfo is actually the Trinity
            # device id. Rename it so the LLM doesn't confuse it with the
            # parent Naarni vehicle id.
            if key in ("vehicleId", "id"):
                cleaned["deviceId"] = str(raw_value)
                continue
            if key == "timestamp" and isinstance(raw_value, (int, float)):
                try:
                    ts = datetime.fromtimestamp(float(raw_value), tz=timezone.utc)
                    cleaned["timestamp"] = ts.isoformat()
                    seconds_ago = int(
                        datetime.now(timezone.utc).timestamp() - float(raw_value)
                    )
                    cleaned["secondsAgo"] = max(seconds_ago, 0)
                except (ValueError, OSError, OverflowError):
                    cleaned["timestamp"] = raw_value
                continue
            if key == "acStatus" and isinstance(raw_value, str):
                cleaned[key] = cls._sanitize_string(raw_value)
                continue
            if key == "vehicleStatus" and isinstance(raw_value, str):
                cleaned[key] = cls._sanitize_string(raw_value)
                continue
            cleaned[key] = cls._round_floats(raw_value)
        return cleaned

    @classmethod
    def _sanitize_response(cls, data: Any) -> Any:
        """Walk the JSON response and clean up data-quality issues.

        Entry point for the sanitizer. Handles:
          - dicts: recurse into each value, strip nulls for known-sparse
            fields, apply `_sanitize_recent_info` on any `recentInfo` block
          - lists: recurse into each element
          - strings: strip bytes literal wrappers
          - floats: round to 2 decimals

        This runs AFTER `_unwrap_envelope`, so it only sees the payload
        the LLM will actually consume.
        """
        if isinstance(data, dict):
            cleaned: dict[str, Any] = {}
            for key, raw_value in data.items():
                if key == "recentInfo":
                    recent = cls._sanitize_recent_info(raw_value)
                    if recent:
                        cleaned[key] = recent
                    continue
                # Drop null values on vehicle/route/depot dicts — they
                # pad the payload without adding information.
                if raw_value is None:
                    continue
                cleaned[key] = cls._sanitize_response(raw_value)
            return cleaned
        if isinstance(data, list):
            return [cls._sanitize_response(item) for item in data]
        if isinstance(data, str):
            return cls._sanitize_string(data)
        return cls._round_floats(data)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        """Send a request to Naarni, retrying once on 401 after a token refresh.

        The Naarni access token is ~6 hours, and a user may come back to
        ONYX hours after linking. Instead of immediately 401-ing them with
        "please reconnect", we make one best-effort refresh attempt using
        the stored refresh token. If that also fails, we fall through to
        the original HTTPError handler in `run()` which surfaces a nice
        reconnect message.

        Responses are passed through two transforms before reaching the
        LLM:
          1. `_unwrap_envelope` — flattens Naarni's CRUD
             `{body, statusCode, success, ...}` wrapper
          2. `_sanitize_response` — normalizes data-quality issues
             (Python bytes literals in acStatus, wrong `vehicleId` inside
             `recentInfo`, unhelpful epoch timestamps, verbose floats, and
             null-padded fields) so the LLM gets clean, compact JSON
        """
        url = f"{NAARNI_API_BASE_URL}{path}"
        resp = requests.request(
            method,
            url,
            headers=self._headers(),
            params=params,
            json=json_body,
            timeout=15,
        )

        if resp.status_code == 401:
            new_token = self._force_refresh_token()
            if new_token is not None:
                resp = requests.request(
                    method,
                    url,
                    headers=self._headers(),
                    params=params,
                    json=json_body,
                    timeout=15,
                )

        resp.raise_for_status()
        return self._sanitize_response(self._unwrap_envelope(resp.json()))

    def _api_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Simple GET with optional query params (e.g. /fleets?page=0&limit=20)."""
        return self._request("GET", path, params=params)

    def _api_get_with_body(self, path: str, body: dict[str, Any]) -> Any:
        """GET with JSON body — used by /vehicles/filter (Spring disableBodyPruning)."""
        return self._request("GET", path, json_body=body)

    def _api_post(self, path: str, body: dict[str, Any]) -> Any:
        """POST with JSON body (analytics endpoints)."""
        return self._request("POST", path, json_body=body)

    @staticmethod
    def _default_time_range() -> dict[str, str]:
        """Today's date range as default for analytics queries."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return {"start": f"{today}T00:00:00", "end": f"{today}T23:59:59"}

    # ── Action handlers ───────────────────────────────────────────────────────
    # Matches Postman collection: Naarni Backend

    def _get_dashboard(self, params: dict[str, Any]) -> Any:  # noqa: ARG002
        """GET /api/v1/dashboard — requires org + role."""
        return self._api_get("/api/v1/dashboard")

    def _list_vehicles(self, params: dict[str, Any]) -> Any:
        """GET /api/v1/vehicles/fleet/{id} or filter."""
        fleet_id = params.get("fleet_id")
        if fleet_id:
            return self._api_get(
                f"/api/v1/vehicles/fleet/{fleet_id}",
                {"page": params.get("page", 0), "limit": params.get("size", 20)},
            )
        return self._filter_vehicles(params)

    def _get_vehicle_details(self, params: dict[str, Any]) -> Any:
        """GET /api/v1/vehicles/{id}"""
        vehicle_id = params.get("vehicle_id")
        if not vehicle_id:
            raise ToolCallException(
                message="vehicle_id is required for get_vehicle_details",
                llm_facing_message="Please provide a vehicle_id parameter.",
            )
        return self._api_get(f"/api/v1/vehicles/{vehicle_id}")

    def _filter_vehicles(self, params: dict[str, Any]) -> Any:
        """GET /api/v1/vehicles/filter — GET with JSON body (Postman: disableBodyPruning)."""
        body: dict[str, Any] = {
            "filterContext": {},
            "page": {"page": params.get("page", 0), "size": params.get("size", 50)},
            "select": [
                "FLEET_ID",
                "OPERATOR_ID",
                "ROUTE_ID",
                "DEVICE_ID",
                "VEHICLE",
                "FLEET",
                "ROUTE",
                "DEVICE",
            ],
        }
        if "operator_ids" in params:
            body["filterContext"]["operatorIds"] = params["operator_ids"]
        if "registration_numbers" in params:
            body["filterContext"]["registrationNumbers"] = params[
                "registration_numbers"
            ]
        if "has_active_device" in params:
            body["filterContext"]["hasActiveDevice"] = params["has_active_device"]
        return self._api_get_with_body("/api/v1/vehicles/filter", body)

    def _list_fleets(self, params: dict[str, Any]) -> Any:
        """GET /api/v1/fleets?page=0&limit=20"""
        return self._api_get(
            "/api/v1/fleets",
            {"page": params.get("page", 0), "limit": params.get("size", 20)},
        )

    def _list_routes(self, params: dict[str, Any]) -> Any:  # noqa: ARG002
        """GET /api/v1/routes"""
        return self._api_get("/api/v1/routes")

    def _get_performance(self, params: dict[str, Any]) -> Any:
        """POST /api/v1/analytics/performance"""
        default_range = self._default_time_range()
        body: dict[str, Any] = {
            "timeRange": {
                "start": params.get("start_date", default_range["start"]),
                "end": params.get("end_date", default_range["end"]),
            },
        }
        if "group_by" in params:
            body["groupBy"] = params["group_by"]
        if "time_granularity" in params:
            body["timeGranularity"] = params["time_granularity"]
        if "order_by" in params:
            body["orderBy"] = params["order_by"]
        if "select_fields" in params:
            body["selectFields"] = params["select_fields"]
        if "vehicle_ids" in params:
            body["vehicleIds"] = params["vehicle_ids"]
        if "route_ids" in params:
            body["routeIds"] = params["route_ids"]
        return self._api_post("/api/v1/analytics/performance", body)

    def _get_vehicle_activity(self, params: dict[str, Any]) -> Any:
        """POST /api/v1/analytics/vehicle-activity"""
        default_range = self._default_time_range()
        body: dict[str, Any] = {
            "timeRange": {
                "start": params.get("start_date", default_range["start"]),
                "end": params.get("end_date", default_range["end"]),
            },
        }
        if "group_by" in params:
            body["groupBy"] = params["group_by"]
        if "time_granularity" in params:
            body["timeGranularity"] = params["time_granularity"]
        if "order_by" in params:
            body["orderBy"] = params["order_by"]
        if "select_fields" in params:
            body["selectFields"] = params["select_fields"]
        if "vehicle_ids" in params:
            body["vehicleIds"] = params["vehicle_ids"]
        if "route_ids" in params:
            body["routeIds"] = params["route_ids"]
        if "status" in params:
            body["status"] = params["status"]
        return self._api_post("/api/v1/analytics/vehicle-activity", body)

    def _get_vehicle_analytics(self, params: dict[str, Any]) -> Any:
        """POST /api/v1/analytics/vehicle-analytics"""
        default_range = self._default_time_range()
        body: dict[str, Any] = {
            "timeRange": {
                "start": params.get("start_date", default_range["start"]),
                "end": params.get("end_date", default_range["end"]),
            },
        }
        if "vehicle_ids" in params:
            body["vehicleIds"] = params["vehicle_ids"]
        if "route_ids" in params:
            body["routeIds"] = params["route_ids"]
        result = self._api_post("/api/v1/analytics/vehicle-analytics", body)
        return self._format_vehicle_analytics_response(result)

    @staticmethod
    def _format_vehicle_analytics_response(data: Any) -> Any:
        """Denormalize the vehicle-analytics API response for LLM readability.

        The raw API response uses ID-based lookup maps that require cross-
        referencing three separate arrays.  The LLM is error-prone when doing
        these lookups itself (e.g. confusing vehicle ID 34 with route ID 34).

        This method flattens the data so each vehicle entry already contains
        its resolved route name and depot name, plus all metrics and live-status
        fields at the top level with descriptive key names.

        Raw shape:
          routes[]  depots[]  vehicles[{metrics, recentInfo}]
          vehicleToRouteIds  vehicleToDepotIds

        Returned shape: flat vehicle list with route/depot names embedded.
        """
        if not isinstance(data, dict):
            return data

        # Build lookup indexes keyed by integer ID
        route_index: dict[int, str] = {}
        for r in data.get("routes", []):
            if isinstance(r, dict) and "id" in r:
                route_index[int(r["id"])] = r.get("name") or f"Route {r['id']}"

        depot_index: dict[int, str] = {}
        for d in data.get("depots", []):
            if isinstance(d, dict) and "id" in d:
                depot_index[int(d["id"])] = d.get("name") or f"Depot {d['id']}"

        # vehicleToRouteIds / vehicleToDepotIds use string keys in JSON
        v_to_route: dict[str, Any] = data.get("vehicleToRouteIds") or {}
        v_to_depot: dict[str, Any] = data.get("vehicleToDepotIds") or {}

        enriched: list[dict[str, Any]] = []
        for v in data.get("vehicles", []):
            if not isinstance(v, dict):
                continue

            vid = v.get("id")
            vid_str = str(vid) if vid is not None else None

            raw_route_id = v_to_route.get(vid_str) if vid_str else None
            raw_depot_id = v_to_depot.get(vid_str) if vid_str else None
            route_name: str | None = None
            depot_name: str | None = None
            if raw_route_id is not None:
                try:
                    route_name = route_index.get(int(raw_route_id))
                except (TypeError, ValueError):
                    pass
            if raw_depot_id is not None:
                try:
                    depot_name = depot_index.get(int(raw_depot_id))
                except (TypeError, ValueError):
                    pass

            metrics: dict[str, Any] = v.get("metrics") or {}
            recent: dict[str, Any] = v.get("recentInfo") or {}

            entry: dict[str, Any] = {
                "vehicleId": vid,
                "registrationNumber": v.get("registrationNumber"),
                "operationalStatus": v.get("status"),
                "assignedRoute": route_name,
                "assignedDepot": depot_name,
                # Performance metrics (over the requested time range)
                "averageMileage_kmPerKwh": metrics.get("averageMileage"),
                "kilometerRun": metrics.get("kilometerRun"),
                "performanceStatus": metrics.get("performanceStatus"),
                # Live / most-recent telemetry
                "batterySOC_percent": recent.get("batSoc"),
                "speedKmph": recent.get("groundSpeedKmph"),
                "liveVehicleStatus": recent.get("vehicleStatus"),
            }
            # Drop None values to reduce token usage
            entry = {k: val for k, val in entry.items() if val is not None}
            enriched.append(entry)

        return {
            "totalVehicles": len(enriched),
            "routes": [
                {"id": r.get("id"), "name": r.get("name")}
                for r in data.get("routes", [])
                if isinstance(r, dict)
            ],
            "depots": [
                {"id": d.get("id"), "name": d.get("name")}
                for d in data.get("depots", [])
                if isinstance(d, dict)
            ],
            "vehicles": enriched,
        }

    def _list_alerts(self, params: dict[str, Any]) -> Any:
        query_params: dict[str, Any] = {
            "page": params.get("page", 0),
            "size": params.get("size", 20),
        }
        for key in [
            "alertStatus",
            "alert_status",
            "criticality",
            "category",
            "vehicleId",
            "vehicle_id",
            "registrationNumber",
            "registration_number",
            "alertDefinitionId",
            "alert_definition_id",
            "startDate",
            "start_date",
            "endDate",
            "end_date",
            "search",
        ]:
            if key in params:
                api_key = key
                if "_" in key:
                    parts = key.split("_")
                    api_key = parts[0] + "".join(p.capitalize() for p in parts[1:])
                query_params[api_key] = params[key]
        return self._api_get("/api/v1/alerts", query_params)

    def _get_alert_definitions(self, params: dict[str, Any]) -> Any:  # noqa: ARG002
        return self._api_get("/api/v1/alert-definitions")

    # ── Main run ──────────────────────────────────────────────────────────────

    def run(
        self,
        placement: Placement,
        override_kwargs: None = None,  # noqa: ARG002
        **llm_kwargs: Any,
    ) -> ToolResponse:
        action = cast(str, llm_kwargs.get(ACTION_FIELD, ""))
        params = cast(dict[str, Any], llm_kwargs.get(PARAMS_FIELD, {}))

        if action not in VALID_ACTIONS:
            raise ToolCallException(
                message=f"Invalid action: {action}",
                llm_facing_message=(
                    f"Unknown action '{action}'. "
                    f"Valid actions: {', '.join(VALID_ACTIONS)}"
                ),
            )

        handler_map: dict[str, Any] = {
            "get_dashboard": self._get_dashboard,
            "list_vehicles": self._list_vehicles,
            "get_vehicle_details": self._get_vehicle_details,
            "filter_vehicles": self._filter_vehicles,
            "list_fleets": self._list_fleets,
            "list_routes": self._list_routes,
            "get_performance": self._get_performance,
            "get_vehicle_activity": self._get_vehicle_activity,
            "get_vehicle_analytics": self._get_vehicle_analytics,
            "list_alerts": self._list_alerts,
            "get_alert_definitions": self._get_alert_definitions,
        }

        try:
            result = handler_map[action](params)
        except ToolCallException:
            raise
        except requests.exceptions.Timeout:
            raise ToolCallException(
                message=f"Naarni API timeout for action={action}",
                llm_facing_message=(
                    "The fleet data service took too long to respond. "
                    "Please try again or narrow your query."
                ),
            )
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else "unknown"
            logger.error(f"Naarni API error: {status} for {action}")
            if status == 401:
                raise ToolCallException(
                    message=f"Naarni token expired for user {self._user.id}",
                    llm_facing_message=(
                        "Your Naarni session has expired. "
                        "Please re-link your account in Settings."
                    ),
                )
            if status == 403:
                raise ToolCallException(
                    message=f"Naarni API 403 for {action}, user {self._user.id}",
                    llm_facing_message=(
                        f"Your Naarni account does not have permission for '{action}'. "
                        "Try a different query — for example, listing fleets, routes, "
                        "or vehicles may work. The dashboard and analytics endpoints "
                        "require your Naarni account to be assigned to an organization."
                    ),
                )
            raise ToolCallException(
                message=f"Naarni API returned {status} for {action}",
                llm_facing_message=(
                    f"The fleet data service returned an error (HTTP {status}). "
                    "The request may be invalid or the service may be temporarily unavailable."
                ),
            )
        except requests.exceptions.ConnectionError:
            raise ToolCallException(
                message="Cannot connect to Naarni API",
                llm_facing_message=(
                    "Unable to reach the fleet data service. "
                    "It may be temporarily down."
                ),
            )

        # Emit result to the streaming frontend
        self.emitter.emit(
            Packet(
                placement=placement,
                obj=CustomToolDelta(
                    tool_name=self.name,
                    tool_id=self._id,
                    response_type="json",
                    data=result,
                ),
            )
        )

        llm_response = json.dumps(result)

        # Truncate if absurdly large so we don't blow the LLM context
        if len(llm_response) > 30_000:
            llm_response = llm_response[:30_000] + "\n... (truncated, data too large)"

        return ToolResponse(
            rich_response=CustomToolCallSummary(
                tool_name=self.name,
                response_type="json",
                tool_result=result,
            ),
            llm_facing_response=llm_response,
        )
