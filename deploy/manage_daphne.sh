#!/bin/bash
# Gestión de múltiples instancias Daphne (systemd template panaccess-wind@PORT).
#
# Uso:
#   DAPHNE_INSTANCES=8 sudo ./deploy/manage_daphne.sh start
#   DAPHNE_INSTANCES=8 sudo ./deploy/manage_daphne.sh status
#
# Puertos: 8000 .. 8000+N-1

DAPHNE_INSTANCES="${DAPHNE_INSTANCES:-8}"
BASE_PORT=8000

ports() {
    seq "$BASE_PORT" $((BASE_PORT + DAPHNE_INSTANCES - 1))
}

case "${1:-}" in
    start|enable)
        for port in $(ports); do
            systemctl "${1}" "panaccess-wind@${port}.service"
        done
        ;;
    stop|disable)
        for port in $(ports | tac); do
            systemctl "${1}" "panaccess-wind@${port}.service"
        done
        ;;
    restart)
        for port in $(ports); do
            systemctl restart "panaccess-wind@${port}.service"
        done
        ;;
    status)
        for port in $(ports); do
            echo "--- panaccess-wind@${port} ---"
            systemctl status "panaccess-wind@${port}.service" --no-pager | head -5
        done
        ;;
    *)
        echo "Uso: DAPHNE_INSTANCES=8 $0 {start|stop|restart|status|enable|disable}"
        exit 1
        ;;
esac
