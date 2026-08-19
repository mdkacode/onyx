#!/usr/bin/env bash
# ============================================================================
# Runs once, at first boot, from cloud-init. Idempotent so it is safe to re-run
# by hand (`sudo /opt/searxng/bootstrap.sh`) after editing config.
# ============================================================================
set -euo pipefail

APP_DIR=/opt/searxng
ENV_FILE="${APP_DIR}/.env"
SETTINGS="${APP_DIR}/config/settings.yml"

log() { echo "[searxng-bootstrap] $*"; }

# --- Bind address -----------------------------------------------------------
# Take the VNet-private address from IMDS and bind the published port to it, so
# the service is not listening on the public interface at all. The public IP on
# this VM exists only to give it outbound internet access.
PRIVATE_IP="$(curl -sf --retry 5 --retry-delay 2 -H 'Metadata:true' \
  'http://169.254.169.254/metadata/instance/network/interface/0/ipv4/ipAddress/0/privateIpAddress?api-version=2021-02-01&format=text')"

if [[ -z "${PRIVATE_IP}" ]]; then
  log "FATAL: could not read private IP from IMDS"
  exit 1
fi
log "private IP: ${PRIVATE_IP}"

# --- Secret -----------------------------------------------------------------
# Generated on the box and never leaves it; regenerating only invalidates
# SearXNG's own cookie signing, which nothing here depends on.
if [[ -f "${ENV_FILE}" ]] && grep -q '^SEARXNG_SECRET=' "${ENV_FILE}"; then
  log "reusing existing secret"
  SECRET="$(grep '^SEARXNG_SECRET=' "${ENV_FILE}" | cut -d= -f2-)"
else
  SECRET="$(openssl rand -hex 32)"
  log "generated new secret"
fi

umask 077
cat > "${ENV_FILE}" <<ENV
SEARXNG_BIND_IP=${PRIVATE_IP}
SEARXNG_SECRET=${SECRET}
ENV
umask 022

# The image reads SEARXNG_SECRET, but substituting the placeholder too means the
# settings file is correct even if the container is started without the env file.
sed -i "s|ultrasecretkey|${SECRET}|g" "${SETTINGS}"

# SearXNG runs as uid 977 inside the image and needs to read its settings.
chown -R 977:977 "${APP_DIR}/config" 2>/dev/null || true

# --- Start ------------------------------------------------------------------
systemctl enable --now docker
cd "${APP_DIR}"
docker compose up -d

systemctl daemon-reload
systemctl enable --now searxng-update.timer

# --- Verify -----------------------------------------------------------------
# Fail loudly in the cloud-init log if the JSON API is not actually answering;
# a container that is "up" but returning 403 is the usual SearXNG failure mode.
for _ in $(seq 1 30); do
  if curl -sf -X POST "http://${PRIVATE_IP}:8080/search" \
       -d 'q=onyx&format=json' -o /dev/null; then
    log "SearXNG JSON API is answering on ${PRIVATE_IP}:8080"
    exit 0
  fi
  sleep 5
done

log "FATAL: SearXNG did not answer on ${PRIVATE_IP}:8080 within 150s"
docker compose logs --tail=50 || true
exit 1
