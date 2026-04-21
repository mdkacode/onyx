# NaArNi Gyan — VM Auto-Resize Operations Guide

This doc covers the **auto-resize schedule** for the production Azure VM. Scope
is ONLY the resize flow. For general ops (deploys, containers, debugging),
see `NAARNI-OPS-GUIDE.md`.

---

## Quick Reference

| Item | Value |
|---|---|
| **Azure Subscription** | `d091bc43-1a97-4a25-b271-a9eb6616bb82` |
| **Resource Group** | `naarni-cad-vm_group` |
| **VM Name** | `naarni-ai-vm` |
| **Public IP** | `52.140.124.116` (must stay Static) |
| **SSH** | `ssh naarni@52.140.124.116` |
| **Working-hours size** | `Standard_E8s_v5` — 8 vCPU, 64 GB RAM |
| **Off-hours size** | `Standard_E4s_v5` — 4 vCPU, 32 GB RAM |
| **Schedule (IST)** | Mon–Fri: 08:00 scale-UP, 22:00 scale-DOWN. Weekends: stay DOWN |
| **Expected downtime per transition** | 3–6 min |

---

## Which accounts do what

| Purpose | Identity | Stored where |
|---|---|---|
| Human Azure admin (manual ops, pre-flight) | `mayank.dwivedi@naarni.com` (or `admin@naarni.com`) | Your laptop via `az login` |
| GitHub Actions → Azure | Service principal referenced by `AZURE_CREDENTIALS` | GitHub repo secret |
| GitHub Actions → VM (SSH) | `naarni` user, key in `VM_SSH_KEY` | GitHub repo secret |
| SSH on the VM | `naarni` (sudoer) | Your laptop `~/.ssh` |

**If the service principal's secret expires**, all scheduled resizes fail silently until rotated. Check expiry with:

```bash
az ad sp credential list --id <sp-app-id> --query "[].endDate"
```

---

## Schedule (cron)

GitHub Actions cron is UTC. IST = UTC+5:30, no DST.

| Action | IST | UTC cron |
|---|---|---|
| Scale UP → E8s_v5 | Mon–Fri 08:00 | `30 2 * * 1-5` |
| Scale DOWN → E4s_v5 | Mon–Fri 22:00 | `30 16 * * 1-5` |

On Friday night the VM drops to E4s_v5 and **stays** through the weekend. First Monday scale-up at 08:00 IST brings it back to E8s_v5.

---

## Log locations — "where is everything captured"

| What | Where | Retention |
|---|---|---|
| Full workflow run (every SSH + `az` command) | GitHub Actions run logs | 90 days |
| Each transition summary line | `/opt/naarni/vm-resize.log` on VM | Forever (rotates at 10 MB) |
| Failure alerts | GitHub Issue auto-opened, tagged `@mdkacode` | Until closed |
| Azure-side record of resize | Azure Activity Log (resource: `naarni-ai-vm`) | 90 days |
| App container startup | `docker compose logs` on VM | 7 days (default) |

### Reading the VM-side log

```bash
ssh naarni@52.140.124.116
tail -n 50 /opt/naarni/vm-resize.log
```

Sample line format:
```
2026-04-21T02:30:14Z up   E4s_v5 -> E8s_v5  duration=4m12s  result=ok  health=200
```

### Azure Activity Log

```bash
az monitor activity-log list \
  --resource-group naarni-cad-vm_group \
  --offset 7d \
  --query "[?contains(operationName.value, 'virtualMachines')].{time:eventTimestamp, op:operationName.localizedValue, status:status.localizedValue, caller:caller}" \
  -o table
```

---

## Useful commands — quick copy/paste

### Check current VM size (instant, no SSH)
```bash
az vm show -g naarni-cad-vm_group -n naarni-ai-vm \
  --query hardwareProfile.vmSize -o tsv
```

### Check VM power state
```bash
az vm get-instance-view -g naarni-cad-vm_group -n naarni-ai-vm \
  --query "instanceView.statuses[?starts_with(code,'PowerState')].displayStatus" -o tsv
```

### Verify public IP is Static
```bash
az vm list-ip-addresses -g naarni-cad-vm_group -n naarni-ai-vm \
  --query "[0].virtualMachine.network.publicIpAddresses[0].{ip:ipAddress, alloc:ipAllocationMethod}" -o table
```

### List sizes this VM can resize to right now
```bash
az vm list-vm-resize-options -g naarni-cad-vm_group -n naarni-ai-vm \
  --query "[].name" -o tsv | sort
```

### Manual resize (EMERGENCY ONLY — bypasses graceful stop)
```bash
az vm deallocate -g naarni-cad-vm_group -n naarni-ai-vm
az vm resize -g naarni-cad-vm_group -n naarni-ai-vm --size Standard_E4s_v5
az vm start -g naarni-cad-vm_group -n naarni-ai-vm
```

### Take an OS-disk snapshot (safety net before changes)
```bash
DISK_ID=$(az vm show -g naarni-cad-vm_group -n naarni-ai-vm \
  --query storageProfile.osDisk.managedDisk.id -o tsv)

az snapshot create \
  -g naarni-cad-vm_group \
  -n naarni-ai-vm-osdisk-snapshot-$(date +%Y%m%d) \
  --source "$DISK_ID"
```

### List snapshots
```bash
az snapshot list -g naarni-cad-vm_group \
  --query "[].{name:name, created:timeCreated, size:diskSizeGB}" -o table
```

### Trigger a manual resize via GitHub Actions (once the workflows exist)
GitHub → Actions → `Scale Up` / `Scale Down` → Run workflow

### Health check from your laptop
```bash
curl -sk -o /dev/null -w "%{http_code}\n" https://ai.naarni.com/api/health
```

---

## Pre-flight check (run once before enabling the schedule)

```bash
# From repo root on your laptop, after 'az login':
./scripts/preflight-resize-check.sh
```

This is **read-only**. It verifies:

1. Azure CLI session is on the right subscription
2. Public IP is Static (required — otherwise deallocate changes the IP and DNS breaks)
3. Current VM size = `Standard_E8s_v5`
4. `Standard_E4s_v5` is offered as a resize option
5. Docker root dir is on OS disk, not `/mnt` tempdisk
6. `/mnt` has no user data that would be lost on deallocate
7. Docker Compose stack is healthy right now

All six must pass before the OS-disk snapshot is taken and the workflows are merged.

---

## Rollback — if a scheduled resize goes wrong

1. **If the workflow failed mid-flight:**
   - The workflow auto-attempts `az vm start` at the original size.
   - Check the GitHub Issue it auto-opened for details.
2. **If the VM is stuck deallocated:**
   ```bash
   az vm start -g naarni-cad-vm_group -n naarni-ai-vm
   ```
3. **If containers won't come up after start:**
   ```bash
   ssh naarni@52.140.124.116
   cd /opt/naarni/deployment/docker_compose
   docker compose -f docker-compose.prod.yml -f docker-compose.naarni.yml up -d
   docker compose -f docker-compose.prod.yml -f docker-compose.naarni.yml logs --tail=100
   ```
4. **If disk corruption suspected (worst case):**
   Restore from the OS-disk snapshot created during pre-flight. Attach the
   restored disk to a new VM and swap DNS. Detailed runbook lives in the
   snapshot recovery section of `NAARNI-OPS-GUIDE.md` (to be added).

---

## Golden rules — do NOT break these

1. **Never** `docker compose down -v` on the VM. Volumes are data — Postgres, Redis, Vespa, file store.
2. **Never** delete and recreate the VM. Resize only.
3. **Never** change the public IP allocation from Static to Dynamic.
4. **Never** put the target off-hours size below 32 GB RAM — Vespa and the embedding model server need it.
5. **Never** run `az vm deallocate` without first `docker compose stop` on app containers. Infra containers (DB, Redis, Vespa) are stopped by the OS during deallocate, which is fine, but app containers holding open connections can leave DB in a less-clean state.
6. **Always** take a fresh OS-disk snapshot before any manual resize or size-family change.
