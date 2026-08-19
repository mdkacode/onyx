#!/usr/bin/env bash
# ============================================================================
# Delete the SearXNG relay and everything provision.sh created.
#
# Deletes in dependency order (VM -> NIC -> public IP -> NSG) so no orphaned
# disks / NICs / IPs are left billing, which is exactly how the last round of
# Azure waste accumulated. The OS disk is found and deleted explicitly because
# `az vm delete` leaves it behind.
#
# Touches ONLY resources named in config.env. Never touches the Onyx VM.
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=config.env disable=SC1091
source "${SCRIPT_DIR}/config.env"

az account set --subscription "${SUBSCRIPTION_ID}"

echo "This will DELETE, in resource group ${RESOURCE_GROUP}:"
echo "  VM        ${SEARXNG_VM_NAME}  (+ its OS disk)"
echo "  NIC       ${NIC_NAME}"
echo "  Public IP ${PUBLIC_IP_NAME}"
echo "  NSG       ${NSG_NAME}"
echo
read -r -p "Type the VM name to confirm: " CONFIRM
[[ "${CONFIRM}" == "${SEARXNG_VM_NAME}" ]] || { echo "Aborted."; exit 1; }

OS_DISK="$(az vm show -g "${RESOURCE_GROUP}" -n "${SEARXNG_VM_NAME}" \
  --query "storageProfile.osDisk.managedDisk.id" -o tsv 2>/dev/null || true)"

for cmd in \
  "vm delete -g ${RESOURCE_GROUP} -n ${SEARXNG_VM_NAME} --yes" \
  "network nic delete -g ${RESOURCE_GROUP} -n ${NIC_NAME}" \
  "network public-ip delete -g ${RESOURCE_GROUP} -n ${PUBLIC_IP_NAME}" \
  "network nsg delete -g ${RESOURCE_GROUP} -n ${NSG_NAME}"
do
  echo "==> az ${cmd}"
  # shellcheck disable=SC2086
  az ${cmd} || echo "    (already gone)"
done

if [[ -n "${OS_DISK}" ]]; then
  echo "==> deleting OS disk"
  az disk delete --ids "${OS_DISK}" --yes || echo "    (already gone)"
fi

echo "Done. Remember to deactivate the SearXNG provider in the Onyx admin panel."
