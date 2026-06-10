"""
Restringe rutas operativas de sync/admin por IP (roadmap #5).

Activo solo si SYNC_ADMIN_IP_ALLOWLIST está definido en .env.
Útil detrás de nginx con TLS; complementa reglas deny/allow en nginx.
"""
import ipaddress
import logging

from django.http import JsonResponse

logger = logging.getLogger(__name__)

# Prefijos de rutas que no deben ser públicas en internet
_PROTECTED_PREFIXES = (
    "/wind/sync-",
    "/wind/compare-and-update",
    "/wind/full-sync",
    "/wind/singleton",
    "/wind/ops/",
    "/api/v1/tasks/",
)


def _client_ip(request) -> str:
    """IP del cliente; usa X-Forwarded-For si el proxy es de confianza (nginx local)."""
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR") or ""


def _ip_allowed(client_ip: str, allowlist: list[str]) -> bool:
    if not client_ip:
        return False
    try:
        addr = ipaddress.ip_address(client_ip)
    except ValueError:
        return False

    for entry in allowlist:
        entry = entry.strip()
        if not entry:
            continue
        try:
            if "/" in entry:
                if addr in ipaddress.ip_network(entry, strict=False):
                    return True
            elif client_ip == entry:
                return True
        except ValueError:
            continue
    return False


class SyncAdminIPRestrictionMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from django.conf import settings

        allowlist = getattr(settings, "SYNC_ADMIN_IP_ALLOWLIST", None) or []
        if allowlist:
            path = request.path
            if any(path.startswith(prefix) for prefix in _PROTECTED_PREFIXES):
                client_ip = _client_ip(request)
                if not _ip_allowed(client_ip, allowlist):
                    logger.warning(
                        "Acceso denegado a %s desde IP %s (no está en SYNC_ADMIN_IP_ALLOWLIST)",
                        path,
                        client_ip,
                    )
                    return JsonResponse(
                        {
                            "success": False,
                            "message": "Acceso no permitido desde esta red.",
                        },
                        status=403,
                    )

        return self.get_response(request)
