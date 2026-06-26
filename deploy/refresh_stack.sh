#!/bin/bash
# Deploy normal: git pull + migrate + restart app (Daphne/Celery) + verificación.
# No detiene PostgreSQL ni Redis. Uso diario en producción.
#
#   cd /opt/panaccess-wind
#   sudo chmod +x deploy/refresh_stack.sh
#   DAPHNE_INSTANCES=8 ./deploy/refresh_stack.sh
#
# Ver docs/DEPLOYMENT_UBUNTU_NATIVE.md — Paso 11 (Deploy normal).

set -euo pipefail

cd /opt/panaccess-wind
source env/bin/activate
DAPHNE_INSTANCES="${DAPHNE_INSTANCES:-8}"

echo "=== Git + dependencias ==="
git pull
pip install -q -r requirements.txt

echo "=== Django ==="
python manage.py migrate --noinput
python manage.py collectstatic --noinput
python manage.py check

echo "=== Reinicio aplicación ==="
DAPHNE_INSTANCES="$DAPHNE_INSTANCES" sudo deploy/manage_daphne.sh restart
sudo deploy/manage_services.sh restart
sudo nginx -t && sudo systemctl reload nginx

echo "=== Verificación ==="
sudo systemctl is-active postgresql redis-server nginx
redis-cli ping
DAPHNE_INSTANCES="$DAPHNE_INSTANCES" sudo deploy/manage_daphne.sh status
sudo deploy/manage_services.sh status
curl -sk https://backend.wind.do/ready/ | python3 -m json.tool
curl -sk https://backend.wind.do/health/ | python3 -m json.tool
echo "=== Deploy normal listo ==="
