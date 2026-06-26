#!/bin/bash
# Reset duro: detiene todo el stack, espera y arranca de cero.
# Usar solo para troubleshooting o cambios de infra (PostgreSQL/Redis).
# Para deploy rutinario usar deploy/refresh_stack.sh
#
#   DAPHNE_INSTANCES=8 ./deploy/reset_stack.sh
#   SKIP_DJANGO=1 DAPHNE_INSTANCES=8 ./deploy/reset_stack.sh   # solo reiniciar servicios
#
# Ver docs/DEPLOYMENT_UBUNTU_NATIVE.md — Paso 11 (Reset duro).

set -euo pipefail

cd /opt/panaccess-wind
source env/bin/activate
DAPHNE_INSTANCES="${DAPHNE_INSTANCES:-8}"
SKIP_DJANGO="${SKIP_DJANGO:-0}"

if [[ "$SKIP_DJANGO" != "1" ]]; then
    echo "=== Git + dependencias ==="
    git pull
    pip install -q -r requirements.txt

    echo "=== Django ==="
    python manage.py migrate --noinput
    python manage.py collectstatic --noinput
    python manage.py check
else
    echo "=== SKIP_DJANGO=1: omitiendo git pull y migraciones ==="
fi

echo "=== Detención completa ==="
DAPHNE_INSTANCES="$DAPHNE_INSTANCES" sudo deploy/manage_daphne.sh stop
sudo deploy/manage_services.sh stop
sudo systemctl stop nginx
sudo systemctl stop redis-server postgresql
sleep 3

echo "=== Arranque en orden ==="
sudo systemctl start postgresql redis-server
DAPHNE_INSTANCES="$DAPHNE_INSTANCES" sudo deploy/manage_daphne.sh start
sudo deploy/manage_services.sh start
sudo nginx -t && sudo systemctl start nginx

echo "=== Verificación ==="
sudo systemctl is-active postgresql redis-server nginx
redis-cli ping
sudo -u postgres psql -d wind_db -c "SELECT 1;" >/dev/null
DAPHNE_INSTANCES="$DAPHNE_INSTANCES" sudo deploy/manage_daphne.sh status
sudo deploy/manage_services.sh status
curl -sk https://backend.wind.do/ready/ | python3 -m json.tool
curl -sk https://backend.wind.do/health/ | python3 -m json.tool
echo "=== Reset duro listo ==="
