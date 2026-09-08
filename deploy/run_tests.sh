#!/bin/bash
# Corre toda la suite de tests de Django (app wind, y telemetry/applogs si en el
# futuro agregan tests) y muestra el resultado de cada test individual.
#
#   cd /opt/panaccess-wind
#   sudo chmod +x deploy/run_tests.sh
#   ./deploy/run_tests.sh                 # corre todo
#   ./deploy/run_tests.sh wind.tests.test_auth   # corre solo un módulo/clase/test puntual
#
# Requisitos en el servidor: PostgreSQL y Redis corriendo (systemctl status
# postgresql redis-server) y el usuario de DB configurado en .env con permiso
# CREATEDB (Django crea/destruye una base de test aparte, no toca la real).
#
# Variables opcionales:
#   KEEPDB=1   -> no borra la base de test al final (más rápido en corridas repetidas)
#   PARALLEL=N -> corre N procesos de test en paralelo

set -euo pipefail

cd "$(dirname "$0")/.."
source env/bin/activate

APPS=(wind telemetry applogs)
ARGS=("$@")
if [ ${#ARGS[@]} -eq 0 ]; then
    ARGS=("${APPS[@]}")
fi

EXTRA=()
if [ "${KEEPDB:-0}" = "1" ]; then
    EXTRA+=(--keepdb)
fi
if [ -n "${PARALLEL:-}" ]; then
    EXTRA+=(--parallel "$PARALLEL")
fi

echo "=== manage.py check ==="
python manage.py check

echo "=== Corriendo tests: ${ARGS[*]} ==="
# --verbosity=2 imprime una línea por test (ok / FAIL / ERROR), que es lo que
# permite ver el resultado de cada uno, no solo el resumen final.
python manage.py test "${ARGS[@]}" --verbosity=2 "${EXTRA[@]}"
