#!/usr/bin/env bash
# ============================================================================
# Provision the private SearXNG relay VM on Azure.
# ----------------------------------------------------------------------------
#   ./provision.sh --dry-run     # print every az command, change nothing
#   ./provision.sh               # create (idempotent -- safe to re-run)
#
# What it builds, and why it is shaped this way:
#
#   * A VM in the SAME VNet/subnet as the Onyx VM, with a STATIC private IP.
#     Onyx reaches it at http://<private-ip>:8080 -- traffic never leaves the
#     VNet, so the search *requests* are not exposed on the public internet.
#
#   * A public IP that is used for OUTBOUND ONLY. Azure retired default
#     outbound access, so a VM with no public IP cannot reach the internet at
#     all -- which would defeat the entire purpose. Inbound on that address is
#     denied at the NSG.
#
#   * An NSG that denies everything inbound (Internet *and* the rest of the
#     VNet) except port 8080 and SSH from the Onyx VM's address alone.
#
# The result is the one-way boundary: this box can reach out to search engines,
# nothing can reach in to it.
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=config.env disable=SC1091
source "${SCRIPT_DIR}/config.env"

DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
BOLD=$'\033[1m'; DIM=$'\033[2m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RESET=$'\033[0m'

step()  { echo; echo "${BOLD}==> $*${RESET}"; }
info()  { echo "    $*"; }
ok()    { echo "    ${GREEN}✓${RESET} $*"; }
skip()  { echo "    ${YELLOW}•${RESET} $* ${DIM}(already exists, skipping)${RESET}"; }
die()   { echo "    ERROR: $*" >&2; exit 1; }

# Run an az command, or just print it under --dry-run.
run() {
  if [[ "${DRY_RUN}" == true ]]; then
    echo "    ${DIM}\$ $*${RESET}"
  else
    "$@" >/dev/null
  fi
}

# True if the resource already exists. Under --dry-run we still query, because
# reads are free and it makes the dry run's output honest about what it'd skip.
exists() { az "$@" >/dev/null 2>&1; }

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
step "Preflight"

command -v az >/dev/null || die "az CLI not found"
az account show >/dev/null 2>&1 || die "not logged in -- run 'az login'"

CURRENT_SUB="$(az account show --query id -o tsv)"
if [[ "${CURRENT_SUB}" != "${SUBSCRIPTION_ID}" ]]; then
  info "switching subscription to ${SUBSCRIPTION_ID}"
  az account set --subscription "${SUBSCRIPTION_ID}"
fi
ok "subscription ${SUBSCRIPTION_ID}"

exists network vnet subnet show -g "${RESOURCE_GROUP}" --vnet-name "${VNET_NAME}" -n "${SUBNET_NAME}" \
  || die "subnet ${VNET_NAME}/${SUBNET_NAME} not found in ${RESOURCE_GROUP}"
ok "subnet ${VNET_NAME}/${SUBNET_NAME}"

# Confirm the allow-listed address really is the Onyx VM, so a typo cannot
# silently open the relay to some other machine on the VNet.
ACTUAL_ONYX_IP="$(az vm list-ip-addresses -g "${RESOURCE_GROUP}" -n "${ONYX_VM_NAME}" \
  --query "[0].virtualMachine.network.privateIpAddresses[0]" -o tsv 2>/dev/null || true)"
if [[ -n "${ACTUAL_ONYX_IP}" && "${ACTUAL_ONYX_IP}" != "${ONYX_VM_PRIVATE_IP}" ]]; then
  die "${ONYX_VM_NAME} is at ${ACTUAL_ONYX_IP}, but config.env allow-lists ${ONYX_VM_PRIVATE_IP}"
fi
ok "caller allow-list ${ONYX_VM_PRIVATE_IP} (${ONYX_VM_NAME})"

SUBNET_ID="$(az network vnet subnet show -g "${RESOURCE_GROUP}" \
  --vnet-name "${VNET_NAME}" -n "${SUBNET_NAME}" --query id -o tsv)"

# ---------------------------------------------------------------------------
# Network security group -- the one-way boundary
# ---------------------------------------------------------------------------
step "Network security group (${NSG_NAME})"

if exists network nsg show -g "${RESOURCE_GROUP}" -n "${NSG_NAME}"; then
  skip "NSG ${NSG_NAME}"
else
  run az network nsg create -g "${RESOURCE_GROUP}" -n "${NSG_NAME}" -l "${LOCATION}"
  ok "created NSG ${NSG_NAME}"
fi

# Rule helper: name priority direction access protocol source-address dest-port description
nsg_rule() {
  local name=$1 priority=$2 access=$3 protocol=$4 source=$5 dport=$6 desc=$7
  if exists network nsg rule show -g "${RESOURCE_GROUP}" --nsg-name "${NSG_NAME}" -n "${name}"; then
    skip "rule ${name}"
    return
  fi
  run az network nsg rule create \
    -g "${RESOURCE_GROUP}" --nsg-name "${NSG_NAME}" -n "${name}" \
    --priority "${priority}" --direction Inbound --access "${access}" \
    --protocol "${protocol}" --source-address-prefixes "${source}" \
    --source-port-ranges '*' --destination-address-prefixes '*' \
    --destination-port-ranges "${dport}" --description "${desc}"
  ok "rule ${name} (${priority}) ${access} ${source} -> :${dport}"
}

# Allows first (lower priority number wins), then the blanket denies.
nsg_rule "allow-onyx-searxng" 100 Allow Tcp "${ONYX_VM_PRIVATE_IP}/32" "${SEARXNG_PORT}" \
  "Onyx API/background workers querying SearXNG"
nsg_rule "allow-onyx-ssh" 110 Allow Tcp "${ONYX_VM_PRIVATE_IP}/32" 22 \
  "Break-glass SSH, reachable only by hopping through the Onyx VM"

# Azure's default rules allow ALL inbound from the VNet (65000) before the
# catch-all deny (65500). These two close that, so the allow rules above are
# genuinely the only way in.
nsg_rule "deny-internet-inbound" 4000 Deny '*' Internet '*' \
  "No inbound from the public internet -- this relay is outbound-only"
nsg_rule "deny-vnet-inbound" 4010 Deny '*' VirtualNetwork '*' \
  "No inbound from other VNet hosts except the allow rules above"

# ---------------------------------------------------------------------------
# Public IP -- outbound only
# ---------------------------------------------------------------------------
step "Public IP (${PUBLIC_IP_NAME})"

if exists network public-ip show -g "${RESOURCE_GROUP}" -n "${PUBLIC_IP_NAME}"; then
  skip "public IP ${PUBLIC_IP_NAME}"
else
  run az network public-ip create \
    -g "${RESOURCE_GROUP}" -n "${PUBLIC_IP_NAME}" -l "${LOCATION}" \
    --sku Standard --allocation-method Static --version IPv4 \
    --tags purpose=searxng-egress-only
  ok "created ${PUBLIC_IP_NAME} (egress only; all inbound denied by NSG)"
fi

# ---------------------------------------------------------------------------
# NIC -- static private IP so the Onyx-side config never has to change
# ---------------------------------------------------------------------------
step "Network interface (${NIC_NAME})"

if exists network nic show -g "${RESOURCE_GROUP}" -n "${NIC_NAME}"; then
  skip "NIC ${NIC_NAME}"
else
  run az network nic create \
    -g "${RESOURCE_GROUP}" -n "${NIC_NAME}" -l "${LOCATION}" \
    --subnet "${SUBNET_ID}" \
    --network-security-group "${NSG_NAME}" \
    --public-ip-address "${PUBLIC_IP_NAME}" \
    --private-ip-address "${SEARXNG_PRIVATE_IP}"
  ok "created ${NIC_NAME} with static private IP ${SEARXNG_PRIVATE_IP}"
fi

# ---------------------------------------------------------------------------
# cloud-init -- rendered from the files in this repo
# ---------------------------------------------------------------------------
step "Render cloud-init"

RENDERED_CLOUD_INIT="$(mktemp -t searxng-cloud-init)"
trap 'rm -f "${RENDERED_CLOUD_INIT}"' EXIT

python3 <<PY
import base64, pathlib, sys

root = pathlib.Path("${SCRIPT_DIR}")

def b64(rel: str) -> str:
    return base64.b64encode((root / rel).read_bytes()).decode()

rendered = (root / "cloud-init.yaml").read_text()
for placeholder, source in (
    ("__COMPOSE_B64__", "../docker-compose.yml"),
    ("__SETTINGS_B64__", "../config/settings.yml"),
    ("__BOOTSTRAP_B64__", "bootstrap.sh"),
):
    if placeholder not in rendered:
        sys.exit(f"cloud-init.yaml is missing the {placeholder} placeholder")
    rendered = rendered.replace(placeholder, b64(source))

pathlib.Path("${RENDERED_CLOUD_INIT}").write_text(rendered)
print(f"    embedded docker-compose.yml, settings.yml and bootstrap.sh "
      f"({len(rendered)} bytes of cloud-init)")
PY

# ---------------------------------------------------------------------------
# VM
# ---------------------------------------------------------------------------
step "Virtual machine (${SEARXNG_VM_NAME}, ${SEARXNG_VM_SIZE})"

if exists vm show -g "${RESOURCE_GROUP}" -n "${SEARXNG_VM_NAME}"; then
  skip "VM ${SEARXNG_VM_NAME}"
else
  run az vm create \
    -g "${RESOURCE_GROUP}" -n "${SEARXNG_VM_NAME}" -l "${LOCATION}" \
    --size "${SEARXNG_VM_SIZE}" \
    --image "${OS_IMAGE}" \
    --nics "${NIC_NAME}" \
    --admin-username "${ADMIN_USERNAME}" \
    --authentication-type ssh \
    --generate-ssh-keys \
    --os-disk-size-gb "${OS_DISK_SIZE_GB}" \
    --storage-sku "${OS_DISK_SKU}" \
    --custom-data "${RENDERED_CLOUD_INIT}" \
    --tags purpose=searxng owner=naarni managed-by=deployment/searxng
  ok "created ${SEARXNG_VM_NAME}"
fi

if [[ "${DRY_RUN}" == true ]]; then
  step "Dry run complete -- nothing was created"
  exit 0
fi

# ---------------------------------------------------------------------------
# Verify from the Onyx VM
# ---------------------------------------------------------------------------
step "Verify (from ${ONYX_VM_NAME}, over the VNet)"

info "waiting for cloud-init to finish on the SearXNG VM (up to ~5 min)..."
SEARXNG_URL="http://${SEARXNG_PRIVATE_IP}:${SEARXNG_PORT}"

for attempt in $(seq 1 30); do
  RESULT="$(az vm run-command invoke \
    -g "${RESOURCE_GROUP}" -n "${ONYX_VM_NAME}" \
    --command-id RunShellScript \
    --scripts "curl -sf -m 20 -X POST '${SEARXNG_URL}/search' -d 'q=onyx+ai&format=json' | head -c 300" \
    --query "value[0].message" -o tsv 2>/dev/null || true)"

  if grep -q '"results"' <<<"${RESULT}"; then
    ok "SearXNG answered over the VNet"
    break
  fi
  if [[ ${attempt} -eq 30 ]]; then
    echo
    die "SearXNG did not answer at ${SEARXNG_URL} after ~5 min.
    Check first-boot logs:
      az vm run-command invoke -g ${RESOURCE_GROUP} -n ${SEARXNG_VM_NAME} \\
        --command-id RunShellScript --scripts 'tail -40 /var/log/cloud-init-output.log'"
  fi
  sleep 10
done

# Confirm the boundary actually holds, rather than assuming the rules took.
step "Verify the one-way boundary"
PUBLIC_IP_ADDR="$(az network public-ip show -g "${RESOURCE_GROUP}" -n "${PUBLIC_IP_NAME}" --query ipAddress -o tsv)"
info "egress address: ${PUBLIC_IP_ADDR}"
if curl -sf -m 10 "http://${PUBLIC_IP_ADDR}:${SEARXNG_PORT}/" -o /dev/null 2>&1; then
  die "SearXNG is REACHABLE from the public internet at ${PUBLIC_IP_ADDR}:${SEARXNG_PORT} -- check the NSG"
fi
ok "not reachable from the public internet"

# ---------------------------------------------------------------------------
step "Done"
cat <<SUMMARY

    SearXNG endpoint (private, VNet only):  ${SEARXNG_URL}
    Egress IP (outbound only):              ${PUBLIC_IP_ADDR}

    Next: point Onyx at it.
      Admin panel -> Web Search -> SearXNG
      Base URL: ${SEARXNG_URL}

    Or from the Onyx VM:
      deployment/searxng/register-provider.sh

SUMMARY
