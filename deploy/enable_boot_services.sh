#!/bin/bash
# Habilita arranque automático de todos los servicios tras reboot.
# Uso: sudo ./deploy/enable_boot_services.sh

set -e

DAPHNE_INSTANCES="${DAPHNE_INSTANCES:-8}"
BASE=/opt/panaccess-wind

echo "=== Copiando units systemd (si existen en deploy/) ==="
cp "$BASE/deploy/systemd/panaccess-wind@.service" /etc/systemd/system/ 2>/dev/null || true
cp "$BASE/deploy/systemd/panaccess-wind.target" /etc/systemd/system/ 2>/dev/null || true
cp "$BASE/deploy/systemd/panaccess-celery-"*.service /etc/systemd/system/ 2>/dev/null || true

systemctl daemon-reload

echo "=== Infraestructura ==="
systemctl enable postgresql redis-server nginx

echo "=== Daphne ($DAPHNE_INSTANCES instancias) ==="
DAPHNE_INSTANCES=$DAPHNE_INSTANCES "$BASE/deploy/manage_daphne.sh" disable 2>/dev/null || true
DAPHNE_INSTANCES=$DAPHNE_INSTANCES "$BASE/deploy/manage_daphne.sh" enable
systemctl enable panaccess-wind.target

echo "=== Celery ==="
systemctl enable panaccess-celery-worker-pipeline.service
systemctl enable panaccess-celery-worker-full.service
systemctl enable panaccess-celery-beat.service

echo ""
echo "=== Verificación (debe decir 'enabled' en todos) ==="
systemctl is-enabled postgresql redis-server nginx panaccess-wind.target || true
for p in $(seq 8000 $((8000 + DAPHNE_INSTANCES - 1))); do
    systemctl is-enabled "panaccess-wind@${p}.service" || true
done
systemctl is-enabled panaccess-celery-worker-pipeline panaccess-celery-worker-full panaccess-celery-beat

echo ""
echo "Listo. Tras reboot: sudo systemctl start postgresql redis-server && DAPHNE_INSTANCES=$DAPHNE_INSTANCES $BASE/deploy/manage_daphne.sh start && systemctl start panaccess-celery-worker-pipeline panaccess-celery-worker-full panaccess-celery-beat nginx"
