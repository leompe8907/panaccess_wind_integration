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


def notify_device_list_changed(subscriber_code: str) -> None:
    """
    Avisa (grupo `subscriber_devices_{subscriber_code}`, uno por CUENTA, no
    por dispositivo) que la lista de "dispositivos vinculados" de este
    suscriptor cambió -- a diferencia de `notify_device_revoked` (que solo
    le llega al dispositivo puntual afectado, y le fuerza el logout), este
    aviso les llega a TODOS los demás dispositivos conectados de la misma
    cuenta, y no implica ninguna acción destructiva de su lado: es solo una
    señal de "volvé a pedir `GET /wind/devices/` si tenés esa pantalla
    abierta". Antes no existía ningún canal así -- si dos dispositivos de la
    misma cuenta tenían el panel de "dispositivos vinculados" abierto a la
    vez, revocar uno desde el otro no actualizaba la lista del que se quedó
    con la sesión activa hasta que alguien apretara "Actualizar" a mano.

    Mismo criterio "eventual consistency" que `notify_device_revoked`: si
    nadie está conectado en este momento, no pasa nada más -- no hace falta,
    el próximo `GET /wind/devices/` ya trae los datos al día de todos modos.
    """
    try:
        channel_layer = get_channel_layer()
        if channel_layer:
            async_to_sync(channel_layer.group_send)(
                f"subscriber_devices_{subscriber_code}",
                {"type": "device.list_changed"},
            )
        else:
            logger.warning(
                "Channel layer no disponible; no se notificó cambio de lista de "
                "dispositivos para subscriber_code=%s",
                subscriber_code,
            )
    except Exception:
        logger.exception(
            "Error notificando WebSocket de cambio de lista de dispositivos "
            "para subscriber_code=%s",
            subscriber_code,
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
            # Aviso aparte para el resto de los dispositivos de esta cuenta
            # (no el que se acaba de revocar, ese ya recibe su propio
            # `device_revoked` arriba) -- para que cualquier pantalla de
            # "dispositivos vinculados" abierta en otro dispositivo se
            # refresque sola, sin necesitar que alguien apriete "Actualizar".
            transaction.on_commit(lambda: notify_device_list_changed(subscriber_code))

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
    activo, o si `subscriber_code` viene vacío), o `None` si algo falló
    internamente -- esta función es un efecto colateral de seguridad sobre
    un cambio de contraseña o un cierre de cuenta que YA ocurrió; nunca
    debe dejar escapar una excepción que le haga parecer al caller que la
    acción principal (cambiar contraseña / cerrar cuenta) falló cuando en
    realidad sí se completó (revisión adversarial: antes de este try/except
    una falla acá se propagaba tal cual, y los tres call-sites de
    `sync_password_locally` capturan cualquier excepción y responden
    "no se pudo cambiar la contraseña" -- un password ya cambiado con
    normalidad quedaría reportado como error al usuario).
    """
    if not subscriber_code:
        return 0

    try:
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
    except Exception:
        logger.exception(
            "Error revocando DeviceSession en bloque para subscriber_code=%s (reason=%s)",
            subscriber_code,
            reason,
        )
        return None


def _notify_many_revoked(device_tokens: list, reason: str) -> None:
    for device_token in device_tokens:
        notify_device_revoked(device_token, reason=reason)
