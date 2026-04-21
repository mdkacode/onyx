# ONYX API Training Document — webApp-naarni

> **Purpose:** Train an ONYX (open-source) agent to understand every data API used by this application so it can correctly decide **what to call, when to call it, with which payload, and what to do with the response**.
>
> **Scope:** Naarni Core Backend data endpoints (`/api/v1/*`) on `https://api.naarni.com`. **Login, OTP, token issue, and token refresh are already working and are out of scope — ONYX will be given a valid `access_token` and `x-device-id` and can assume they stay valid.** If a call returns 401, the surrounding system will handle re-auth; ONYX just re-issues the call.
>
> **Audience:** An LLM being instructed via Claude prompt. Treat every section as authoritative.

---

## 1. High-Level Architecture

ONYX calls the Naarni Core Backend **directly** over HTTPS. There is no reverse proxy in ONYX's path.

- **Base URL:** `https://api.naarni.com`
- **Every path in this document is appended to that base URL.** E.g. `POST /api/v1/analytics/performance` → `POST https://api.naarni.com/api/v1/analytics/performance`.
- **Authentication (assumed available):** a valid JWT `access_token` plus the `deviceUuid` string the token was issued for. ONYX does NOT manage these — they are provided.

### One API namespace ONYX uses

| Prefix       | Host                     | Auth                        | Purpose                                    |
| ------------ | ------------------------ | --------------------------- | ------------------------------------------ |
| `/api/v1/*`  | `https://api.naarni.com` | JWT Bearer + `x-device-id`  | User profile, vehicles, analytics, alerts  |

---

## 2. Transport Rules (read these first)

These rules apply to EVERY call ONYX makes:

1. **Always use the absolute base URL `https://api.naarni.com`.**
2. **Required headers on every call:**
   - `Authorization: Bearer <accessToken>`
   - `x-device-id: <deviceUuid>`
   - `x-platform: WEB` (or `IOS` / `ANDROID` if the caller is a mobile client)
   - `Content-Type: application/json`
3. **Methods.** `GET` for read-only lookups. `POST` for analytics (bodies contain `timeRange`, `groupBy`, `selectFields`, etc. — see §4).
4. **Timeouts.** Use 10s for small metadata queries (`/users/me`, `/alert-definitions`), 30–60s for analytics aggregations.
5. **Error contract.**
   - Any non-2xx is an error.
   - On **401**: the access token is stale. ONYX should surface the error; the surrounding system refreshes the token and re-invokes ONYX. Do NOT attempt to handle refresh inside ONYX.
   - 400 → payload is malformed. Re-read the request rules in §4.
   - 5xx → retry once after ~1s; otherwise surface the error.

---

## 3. Naarni Core API Reference (`/api/v1/*`)

All endpoints below require the three headers in §2.

### 3.1 `GET /api/v1/users/me` — current user profile

- **Query params / body:** none.
- **Use when:** any screen loads — hydrates header (`name`, `role`, `avatar`).
- **Response (normalized):** `{ name, role, avatar, ... }`.

### 3.2 `GET /api/v1/dashboard/summary` — dashboard summary cards

- **Query params:** `startDate=YYYY-MM-DD`, `endDate=YYYY-MM-DD` (from current `dateRange`).
- **Body:** none.
- **Use when:** Dashboard screen loads or date range changes.
- **Response:** summary-card data (average mileage, kms run, active vehicles count, etc.).

### 3.3 `POST /api/v1/analytics/vehicle-activity` — vehicle activity aggregations

The most overloaded endpoint in the codebase. The **payload shape** determines the meaning.

#### Base payload

```json
{
  "timeRange": { "start": "2026-04-15T00:00:00", "end": "2026-04-20T23:59:59" },
  "groupBy": "VEHICLE | DEPOT | ROUTE | TIME",
  "status": "ACTIVE | INACTIVE",
  "timeGranularity": "DAY | WEEK | MONTH | HOUR",
  "depotIds": [123],
  "routeIds": [456],
  "vehicleIds": [3, 4, 5],
  "selectFields": ["INACTIVITY_MTD", "INACTIVITY_AGING", "KMS_GOAL", "ENERGY_CONSUMED", "ENERGY_REGENERATED", "AVERAGE_MILEAGE", "KILOMETER_RUN"],
  "orderBy": [{ "field": "INACTIVITY_AGING", "direction": "ASC" }]
}
```

#### Use cases (product intent → required payload rules)

| Intent                                | Required overrides                                                                                                                                                              |
| ------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Active / total vehicle count card** | `timeRange` must be **yesterday only** (`start` = yesterday 00:00:00, `end` = yesterday 23:59:59). Do NOT send `groupBy`, `status`, or filters. Product rule: "active = active yesterday" regardless of the user's date picker. |
| **Activity bar chart**                | `groupBy: "TIME"`. `timeGranularity` from the user's period toggle: `day → DAY` (1 bar), `week → DAY` (7 bars), `month → WEEK` (~4 bars).                                        |
| **Inactive vehicles table**           | `status: "INACTIVE"`, `groupBy: "VEHICLE"`. **`timeRange` is forced to the LAST 6 MONTHS** ending at the current `endDate`. `orderBy.field` ∈ `{INACTIVITY_AGING, INACTIVITY_MTD}`; default `INACTIVITY_AGING ASC`. Strip empty `depotIds`/`routeIds`. |
| **SLA vehicle uptime list**           | `status: "INACTIVE"`, `groupBy: "VEHICLE"`, `selectFields: ["INACTIVITY_MTD", "INACTIVITY_AGING"]`. Same 6-month `timeRange` override.                                           |
| **Kilometer-run per-vehicle list**    | `groupBy: "VEHICLE"`. **Strip `depotIds`/`routeIds` if the array is empty** — this endpoint rejects empty arrays for this intent.                                                |

### 3.4 `POST /api/v1/analytics/performance` — aggregated performance metrics

#### Base payload

```json
{
  "timeRange": { "start": "...T00:00:00", "end": "...T23:59:59" },
  "groupBy": "DEPOT | ROUTE | VEHICLE | TIME",
  "timeGranularity": "DAY | WEEK | MONTH | HOUR",
  "depotIds": [123],
  "routeIds": [456],
  "selectFields": ["KMS_GOAL", "ENERGY_CONSUMED", "ENERGY_REGENERATED", "AVERAGE_MILEAGE", "KILOMETER_RUN"],
  "orderBy": [{ "field": "AVERAGE_MILEAGE", "direction": "DESC" }]
}
```

#### Use cases (product intent → required payload)

| Intent                                            | Required body                                                                                                                                                                           |
| ------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Flat performance summary** (current date range) | `groupBy: "TIME"`, `selectFields: ["KMS_GOAL"]` (or the fields the screen needs).                                                                                                        |
| **Dashboard tabbed list** (depot / routes / vehicles) | `groupBy` matches the tab — `"depot" → DEPOT`, `"routes" → ROUTE`, `"vehicles" → VEHICLE`. `orderBy: [{field: "AVERAGE_MILEAGE", direction: "DESC"}]`.                               |
| **Depots dropdown options**                       | `groupBy: "DEPOT"`. If the UI has no selected date range yet, substitute "week ending yesterday" — never send `undefined`/`null` strings inside `timeRange`.                             |
| **Routes dropdown options**                       | `groupBy: "ROUTE"`. Same safe-default rule.                                                                                                                                              |
| **Average-mileage chart**                         | `groupBy: "TIME"`, `timeGranularity` mapped per §3.6.                                                                                                                                    |
| **Kilometer-run chart**                           | `groupBy: "TIME"`, `selectFields: ["KILOMETER_RUN"]` (or `KMS_GOAL`).                                                                                                                    |
| **Route-filtered chart**                          | `routeIds: [<selected>]`. Empty array = all routes (send it — don't strip).                                                                                                              |
| **Depot-filtered chart**                          | `depotIds: [<selected>]`. Empty array = all depots (send it — don't strip).                                                                                                              |
| **Vehicle energy chart** (vehicle detail)         | `groupBy: "TIME"`, `selectFields: ["ENERGY_CONSUMED", "ENERGY_REGENERATED"]`, `orderBy: [{field: "KILOMETER_RUN", direction: "DESC"}]`, `vehicleIds: [<id>]`.                            |
| **Vehicle detail KMS-goal chart**                 | `groupBy: "TIME"`, `status: "ACTIVE"`, `selectFields: ["KMS_GOAL"]`, `orderBy: [{field: "TIME", direction: "ASC"}]`, `vehicleIds: [<id>]`.                                              |

### 3.5 `POST /api/v1/analytics/vehicle-analytics` — per-vehicle detail

- **Body:**
  ```json
  {
    "timeRange": { "start": "...T00:00:00", "end": "...T23:59:59" },
    "vehicleIds": [3]
  }
  ```
- **Use when:** opening a Vehicle Detail page, or resolving the all-vehicles list. If `vehicleIds` is empty, DO NOT make the call.
- **Date resolution priority:** explicit request param > vehicle-detail-specific calendar state > global `dateRange` > default "week ending yesterday".

### 3.6 `timeGranularity` mapping (enforce before every analytics POST)

The backend accepts only uppercase enums. Map UI period labels to the correct enum **before** sending:

| UI period | `timeGranularity` to send |
| --------- | ------------------------- |
| `day`     | `DAY`                     |
| `week`    | `DAY` (7 daily bars)      |
| `month`   | `WEEK` (~4 weekly bars)   |
| `6m`      | `MONTH`                   |

If the period is already uppercase and one of `{DAY, WEEK, MONTH, HOUR}`, pass it through. Unknown → default to `DAY`.

### 3.7 `GET /api/v1/routes` — raw routes catalogue

- **Params:** none.
- **Response:** may be a bare array, `{ body: [...] }`, or `{ routes: [...] }` — normalize to one shape on the client side.

### 3.8 `GET /api/v1/sla/uptime-chart` — SLA uptime chart (backend source)

- **Auth:** Bearer.
- **Use when:** SLA Management screen. Returns the monthly uptime chart + stats.

### 3.9 `GET /api/v1/alerts` — critical alerts (paginated)

- **Query params (send only non-empty):**
  - `page`, `size` — pagination (0-indexed).
  - `alertDefinitionId`, `alertStatus`, `criticality`, `name`, `type`.
  - `startDate`, `endDate` — `YYYY-MM-DD`.
  - `search` — free-text. **Uppercase it and strip all whitespace before sending.**
- **Special rule:** when `search` has a non-empty value, **drop every other filter** except `search`, `startDate`, `endDate`, `page`, `size`.
- **Response (normalized):** `{ content: [...], totalElements, totalPages, size, number }`. If the backend ever returns a bare array, wrap it; if it returns `{ body | data | alerts: [...] }`, map that key to `content`.

### 3.10 `GET /api/v1/alert-definitions` — alert-type dropdown

- **Params:** none. Used to populate the Critical Alerts filter dropdown.

### 3.11 `POST /api/v1/superset/dashboard/{dashboardId}/filters` — Superset native filters

- **Auth:** NO Bearer, NO device headers — Superset session is separate.
- **Body:**
  ```json
  {
    "filters": [
      {
        "id": "NATIVE_FILTER-time_range",
        "name": "Time Range",
        "filterType": "filter_time | filter_select | filter_range",
        "datasetId": 42,
        "column": "event_time",
        "controlValues": { }
      }
    ]
  }
  ```
- **Use when:** programmatically provisioning filters for an embedded Superset dashboard. Not part of a normal screen load.

---

## 4. Decision Recipes — "When should ONYX call what?"

### Recipe A — "User opened screen X"
1. Always call `GET /api/v1/users/me` (header hydration).
2. Call the screen's primary data source(s). Examples:
   - Dashboard → `GET /api/v1/dashboard/summary` + `POST /api/v1/analytics/vehicle-activity` (active count, yesterday) + `POST /api/v1/analytics/performance` (tabbed list).
   - All Vehicles → `POST /api/v1/analytics/vehicle-analytics`.
   - Mileage / Kilometer Run / Depot Perf / Route Perf → `POST /api/v1/analytics/performance` (possibly multiple variants: main chart, KPI cards).
   - Critical Alerts → `GET /api/v1/alerts` + `GET /api/v1/alert-definitions`.
   - SLA Management → `GET /api/v1/sla/uptime-chart` + `POST /api/v1/analytics/vehicle-activity` (SLA list intent).
   - Vehicle Detail → `POST /api/v1/analytics/vehicle-analytics` + `POST /api/v1/analytics/performance` (energy chart + KMS-goal chart).
3. Fire these in parallel where possible — they are independent.

### Recipe B — "User changed date range"
- Refetch every data source whose body/query references the date range.
- Do NOT refetch `/users/me` or `/alert-definitions` — they don't depend on dates.
- Exception: the "active vehicles count" card always uses yesterday regardless — do NOT refetch it when the user changes the picker.

### Recipe C — "User typed in Critical Alerts search"
- Debounce ~300ms.
- Call `GET /api/v1/alerts` with `search=<UPPERCASE_NO_SPACES>`, plus `page`, `size`, `startDate`, `endDate`. Drop every other filter while search is active.

### Recipe D — "User clicked a vehicle row"
- Navigate to Vehicle Detail with that `vehicleId`.
- Fire `POST /api/v1/analytics/vehicle-analytics` with `vehicleIds: [<id>]`.
- Fire `POST /api/v1/analytics/performance` for the energy chart (`groupBy: TIME`, `selectFields: [ENERGY_CONSUMED, ENERGY_REGENERATED]`).
- Fire `POST /api/v1/analytics/performance` for the KMS-goal chart (`groupBy: TIME`, `selectFields: [KMS_GOAL]`, `status: ACTIVE`).

### Recipe E — "User toggled Dashboard tab (depot / routes / vehicles)"
- Refetch `POST /api/v1/analytics/performance` with `groupBy` mapped from the tab. Nothing else.

### Recipe F — "User picked a depot / route from dropdown"
- Update the stored selection; refetch the filtered analytics calls, injecting `depotIds` / `routeIds`. Empty array = all.

### Recipe G — "401 Unauthorized"
- Do NOT try to refresh inside ONYX. Surface the error. The surrounding system will refresh the token and re-invoke you.

---

## 5. CRITICAL Do's and Don'ts

**DO**
- Use `https://api.naarni.com` as the base URL.
- Always send `Authorization: Bearer <accessToken>`, `x-device-id: <deviceUuid>`, and `x-platform`.
- Uppercase every `timeGranularity` value (`DAY`, `WEEK`, `MONTH`, `HOUR`).
- Map UI periods per §3.6 — `week → DAY`, `month → WEEK`.
- For `active vehicles count`, force `timeRange` to yesterday regardless of the calendar — product decision.
- For `inactive vehicles list` and `SLA vehicle uptime list`, force `timeRange` to the last 6 months.
- Treat empty `depotIds` / `routeIds` arrays as "all" for depot/route performance intents — send them as `[]`.
- Strip empty `depotIds` / `routeIds` arrays for the kilometer-run per-vehicle list intent — the backend rejects them there.
- Uppercase + whitespace-strip `search` on `/api/v1/alerts`, and drop all other filters while search is active.
- Substitute "week ending yesterday" when `timeRange.start`/`end` would otherwise be undefined/null/non-ISO.
- Debounce user-typed search fields ~300ms before firing.
- Use 10s timeout for small lookups, 30–60s for analytics.

**DON'T**
- Don't hit `localhost` or any reverse-proxy URL — ONYX is outside the web app.
- Don't attempt to call token refresh, OTP, or device registration — those are out of scope for ONYX and handled elsewhere.
- Don't send `orderBy` with `direction: ""`; use `ASC` / `DESC` only.
- Don't send `groupBy` on the "active vehicles count" call — only `timeRange`.
- Don't send `depotIds` / `routeIds` on data sources whose intent doesn't include them.
- Don't send `timeRange` with `undefined` / `null` / non-ISO values — substitute the safe default.
- Don't call `/users/me` or `/alert-definitions` on date-range changes — they don't depend on dates.
- Don't send Bearer or device headers to `/api/v1/superset/*`.
- Don't paginate beyond what the backend response advertises (`totalPages`).

---

## 6. Minimal Example — authenticated analytics POST

```
POST https://api.naarni.com/api/v1/analytics/performance
Authorization: Bearer <accessToken>
x-device-id: <deviceUuid>
x-platform: WEB
Content-Type: application/json

{
  "timeRange": { "start": "2026-04-15T00:00:00", "end": "2026-04-20T23:59:59" },
  "groupBy": "DEPOT",
  "timeGranularity": "DAY",
  "selectFields": ["KMS_GOAL", "AVERAGE_MILEAGE"],
  "orderBy": [{ "field": "AVERAGE_MILEAGE", "direction": "DESC" }],
  "depotIds": [12, 17]
}
```

Expected: 2xx with a grouped array of performance rows, one per depot in the selected window. On 401, surface the error; the caller refreshes and retries.

---

### End of training document.
