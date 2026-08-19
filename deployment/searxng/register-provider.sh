#!/usr/bin/env bash
# ============================================================================
# Point Onyx at the private SearXNG relay.
#
# Equivalent to filling in Admin Panel -> Web Search -> SearXNG, but scriptable
# so a rebuilt environment can be reconfigured without clicking through the UI.
#
#   ONYX_API_KEY=on_... ./register-provider.sh
#   ONYX_API_KEY=on_... SEARXNG_URL=http://172.16.0.10:8080 ./register-provider.sh
#
# Run it from the Onyx VM (or anywhere that can reach both Onyx and SearXNG).
# Create the API key under Admin Panel -> API Keys with admin permissions.
# ============================================================================
set -euo pipefail

: "${ONYX_URL:=http://localhost:3000}"
: "${SEARXNG_URL:=http://172.16.0.10:8080}"
: "${PROVIDER_NAME:=SearXNG (private)}"

# Onyx asks for up to 20 results per query; 3 pages is enough to fill that from
# SearXNG's merged engine output without paying for pages nobody reads.
: "${NUM_RESULTS:=20}"
: "${MAX_PAGES:=3}"
: "${TIMEOUT_SECONDS:=15}"
: "${LANGUAGE:=en}"

if [[ -z "${ONYX_API_KEY:-}" ]]; then
  echo "ERROR: set ONYX_API_KEY (Admin Panel -> API Keys, admin permissions)." >&2
  exit 1
fi

api() {
  local method=$1 path=$2 body=${3:-}
  if [[ -n "${body}" ]]; then
    curl -sS -X "${method}" "${ONYX_URL}${path}" \
      -H "Authorization: Bearer ${ONYX_API_KEY}" \
      -H "Content-Type: application/json" \
      -d "${body}"
  else
    curl -sS -X "${method}" "${ONYX_URL}${path}" \
      -H "Authorization: Bearer ${ONYX_API_KEY}"
  fi
}

CONFIG_JSON=$(cat <<JSON
{
  "searxng_base_url": "${SEARXNG_URL}",
  "num_results": "${NUM_RESULTS}",
  "max_pages": "${MAX_PAGES}",
  "timeout_seconds": "${TIMEOUT_SECONDS}",
  "language": "${LANGUAGE}"
}
JSON
)

# Validate before saving, so a bad URL surfaces here rather than as failed
# searches inside someone's chat.
echo "==> Testing ${SEARXNG_URL}"
TEST_RESULT="$(api POST /api/admin/web-search/search-providers/test \
  "{\"provider_type\":\"searxng\",\"config\":${CONFIG_JSON}}")"

if ! grep -q '"status"' <<<"${TEST_RESULT}"; then
  echo "    FAILED: ${TEST_RESULT}" >&2
  exit 1
fi
echo "    ok"

echo "==> Registering and activating provider"
RESULT="$(api POST /api/admin/web-search/search-providers \
  "{\"name\":\"${PROVIDER_NAME}\",\"provider_type\":\"searxng\",\"config\":${CONFIG_JSON},\"activate\":true}")"
echo "    ${RESULT}"

echo
echo "Done. Web Search is now backed by the private SearXNG relay."
echo "Content fetching should stay on the built-in Onyx Web Crawler --"
echo "Firecrawl would send every URL Onyx opens to a third-party SaaS."
