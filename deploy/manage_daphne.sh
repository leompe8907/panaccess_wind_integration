#!/bin/bash
# Gestión de múltiples instancias Daphne (systemd template panaccess-wind@PORT).
#
# Uso (siempre ruta absoluta con sudo):
#   DAPHNE_INSTANCES=8 sudo /opt/panaccess-wind/deploy/manage_daphne.sh start
#
# Puertos: 8000 .. 8000+N-1

DAPHNE_INSTANCES="${DAPHNE_INSTANCES:-8}"
BASE_PORT=8000

ports() {
    seq "$BASE_PORT" $((BASE_PORT + DAPHNE_INSTANCES - 1))
}

case "${1:-}" in
    enable)
        for port in $(ports); do
            systemctl enable "panaccess-wind@${port}.service"
        done
        systemctl enable panaccess-wind.target 2>/dev/null || true
        ;;
    start)
        for port in $(ports); do
            systemctl start "panaccess-wind@${port}.service"
        done
        ;;
    stop)
        for port in $(ports | tac); do
            systemctl stop "panaccess-wind@${port}.service"
        done
        ;;
    disable)
        for port in $(ports | tac); do
            systemctl disable "panaccess-wind@${port}.service"
        done
        systemctl disable panaccess-wind.target 2>/dev/null || true
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
