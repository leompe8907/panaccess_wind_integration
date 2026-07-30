import os
from celery import Celery
from celery.signals import task_postrun, task_prerun

# Asegurar que las settings de Django estén cargadas
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "panaccess_wind_integration.settings")

app = Celery("panaccess_wind_integration")

# Cargar configuración desde Django usando el prefijo CELERY_
app.config_from_object("django.conf:settings", namespace="CELERY")

# Autodiscover tasks.py en las apps instaladas
app.autodiscover_tasks()


@task_prerun.connect
def _close_old_connections_before_task(**kwargs):
    """
    Django solo revalida (CONN_HEALTH_CHECKS) o recicla (CONN_MAX_AGE) las
    conexiones de BD persistentes cuando algo llama a close_old_connections()
    -- y eso normalmente lo disparan las señales request_started/
    request_finished del ciclo request/response HTTP, que Celery NUNCA
    emite. Sin este hook, una tarea periódica de Celery beat (p.ej. cada 10
    min) reutiliza para siempre la misma conexión de su hilo worker,
    incluyendo una que Postgres ya cerró por su cuenta -- eso es justo lo que
    causó los "connection already closed" / celery.beat.SchedulingError
    vistos en producción corriendo sin parar durante horas. Se fuerza acá,
    antes de cada tarea, la misma revalidación que un request HTTP tendría
    gratis.
    """
    from django.db import close_old_connections

    close_old_connections()


@task_postrun.connect
def _close_old_connections_after_task(**kwargs):
    """Igual que el hook de arriba, pero al terminar la tarea -- para no
    dejar la conexión abierta ociosa hasta la siguiente ejecución programada
    (contribuía a los agotamientos de pool "reserved for roles with the
    SUPERUSER" vistos junto con getSmartcard.py)."""
    from django.db import close_old_connections

    close_old_connections()


@app.task(bind=True)
def debug_task(self):
    """
    Tarea de diagnóstico. Útil para validar la integración Celery-Django.
    """
    print(f"Request: {self.request!r}")
