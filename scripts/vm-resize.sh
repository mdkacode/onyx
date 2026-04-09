#!/bin/bash
# ============================================================================
# NaArNi VM Resize Script (runs ON the VM itself)
# ============================================================================
# Usage:
#   ./vm-resize.sh up      # Scale to E4s_v5 (4 vCPU, 32 GB)
#   ./vm-resize.sh down    # Scale to E2s_v5 (2 vCPU, 16 GB)
#   ./vm-resize.sh status  # Show current VM size
#
# IMPORTANT: When resizing, this script triggers an async Azure operation.
# The VM will deallocate (this script dies), Azure resizes it, then
# Azure starts it back up. Total downtime: ~3 minutes.
#
# The "start after resize" is handled by an Azure REST API call with
# --no-wait, so the command returns immediately before the VM goes down.
# ============================================================================

set -e

RG="naarni-cad-vm_group"
VM="naarni-ai-vm"
SUBSCRIPTION="d091bc43-1a97-4a25-b271-a9eb6616bb82"
SIZE_UP="Standard_E4s_v5"    # 4 vCPU, 32 GB — business hours
SIZE_DOWN="Standard_E2s_v5"  # 2 vCPU, 16 GB — off-hours

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] $*"; }

get_current_size() {
  curl -s -H 'Metadata:true' \
    'http://169.254.169.254/metadata/instance/compute/vmSize?api-version=2021-02-01&format=text' 2>/dev/null
}

resize_vm() {
  local TARGET="$1"
  local CURRENT
  CURRENT=$(get_current_size)

  log "Current: $CURRENT → Target: $TARGET"

  if [ "$CURRENT" = "$TARGET" ]; then
    log "Already at target size. Nothing to do."
    return 0
  fi

  # Get access token using az CLI (logged in as admin@naarni.com)
  log "Getting access token..."
  TOKEN=$(az account get-access-token --query accessToken -o tsv 2>/dev/null)

  if [ -z "$TOKEN" ]; then
    log "ERROR: Failed to get access token. Run 'az login --use-device-code' first."
    return 1
  fi

  API_URL="https://management.azure.com/subscriptions/${SUBSCRIPTION}/resourceGroups/${RG}/providers/Microsoft.Compute/virtualMachines/${VM}"

  # Step 1: Update the VM size (this takes effect on next start)
  log "Setting target size to $TARGET..."
  curl -s -X PATCH \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"properties\":{\"hardwareProfile\":{\"vmSize\":\"$TARGET\"}}}" \
    "${API_URL}?api-version=2024-03-01" > /dev/null

  # Step 2: Trigger deallocate+start via a single "restart" won't work for resize.
  # Instead, we use "reapply" or deallocate async then use az vm start with --no-wait.
  # But we need to deallocate for size change to take effect.
  #
  # Strategy: Use Azure's async operations.
  # 1. Trigger deallocate (async) — returns immediately
  # 2. Set up a background "start" that waits then starts the VM
  #    This uses Azure's built-in operation queue.

  log "Triggering async deallocate → resize → start..."

  # Fire deallocate asynchronously — this will kill us, but Azure continues
  # We use --no-wait so the command returns before the VM actually goes down
  az vm deallocate -g "$RG" -n "$VM" --no-wait 2>/dev/null

  log "Deallocate queued. VM will go down in ~10s, then Azure resizes and..."
  log "Queuing start operation..."

  # Queue a start operation. Azure will execute this after deallocation completes.
  # --no-wait returns immediately. Azure's Resource Manager sequences these operations.
  az vm start -g "$RG" -n "$VM" --no-wait 2>/dev/null

  log "Start queued. Azure will: deallocate → apply new size → start automatically."
  log "Expected downtime: ~3 minutes. Script exiting."
}

case "${1:-}" in
  up)
    log "=== SCALE UP (business hours) ==="
    resize_vm "$SIZE_UP"
    ;;
  down)
    log "=== SCALE DOWN (off-hours) ==="
    resize_vm "$SIZE_DOWN"
    ;;
  status)
    CURRENT=$(get_current_size)
    echo "VM: $VM"
    echo "Size: $CURRENT"
    echo "State: running"
    ;;
  *)
    echo "Usage: $0 {up|down|status}"
    exit 1
    ;;
esac
