"""
Health y readiness (Fase 3) — sin dependencia de django-health-check.
"""
import hmac
import logging

from django.db import connections
from django.http import JsonResponse
from django.views.decorators.http import require_GET

logger = logging.getLogger(__name__)


def _check_database():
    connections["default"].cursor()
    return "database", None


def _check_cache():
    from django.core.cache import cache

    cache.set("health:probe", "ok", timeout=5)
    if cache.get("health:probe") != "ok":
        return "cache", "read/write failed"
    return "cache", None


def _check_panaccess():
    from wind.services import get_panaccess

    panaccess = get_panaccess()
    panaccess.ensure_session()
    if not panaccess.get_client().session_id:
        return "panaccess", "no session"
    return "panaccess", None


def _panaccess_check_authorized(request) -> bool:
    """
    El check de PanAccess fuerza un login real contra el proveedor (cuenta
    contra su límite de intentos) y antes exponía el texto crudo de sus
    excepciones en la respuesta pública. Ahora solo corre si el llamador
    presenta el token interno (header 'X-Health-Token'), para que /health/
    siga siendo apto para monitoreo externo genérico (uptime checks) sin
    disparar tráfico real hacia PanAccess ni filtrar detalles internos.
    """
    from appConfig import HealthCheckConfig

    token = HealthCheckConfig.TOKEN
    if not token:
        return False
    provided = request.META.get("HTTP_X_HEALTH_TOKEN", "")
    return hmac.compare_digest(provided, token)


@require_GET
def ready_view(request):
    """/ready/ — DB + caché (probes ligeros de orquestación)."""
    errors = []
    for name, fn in (("database", _check_database), ("cache", _check_cache)):
        try:
            key, err = fn()
            if err:
                errors.append(f"{key}: error")
        except Exception:
            logger.exception("Ready check '%s' falló con excepción", name)
            errors.append(f"{name}: error")

    if errors:
        return JsonResponse({"ready": False, "errors": errors}, status=503)
    return JsonResponse({"ready": True})


@require_GET
def health_view(request):
    """
    /health/ — DB + caché siempre; PanAccess solo con token interno válido.

    No expone texto crudo de excepciones: el detalle real se loguea
    server-side, la respuesta pública solo dice "ok"/"error"/"skipped".
    """
    checks = {}
    ok = True

    for label, fn in (("database", _check_database), ("cache", _check_cache)):
        try:
            key, err = fn()
            checks[key] = "ok" if not err else "error"
            if err:
                ok = False
                logger.warning("Health check '%s' falló: %s", key, err)
        except Exception:
            checks[label] = "error"
            ok = False
            logger.exception("Health check '%s' falló con excepción", label)

    if _panaccess_check_authorized(request):
        try:
            key, err = _check_panaccess()
            checks[key] = "ok" if not err else "error"
            if err:
                ok = False
                logger.warning("Health check '%s' falló: %s", key, err)
        except Exception:
            checks["panaccess"] = "error"
            ok = False
            logger.exception("Health check 'panaccess' falló con excepción")
    else:
        checks["panaccess"] = "skipped"

    status = 200 if ok else 503
    return JsonResponse({"healthy": ok, "checks": checks}, status=status)
