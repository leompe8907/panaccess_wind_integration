#!/bin/bash
# Reinicio / estado de servicios Celery (Daphne escalado: deploy/manage_daphne.sh).
# Uso: sudo ./deploy/manage_services.sh {start|stop|restart|status}

SERVICES=(
    panaccess-celery-worker-pipeline.service
    panaccess-celery-worker-full.service
    panaccess-celery-worker-compare.service
    panaccess-celery-beat.service
)

case "${1:-}" in
    start)
        for svc in "${SERVICES[@]}"; do
            systemctl start "$svc"
        done
        ;;
    stop)
        for svc in "${SERVICES[@]}"; do
            systemctl stop "$svc"
        done
        ;;
    restart)
        for svc in "${SERVICES[@]}"; do
            systemctl restart "$svc"
        done
        ;;
    status)
        for svc in "${SERVICES[@]}"; do
            echo "--- $svc ---"
            systemctl status "$svc" --no-pager | head -5
        done
        ;;
    *)
        echo "Uso: $0 {start|stop|restart|status}"
        exit 1
        ;;
esac
