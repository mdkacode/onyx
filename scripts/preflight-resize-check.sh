#!/usr/bin/env bash
# ============================================================================
# NaArNi VM Resize — Pre-flight Verification (READ-ONLY)
# ============================================================================
# Purpose: verify the Azure VM is safe to put on an auto-resize schedule.
# Runs six read-only checks. Makes NO changes to the VM, disks, containers,
# or network. Safe to run any time.
#
# Prerequisites on the machine running this script:
#   - az CLI installed (`brew install azure-cli` on macOS)
#   - Logged in:  az login
#   - ssh access to the VM as user 'naarni'
#
# Usage:
#   ./scripts/preflight-resize-check.sh
#
# Exit code: 0 if all six checks pass, 1 otherwise.
# ============================================================================

set -u

RG="naarni-cad-vm_group"
VM="naarni-ai-vm"
SUBSCRIPTION="d091bc43-1a97-4a25-b271-a9eb6616bb82"
VM_HOST="52.140.124.116"
SSH_USER="naarni"
EXPECTED_CURRENT_SIZE="Standard_E8s_v5"
TARGET_DOWN_SIZE="Standard_E4s_v5"

PASS=0
FAIL=0

ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; PASS=$((PASS+1)); }
bad()  { printf "  \033[31m✗\033[0m %s\n" "$1"; FAIL=$((FAIL+1)); }
info() { printf "  \033[36m·\033[0m %s\n" "$1"; }
hdr()  { printf "\n\033[1m==> %s\033[0m\n" "$1"; }

# --- 0. Ensure az is logged in and on the right subscription -----------------
hdr "0. Azure CLI session"
if ! az account show >/dev/null 2>&1; then
  bad "Not logged in. Run: az login"
  exit 1
fi
CURRENT_SUB=$(az account show --query id -o tsv)
if [ "$CURRENT_SUB" != "$SUBSCRIPTION" ]; then
  info "Switching subscription to $SUBSCRIPTION"
  az account set --subscription "$SUBSCRIPTION" || { bad "Could not switch subscription"; exit 1; }
fi
ACCT=$(az account show --query '{user:user.name, sub:name}' -o tsv | tr '\t' ' ')
ok "Logged in: $ACCT"

# --- 1. Public IP is static --------------------------------------------------
hdr "1. Public IP allocation method"
IP_INFO=$(az vm list-ip-addresses -g "$RG" -n "$VM" -o json 2>/dev/null)
if [ -z "$IP_INFO" ]; then
  bad "Could not fetch VM IP info"
else
  ALLOC=$(echo "$IP_INFO" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d[0]['virtualMachine']['network']['publicIpAddresses'][0].get('ipAllocationMethod','unknown'))" 2>/dev/null)
  PUB_IP=$(echo "$IP_INFO" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d[0]['virtualMachine']['network']['publicIpAddresses'][0].get('ipAddress','unknown'))" 2>/dev/null)
  info "Public IP: $PUB_IP"
  if [ "$ALLOC" = "Static" ]; then
    ok "IP allocation is Static — survives deallocate"
  else
    bad "IP allocation is '$ALLOC' — MUST be Static before scheduled resize. Fix with:"
    info "  az network public-ip update -g $RG -n <pip-name> --allocation-method Static"
  fi
fi

# --- 2. Current VM size matches expectation ----------------------------------
hdr "2. Current VM size"
CURRENT_SIZE=$(az vm show -g "$RG" -n "$VM" --query hardwareProfile.vmSize -o tsv 2>/dev/null)
info "Current size: $CURRENT_SIZE"
if [ "$CURRENT_SIZE" = "$EXPECTED_CURRENT_SIZE" ]; then
  ok "Matches expected $EXPECTED_CURRENT_SIZE"
else
  bad "Expected $EXPECTED_CURRENT_SIZE but found $CURRENT_SIZE — update the workflow SIZE_UP before running"
fi

# --- 3. Target down-size (E4s_v5) is available in this region ----------------
hdr "3. Resize availability — $TARGET_DOWN_SIZE"
if az vm list-vm-resize-options -g "$RG" -n "$VM" --query "[].name" -o tsv 2>/dev/null | grep -qx "$TARGET_DOWN_SIZE"; then
  ok "$TARGET_DOWN_SIZE is a valid resize target"
else
  bad "$TARGET_DOWN_SIZE is NOT offered as a resize option in this region"
fi

# --- 4. Docker root is on OS disk (not /mnt) ---------------------------------
hdr "4. Docker data on persistent disk (not /mnt tempdisk)"
DOCKER_ROOT=$(ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 "$SSH_USER@$VM_HOST" "docker info 2>/dev/null | grep 'Docker Root Dir' | awk -F: '{print \$2}' | xargs" 2>/dev/null)
if [ -z "$DOCKER_ROOT" ]; then
  bad "Could not SSH or read docker info"
else
  info "Docker Root Dir: $DOCKER_ROOT"
  if [[ "$DOCKER_ROOT" != /mnt* ]]; then
    ok "Docker data is on the OS/data disk (preserved across deallocate)"
  else
    bad "Docker data is on /mnt tempdisk — WILL BE LOST on deallocate. ABORT."
  fi
fi

# --- 5. /mnt has nothing critical --------------------------------------------
hdr "5. /mnt tempdisk contents"
MNT_CONTENT=$(ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 "$SSH_USER@$VM_HOST" "ls -la /mnt 2>/dev/null | tail -n +2" 2>/dev/null)
if [ -n "$MNT_CONTENT" ]; then
  info "Listing /mnt:"
  echo "$MNT_CONTENT" | sed 's/^/      /'
  # Anything other than . .. DATALOSS_WARNING_README.txt lost+found swapfile is suspicious
  SUSPICIOUS=$(echo "$MNT_CONTENT" | awk '{print $NF}' | grep -vxE '\.|\.\.|DATALOSS_WARNING_README\.txt|lost\+found|swapfile' | wc -l | tr -d ' ')
  if [ "$SUSPICIOUS" = "0" ]; then
    ok "/mnt has only expected tempdisk contents"
  else
    bad "/mnt has unexpected files — review manually before enabling schedule"
  fi
else
  bad "Could not list /mnt via SSH"
fi

# --- 6. Docker Compose file exists and containers are running ----------------
hdr "6. Docker Compose state"
COMPOSE_PS=$(ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 "$SSH_USER@$VM_HOST" "cd /opt/naarni/deployment/docker_compose && docker compose -f docker-compose.prod.yml -f docker-compose.naarni.yml ps --services --filter status=running 2>/dev/null" 2>/dev/null)
if [ -n "$COMPOSE_PS" ]; then
  COUNT=$(echo "$COMPOSE_PS" | wc -l | tr -d ' ')
  ok "$COUNT services currently running:"
  echo "$COMPOSE_PS" | sed 's/^/      /'
else
  bad "Could not read compose state"
fi

# --- Summary -----------------------------------------------------------------
hdr "Summary"
printf "  \033[32mPassed:\033[0m %d   \033[31mFailed:\033[0m %d\n\n" "$PASS" "$FAIL"

if [ "$FAIL" -eq 0 ]; then
  printf "  \033[32mAll checks passed.\033[0m Ready to take OS-disk snapshot and write workflows.\n"
  exit 0
else
  printf "  \033[31mFix the failures above before enabling the schedule.\033[0m\n"
  exit 1
fi
