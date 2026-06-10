"""
Health y readiness (Fase 3) — sin dependencia de django-health-check.
"""
from django.db import connections
from django.http import JsonResponse
from django.views.decorators.http import require_GET


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


@require_GET
def ready_view(request):
    """/ready/ — DB + caché (probes ligeros de orquestación)."""
    errors = []
    for name, fn in (("database", _check_database), ("cache", _check_cache)):
        try:
            key, err = fn()
            if err:
                errors.append(f"{key}: {err}")
        except Exception as exc:
            errors.append(f"{name}: {exc}")

    if errors:
        return JsonResponse({"ready": False, "errors": errors}, status=503)
    return JsonResponse({"ready": True})


@require_GET
def health_view(request):
    """/health/ — DB + caché + sesión PanAccess."""
    checks = {}
    ok = True

    for label, fn in (
        ("database", _check_database),
        ("cache", _check_cache),
        ("panaccess", _check_panaccess),
    ):
        try:
            key, err = fn()
            checks[key] = "ok" if not err else err
            if err:
                ok = False
        except Exception as exc:
            checks[label] = str(exc)
            ok = False

    status = 200 if ok else 503
    return JsonResponse({"healthy": ok, "checks": checks}, status=status)
