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
from typing import Any
from typing import cast

import requests
from sqlalchemy.orm import Session
from typing_extensions import override

from onyx.chat.emitter import Emitter
from onyx.db.engine.sql_engine import get_session_with_current_tenant
from onyx.db.models import User
from onyx.db.naarni_auth import get_naarni_token_for_user
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
        }

    def _api_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{NAARNI_API_BASE_URL}{path}"
        resp = requests.get(url, headers=self._headers(), params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def _api_post(self, path: str, body: dict[str, Any]) -> Any:
        url = f"{NAARNI_API_BASE_URL}{path}"
        resp = requests.post(url, headers=self._headers(), json=body, timeout=15)
        resp.raise_for_status()
        return resp.json()

    # ── Action handlers ───────────────────────────────────────────────────────

    def _get_dashboard(self, params: dict[str, Any]) -> Any:  # noqa: ARG002
        return self._api_get("/api/v1/dashboard")

    def _list_vehicles(self, params: dict[str, Any]) -> Any:
        fleet_id = params.get("fleet_id")
        if fleet_id:
            return self._api_get(
                f"/api/v1/vehicles/fleet/{fleet_id}",
                {"page": params.get("page", 0), "limit": params.get("size", 20)},
            )
        return self._filter_vehicles(params)

    def _get_vehicle_details(self, params: dict[str, Any]) -> Any:
        vehicle_id = params.get("vehicle_id")
        if not vehicle_id:
            raise ToolCallException(
                message="vehicle_id is required for get_vehicle_details",
                llm_facing_message="Please provide a vehicle_id parameter.",
            )
        return self._api_get(f"/api/v1/vehicles/{vehicle_id}")

    def _filter_vehicles(self, params: dict[str, Any]) -> Any:
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
        return self._api_get("/api/v1/vehicles/filter", body)

    def _list_fleets(self, params: dict[str, Any]) -> Any:
        return self._api_get(
            "/api/v1/fleets",
            {"page": params.get("page", 0), "limit": params.get("size", 20)},
        )

    def _list_routes(self, params: dict[str, Any]) -> Any:  # noqa: ARG002
        return self._api_get("/api/v1/routes")

    def _get_performance(self, params: dict[str, Any]) -> Any:
        body: dict[str, Any] = {
            "timeRange": {
                "start": params.get("start_date", "2025-10-01T00:00:00"),
                "end": params.get("end_date", "2025-10-31T00:00:00"),
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
        body: dict[str, Any] = {
            "timeRange": {
                "start": params.get("start_date", "2025-10-01T00:00:00"),
                "end": params.get("end_date", "2025-10-31T00:00:00"),
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
        body: dict[str, Any] = {
            "timeRange": {
                "start": params.get("start_date", "2025-10-01T00:00:00"),
                "end": params.get("end_date", "2025-10-31T00:00:00"),
            },
        }
        if "vehicle_ids" in params:
            body["vehicleIds"] = params["vehicle_ids"]
        if "route_ids" in params:
            body["routeIds"] = params["route_ids"]
        return self._api_post("/api/v1/analytics/vehicle-analytics", body)

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
