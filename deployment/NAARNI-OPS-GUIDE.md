# Naarni Operations & Debugging Guide

## Quick Reference

| Item | Value |
|------|-------|
| **Production URL** | https://ai.naarni.com |
| **VM IP** | 52.140.124.116 |
| **SSH** | `ssh naarni@52.140.124.116` |
| **VM Size** | E8s_v5 (8 vCPU, 64 GB RAM, 256 GB SSD) |
| **Docker Hub** | naarnimd/naarni-ai-* |
| **Deploy** | Push to `main` branch triggers CI/CD |

---

## SSH Into the Server

```bash
ssh naarni@52.140.124.116
cd /opt/naarni/deployment/docker_compose
```

---

## View Container Status

```bash
# All containers
docker compose -f docker-compose.prod.yml -f docker-compose.naarni.yml ps

# Just names and status
docker ps --format "table {{.Names}}\t{{.Status}}" | sort
```

---

## View Logs

### API Server (most common to check)
```bash
# Last 50 lines
docker logs onyx-api_server-1 --tail 50

# Follow live
docker logs onyx-api_server-1 -f

# Search for errors
docker logs onyx-api_server-1 2>&1 | grep -i error | tail -20
```

### Background Workers (Celery)
```bash
docker logs onyx-background-1 --tail 50
docker logs onyx-background-1 -f
```

### Web Server (Next.js Frontend)
```bash
docker logs onyx-web_server-1 --tail 50
```

### Model Server (Embeddings)
```bash
docker logs onyx-inference_model_server-1 --tail 50
docker logs onyx-indexing_model_server-1 --tail 50
```

### Nginx (Reverse Proxy + SSL)
```bash
docker logs onyx-nginx-1 --tail 50
# Check for upstream errors
docker logs onyx-nginx-1 2>&1 | grep -i error | tail -10
```

### Database (PostgreSQL)
```bash
docker logs onyx-relational_db-1 --tail 50
```

### All Logs at Once
```bash
docker compose -f docker-compose.prod.yml -f docker-compose.naarni.yml logs --tail 20
```

---

## Health Checks

```bash
# API health (via nginx)
curl -s http://localhost:80/api/health

# API health (direct, bypass nginx)
curl -s http://localhost:8080/api/health

# Frontend
curl -s -o /dev/null -w "HTTP %{http_code}\n" http://localhost:80/

# SSL cert check (from outside)
echo | openssl s_client -connect ai.naarni.com:443 -servername ai.naarni.com 2>/dev/null | openssl x509 -noout -dates
```

---

## Restart Services

```bash
cd /opt/naarni/deployment/docker_compose

# Restart a single service
docker compose -f docker-compose.prod.yml -f docker-compose.naarni.yml restart api_server

# Restart all
docker compose -f docker-compose.prod.yml -f docker-compose.naarni.yml restart

# Full stop and start (if things are really broken)
docker compose -f docker-compose.prod.yml -f docker-compose.naarni.yml down
docker compose -f docker-compose.prod.yml -f docker-compose.naarni.yml up -d

# Restart just nginx (after SSL issues)
docker compose -f docker-compose.prod.yml -f docker-compose.naarni.yml restart nginx
```

---

## Pull Latest Images (Manual Deploy)

```bash
cd /opt/naarni/deployment/docker_compose
docker compose -f docker-compose.prod.yml -f docker-compose.naarni.yml pull
docker compose -f docker-compose.prod.yml -f docker-compose.naarni.yml up -d --remove-orphans
```

---

## Database Operations

```bash
# Connect to PostgreSQL
docker exec -it onyx-relational_db-1 psql -U postgres

# Run a quick SQL query
docker exec -it onyx-relational_db-1 psql -U postgres -c "SELECT count(*) FROM public.\"user\";"

# Check database size
docker exec -it onyx-relational_db-1 psql -U postgres -c "SELECT pg_size_pretty(pg_database_size('postgres'));"
```

---

## Memory & Disk Usage

```bash
# RAM usage
free -h

# Per-container memory
docker stats --no-stream --format "table {{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}" | sort

# Disk usage
df -h /
docker system df
```

---

## SSL Certificate

### Check Expiry
```bash
echo | openssl s_client -connect ai.naarni.com:443 -servername ai.naarni.com 2>/dev/null | openssl x509 -noout -dates
```

### Force Renewal
```bash
cd /opt/naarni/deployment/docker_compose
docker compose -f docker-compose.prod.yml -f docker-compose.naarni.yml run --rm --entrypoint "\
  certbot renew --force-renewal" certbot
docker compose -f docker-compose.prod.yml -f docker-compose.naarni.yml exec nginx nginx -s reload
```

Auto-renewal runs every 12 hours via the certbot container.

---

## Common Issues

### "Backend is currently unavailable"
1. Check API server: `docker logs onyx-api_server-1 --tail 20`
2. Restart API: `docker compose -f docker-compose.prod.yml -f docker-compose.naarni.yml restart api_server`
3. Restart nginx: `docker compose -f docker-compose.prod.yml -f docker-compose.naarni.yml restart nginx`

### 502 Bad Gateway
1. API server might be restarting after a deploy
2. Wait 30-60 seconds, then refresh
3. If persistent: `docker logs onyx-nginx-1 2>&1 | grep error | tail -5`

### OIDC Login Fails
1. Check redirect URI in Azure Entra: must be `https://ai.naarni.com/auth/oidc/callback`
2. Check `VALID_EMAIL_DOMAINS` in `.env`: `grep VALID_EMAIL .env`
3. Check API logs: `docker logs onyx-api_server-1 2>&1 | grep -i "oidc\|oauth\|auth" | tail -10`

### Container Keeps Restarting
```bash
# Check which container is restarting
docker ps --format "{{.Names}}: {{.Status}}" | grep -i restart

# Check its logs
docker logs <container-name> --tail 50
```

### Out of Disk Space
```bash
# Clean unused Docker images/volumes
docker system prune -a --volumes
```

### OpenSearch Eating Too Much RAM
OpenSearch is running but disabled via env var. To fully stop it:
```bash
docker compose -f docker-compose.prod.yml -f docker-compose.naarni.yml stop opensearch
```

---

## Environment Variables

Config file (no secrets): `/opt/naarni/deployment/docker_compose/.env`

Secrets are injected by GitHub Actions during deploy. To update a secret:
1. Update in GitHub repo → Settings → Secrets → Actions
2. Push any commit to trigger a new deploy (or manually re-run the workflow)

---

## VM Management (Azure)

```bash
# Stop VM (stops billing for compute)
az vm deallocate --resource-group naarni-cad-vm_group --name naarni-ai-vm

# Start VM
az vm start --resource-group naarni-cad-vm_group --name naarni-ai-vm

# Check VM status
az vm get-instance-view --resource-group naarni-cad-vm_group --name naarni-ai-vm --query "instanceView.statuses[1].displayStatus" -o tsv
```
