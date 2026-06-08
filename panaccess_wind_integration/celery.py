import os
from celery import Celery

# Asegurar que las settings de Django estén cargadas
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "panaccess_wind_integration.settings")

app = Celery("panaccess_wind_integration")

# Cargar configuración desde Django usando el prefijo CELERY_
app.config_from_object("django.conf:settings", namespace="CELERY")

# Autodiscover tasks.py en las apps instaladas
app.autodiscover_tasks()


@app.task(bind=True)
def debug_task(self):
    """
    Tarea de diagnóstico. Útil para validar la integración Celery-Django.
    """
    print(f"Request: {self.request!r}")
