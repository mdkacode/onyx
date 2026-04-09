# NaArNi Gyan — Azure Cost Optimization

**Date:** 2026-03-31
**Server:** naarni-ai-vm (52.140.124.116)
**Region:** Central India
**Site:** https://ai.naarni.com

---

## Summary

| Metric | Before | After |
|---|---|---|
| **VM Size** | Standard_E8s_v5 (8 vCPU, 64 GB) | E4s_v5 (day) / E2s_v5 (night) |
| **Monthly Cost** | ~$380/mo (~₹32,000) | ~$152/mo (~₹12,800) |
| **Disk Usage** | 82 GB (34%) | 47 GB (19%) |
| **Monthly Savings** | — | **$228 (~₹19,200)** |
| **Annual Savings** | — | **$2,736 (~₹2,30,000)** |

---

## What Was Done

### 1. Docker Image Cleanup
- Removed 35 GB of dangling `<none>` images from old deployments
- Zero impact on running containers or data volumes
- Disk usage dropped from 82 GB → 47 GB

### 2. MinIO Memory Fix
- **Problem:** MinIO was at 86% of its 256 MB limit — near OOM crash
- **Fix:** Increased limit from 256 MB → 512 MB in `docker-compose.naarni.yml`
- **Result:** Now at ~50% usage, healthy

### 3. Web Server Healthcheck Fix
- **Problem:** Healthcheck failing 2,369 consecutive times (marked "unhealthy")
- **Root Cause:** Next.js 16 binds to the container IP, not `localhost`. The `wget http://localhost:3000/` healthcheck was hitting the wrong address.
- **Fix:** Added `HOSTNAME=0.0.0.0` env var and changed healthcheck to `wget http://0.0.0.0:3000/`
- **Result:** Healthy in 20 seconds

### 4. Container Memory Limits Tightened
Sized to fit within the 8 GB night VM (E2s_v5) while keeping headroom above actual usage:

| Container | Old Limit | Actual Usage | New Limit |
|---|---|---|---|
| Vespa | 8 GB | 3.2 GB | 4 GB |
| Background | 4 GB | 2.5 GB | 3 GB |
| API Server | 4 GB | 840 MB | 1.5 GB |
| PostgreSQL | 2 GB | 189 MB | 512 MB |
| MinIO | 256 MB | 253 MB | 512 MB |
| Web Server | 1 GB | 85 MB | 256 MB |
| Redis | 512 MB | 22 MB | 128 MB |
| Nginx | 256 MB | 15 MB | 128 MB |
| **Total** | **20.3 GB** | **7.1 GB** | **~10 GB** |

### 5. VM Resized (E8s_v5 → E4s_v5)
- Downsized from memory-optimized E8s_v5 (8 vCPU, 64 GB) to E4s_v5 (4 vCPU, 32 GB)
- Same E-series family — instant resize, no architecture changes
- All 10 containers running, site healthy at HTTP 200

### 6. Automated Day/Night Resize Schedule
Cron jobs on the server auto-resize the VM daily:

| IST Time | UTC Time | Action | VM Size | Cost/hr |
|---|---|---|---|---|
| 9:55 AM | 4:25 AM | Scale UP | E4s_v5 (4 vCPU, 32 GB) | $0.26/hr |
| 7:05 PM | 1:35 PM | Scale DOWN | E2s_v5 (2 vCPU, 16 GB) | $0.13/hr |

Downtime: ~3 minutes per resize (before/after work hours).

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  Azure VM: naarni-ai-vm  │  Central India  │  E4s_v5 (day)  │
│                                                              │
│  ┌─────────────┐  ┌─────────────┐  ┌──────────────┐         │
│  │ API Server  │  │ Web Server  │  │  Background  │         │
│  │   1.5 GB    │  │   256 MB    │  │    3 GB      │         │
│  └─────────────┘  └─────────────┘  └──────────────┘         │
│  ┌─────────────┐  ┌─────────────┐  ┌──────────────┐         │
│  │   Vespa     │  │ PostgreSQL  │  │    Redis     │         │
│  │   4 GB      │  │   512 MB    │  │   128 MB     │         │
│  └─────────────┘  └─────────────┘  └──────────────┘         │
│  ┌─────────────┐  ┌─────────────┐  ┌──────────────┐         │
│  │   Nginx     │  │   MinIO     │  │  Certbot     │         │
│  │  128 MB     │  │  512 MB     │  │   (shared)   │         │
│  └─────────────┘  └─────────────┘  └──────────────┘         │
│                                                              │
│  OS Disk: 256 GB StandardSSD  │  Public IP: 52.140.124.116  │
└──────────────────────────────────────────────────────────────┘
```

---

## Files & Locations

### On Server (52.140.124.116)

| File | Purpose |
|---|---|
| `/opt/naarni/vm-resize.sh` | Resize script (up/down/status) |
| `/var/log/vm-resize.log` | Resize operation logs |
| `/opt/naarni/deployment/docker_compose/` | Docker Compose configs |
| `crontab -l` (naarni user) | Scheduled resize cron jobs |

### In Repository

| File | Purpose |
|---|---|
| `deployment/docker_compose/docker-compose.naarni.yml` | Container config with tightened limits |
| `scripts/vm-resize.sh` | Resize script (source copy) |
| `.github/workflows/vm-resize-schedule.yml` | GitHub Actions workflow (needs AZURE_CREDENTIALS secret) |
| `.github/workflows/deploy-naarni-azure.yml` | CI/CD deployment pipeline |

---

## Useful Commands

```bash
# SSH to server
ssh naarni@52.140.124.116

# Check VM size and state
/opt/naarni/vm-resize.sh status

# Manual scale up (business hours size)
/opt/naarni/vm-resize.sh up

# Manual scale down (off-hours size)
/opt/naarni/vm-resize.sh down

# Check resize logs
cat /var/log/vm-resize.log

# Check container health
docker ps --format 'table {{.Names}}\t{{.Status}}'

# Check memory usage
docker stats --no-stream --format 'table {{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}'

# Check disk usage
df -h / && docker system df

# Clean old Docker images (safe — doesn't touch data)
docker image prune -f
```

---

## Cost Breakdown

### Monthly Cost Calculation

```
Day hours:   9 hrs × 30 days × $0.260/hr  = $70.20  (E4s_v5)
Night hours: 15 hrs × 30 days × $0.130/hr = $58.50  (E2s_v5)
OS Disk:     256 GB StandardSSD            = $19.00
Public IP:   Static                        =  $4.00
────────────────────────────────────────────────────
Total:                                      ~$152/mo (~₹12,800/mo)
```

### External Costs (not Azure)
- **AWS S3** — document storage (~$5-20/mo depending on volume)
- **Docker Hub** — image registry (free tier)

---

## Future Scaling Path

| Stage | Trigger | Action | Cost |
|---|---|---|---|
| **Current** | < 10 users | E4s_v5 day / E2s_v5 night | ~$152/mo |
| **Growing** | 10-50 users, CPU > 70% | Remove schedule, run E4s_v5 24/7 | ~$190/mo |
| **Scaling** | 50+ users, need more CPU | Resize to E8s_v5 | ~$380/mo |
| **Enterprise** | 500+ users, need HA | Migrate to AKS with auto-scaling | ~$500+/mo |

---

## Known Limitations

1. **Azure CLI token expiry** — The `az` login on the server will expire (typically 90 days). When it does, SSH in and run `az login --use-device-code` again.
2. **Contributor role** — The `admin@naarni.com` account has Contributor (not Owner) role. This prevents creating service principals or assigning roles for fully automated Azure Automation. Ask your Azure admin for Owner on the `naarni-cad-vm_group` resource group to unlock this.
3. **Resize downtime** — Each resize causes ~3 min downtime. Scheduled before/after work hours to minimize impact.
