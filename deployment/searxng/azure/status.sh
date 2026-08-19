#!/usr/bin/env bash
# ============================================================================
# Read-only health + boundary check for the SearXNG relay. Creates nothing.
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=config.env disable=SC1091
source "${SCRIPT_DIR}/config.env"

BOLD=$'\033[1m'; GREEN=$'\033[32m'; RED=$'\033[31m'; RESET=$'\033[0m'
pass() { echo "  ${GREEN}✓${RESET} $*"; }
fail() { echo "  ${RED}✗${RESET} $*"; }

az account set --subscription "${SUBSCRIPTION_ID}"

echo "${BOLD}VM${RESET}"
POWER="$(az vm get-instance-view -g "${RESOURCE_GROUP}" -n "${SEARXNG_VM_NAME}" \
  --query "instanceView.statuses[?starts_with(code,'PowerState/')].displayStatus | [0]" -o tsv 2>/dev/null || echo "missing")"
SIZE="$(az vm show -g "${RESOURCE_GROUP}" -n "${SEARXNG_VM_NAME}" --query hardwareProfile.vmSize -o tsv 2>/dev/null || echo "-")"
if [[ "${POWER}" == "VM running" ]]; then
  pass "${SEARXNG_VM_NAME} ${POWER} (${SIZE})"
else
  fail "${SEARXNG_VM_NAME} ${POWER}"
fi

echo "${BOLD}Container${RESET}"
CONTAINER="$(az vm run-command invoke -g "${RESOURCE_GROUP}" -n "${SEARXNG_VM_NAME}" \
  --command-id RunShellScript --scripts "docker ps --filter name=searxng --format '{{.Status}}'" \
  --query "value[0].message" -o tsv 2>/dev/null | grep -i "up" || true)"
if [[ -n "${CONTAINER}" ]]; then
  pass "searxng container: ${CONTAINER##*stdout]}"
else
  fail "searxng container not running"
fi

echo "${BOLD}Reachability from Onyx (should succeed)${RESET}"
REACH="$(az vm run-command invoke -g "${RESOURCE_GROUP}" -n "${ONYX_VM_NAME}" \
  --command-id RunShellScript \
  --scripts "curl -sf -m 20 -X POST 'http://${SEARXNG_PRIVATE_IP}:${SEARXNG_PORT}/search' -d 'q=test&format=json' | head -c 120" \
  --query "value[0].message" -o tsv 2>/dev/null || true)"
if grep -q '"results"' <<<"${REACH}"; then
  pass "Onyx VM can query http://${SEARXNG_PRIVATE_IP}:${SEARXNG_PORT}"
else
  fail "Onyx VM cannot reach SearXNG"
fi

echo "${BOLD}Reachability from the internet (should FAIL)${RESET}"
PUBLIC_IP_ADDR="$(az network public-ip show -g "${RESOURCE_GROUP}" -n "${PUBLIC_IP_NAME}" --query ipAddress -o tsv 2>/dev/null || echo "")"
if [[ -z "${PUBLIC_IP_ADDR}" ]]; then
  pass "no public IP attached"
elif curl -sf -m 10 "http://${PUBLIC_IP_ADDR}:${SEARXNG_PORT}/" -o /dev/null 2>&1; then
  fail "PUBLICLY REACHABLE at ${PUBLIC_IP_ADDR}:${SEARXNG_PORT} -- fix the NSG now"
else
  pass "${PUBLIC_IP_ADDR}:${SEARXNG_PORT} refused from the internet (egress-only)"
fi

echo "${BOLD}NSG inbound rules${RESET}"
az network nsg rule list -g "${RESOURCE_GROUP}" --nsg-name "${NSG_NAME}" \
  --query "sort_by([?direction=='Inbound'],&priority)[].{priority:priority,name:name,access:access,source:sourceAddressPrefix,port:destinationPortRange}" \
  -o table 2>/dev/null || fail "NSG ${NSG_NAME} not found"
