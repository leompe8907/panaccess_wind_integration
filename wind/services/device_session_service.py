"""
Fase 3 -- "dispositivos vinculados" (listar/revocar, estilo WhatsApp Web).

Distinto de `udid_auth_service.py` (pareo inicial de TV vía QR, de vida
corta): acá un `DeviceSession` vive mientras el usuario no lo revoque, y
puede corresponder a cualquier dispositivo que se haya logueado por
cualquier método (manual, social, o TV pareada por QR).
"""
from __future__ import annotations

import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction

from wind.models import DeviceSession

logger = logging.getLogger(__name__)


def notify_device_revoked(device_token: str, *, reason: str) -> None:
    """
    Avisa por WebSocket (grupo `device_{token}`) que este dispositivo fue
    revocado. Si el dispositivo sigue conectado a `/ws/device/`,
    `DeviceSessionWS.device_revoked()` lo recibe y fuerza el cierre de
    sesión del lado del cliente de inmediato.

    Si no está conectado en este momento, acá no pasa nada más -- eso es
    intencional: la próxima vez que ese dispositivo intente reconectarse o
    refrescar su registro con este mismo `device_token`, el propio estado
    persistido (`status='revoked'`) lo rechaza (ver
    `DeviceSessionWS._register_or_refresh`), así que la revocación no se
    pierde, solo se aplica en cuanto vuelva a aparecer (misma lógica de
    "eventual consistency" que ya usa WhatsApp con dispositivos offline).
    """
    try:
        channel_layer = get_channel_layer()
        if channel_layer:
            async_to_sync(channel_layer.group_send)(
                f"device_{device_token}",
                {"type": "device.revoked", "reason": reason},
            )
        else:
            logger.warning(
                "Channel layer no disponible; no se notificó revocación de device_token %s...",
                device_token[:8],
            )
    except Exception:
        logger.exception(
            "Error notificando WebSocket de revocación para device_token %s...",
            device_token[:8],
        )


def revoke_device_session(
    *, subscriber_code: str, device_session_id: int, reason: str = "revoked_by_subscriber"
) -> dict:
    """
    Revoca un `DeviceSession`, verificando dueño antes de tocar nada.

    Crítico: el filtro por `subscriber_code` va en la MISMA consulta que
    busca el registro por `pk` -- si el id existe pero pertenece a otro
    suscriptor, esto da exactamente el mismo resultado (`not_found`) que si
    no existiera en absoluto. Así, ninguna cuenta logueada puede usar este
    endpoint para (a) confirmar que cierto id existe, ni (b) revocar el
    dispositivo de otra cuenta -- sin este filtro compuesto, cualquiera con
    sesión podría desloguear dispositivos ajenos con solo probar ids.
    """
    try:
        with transaction.atomic():
            try:
                session = DeviceSession.objects.select_for_update().get(
                    pk=device_session_id, subscriber_code=subscriber_code,
                )
            except DeviceSession.DoesNotExist:
                return {"ok": False, "code": "not_found", "error": "Device not found"}

            if session.status != "active":
                return {"ok": False, "code": "already_revoked", "error": "Device already revoked"}

            session.revoke(reason=reason)
            device_token = session.device_token

            transaction.on_commit(lambda: notify_device_revoked(device_token, reason=reason))

        return {"ok": True}

    except Exception as e:
        return {
            "ok": False,
            "code": "internal_error",
            "error": "Internal server error",
            "details": str(e),
        }


def revoke_all_device_sessions_for_subscriber(subscriber_code: str, *, reason: str) -> int:
    """
    Revoca TODOS los `DeviceSession` activos de un `subscriber_code` de una
    sola vez (Fase 4) -- se llama desde `password_reset.sync_password_locally`
    (cambio de contraseña) y `subscriber_closure` (cierre de cuenta).

    Sin esto, cambiar la contraseña o cerrar la cuenta solo invalidaba el
    JWT (`mark_password_changed`/`invalidate_active_sessions`) -- cualquier
    TV/app ya vinculada (`DeviceSession`) seguía apareciendo "activa" en el
    dashboard, y si tenía una conexión `/ws/device/` abierta en ese momento,
    no recibía ningún aviso de cierre de sesión hasta que intentara
    reconectarse por su cuenta. Ahora, además de cortar el JWT, se revoca
    en bloque cada dispositivo vinculado y se le avisa en vivo si sigue
    conectado -- mismo mecanismo que la revocación individual desde el
    dashboard (`revoke_device_session`), aplicado a todos a la vez.

    Devuelve cuántos `DeviceSession` se revocaron (0 si no había ninguno
    activo, o si `subscriber_code` viene vacío).
    """
    if not subscriber_code:
        return 0

    with transaction.atomic():
        sessions = list(
            DeviceSession.objects.select_for_update().filter(
                subscriber_code=subscriber_code, status="active"
            )
        )
        device_tokens = []
        for session in sessions:
            session.revoke(reason=reason)
            device_tokens.append(session.device_token)

        if device_tokens:
            transaction.on_commit(lambda tokens=device_tokens: _notify_many_revoked(tokens, reason))

    return len(sessions)


def _notify_many_revoked(device_tokens: list, reason: str) -> None:
    for device_token in device_tokens:
        notify_device_revoked(device_token, reason=reason)
