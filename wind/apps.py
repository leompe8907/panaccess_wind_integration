import logging
import os
import sys

from django.apps import AppConfig

logger = logging.getLogger(__name__)

# Comandos de management donde no hace falta login PanAccess al arranque
_SKIP_PANACCESS_INIT_COMMANDS = frozenset({
    'migrate', 'makemigrations', 'shell', 'test', 'collectstatic',
    'check', 'showmigrations', 'sqlmigrate', 'createsuperuser',
    'loaddata', 'dumpdata', 'flush',
    'check_deploy', 'check_redis', 'check_database', 'sentry_test',
})


def _should_initialize_panaccess() -> bool:
    """
    Decide si este proceso debe inicializar el singleton PanAccess.

    - runserver: solo el proceso hijo (RUN_MAIN=true), no el reloader padre.
    - daphne / gunicorn / celery worker: siempre (no hay RUN_MAIN).
    - migrate, test, shell, etc.: no (evita login innecesario).
    """
    argv = sys.argv
    if not argv:
        return True

    management_cmd = argv[1] if len(argv) > 1 else ''
    if management_cmd in _SKIP_PANACCESS_INIT_COMMANDS:
        return False

    if management_cmd == 'runserver':
        return os.environ.get('RUN_MAIN') == 'true'

    return True


def _check_sync_admin_ip_allowlist() -> None:
    """
    Avisa en el arranque si la restricción por IP de las rutas
    administrativas/sync está desactivada (SyncAdminIPRestrictionMiddleware
    no se activa sin SYNC_ADMIN_IP_ALLOWLIST). Antes esto podía pasar
    desapercibido: esas rutas quedaban protegidas solo por IsAdminUser, sin
    la capa adicional de restricción por IP, sin ningún aviso de que esa
    capa estaba desactivada.
    """
    from django.conf import settings

    if not getattr(settings, "SYNC_ADMIN_IP_ALLOWLIST", None):
        logger.warning(
            "SYNC_ADMIN_IP_ALLOWLIST no está configurado: las rutas "
            "administrativas/sync (/wind/sync-*, /wind/full-sync, "
            "/wind/singleton, /wind/ops/*, /wind/logged-in, /wind/*-stats, "
            "/api/v1/tasks/*) sólo quedan protegidas por autenticación de "
            "admin (IsAdminUser), sin la capa adicional de restricción por "
            "IP. Si es intencional (por ejemplo, otra capa de red ya lo "
            "cubre) puedes ignorar este aviso; si no, define "
            "SYNC_ADMIN_IP_ALLOWLIST en el entorno."
        )


class WindConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'wind'

    def ready(self):
        if not _should_initialize_panaccess():
            return

        _check_sync_admin_ip_allowlist()

        try:
            from wind.services.panaccess_singleton import initialize_panaccess
            logger.info("Inicializando PanAccess singleton...")
            initialize_panaccess()
        except Exception as e:
            logger.error("Error al inicializar PanAccess en ready(): %s", e)
            logger.warning("El sistema intentará autenticarse en el primer request")
