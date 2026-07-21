"""
Fase 3 -- consumer de WebSocket para "dispositivos vinculados"
(`/ws/device/`). Separado de `consumers.py` (pareo inicial de TV vía QR,
`/ws/auth/`) porque son protocolos y credenciales distintas: `/ws/auth/`
se autentica con `temp_token` (secreto de un pareo de vida corta),
`/ws/device/` se autentica con el JWT de una sesión ya logueada (manual,
social o TV pareada) y registra un dispositivo de vida larga.
"""
import asyncio
import json
import logging
from urllib.parse import parse_qs

from asgiref.sync import sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder

from django.db import transaction

from wind.models import DeviceSession
from wind.services.subscriber_catalog import resolve_subscriber_code_for_user
from wind.utils.websocket_utils import (
    generate_device_fingerprint,
    check_websocket_limits,
    decrement_websocket_limits,
    check_token_bucket_lua,
)
from wind.utils.ws_auth import resolve_user_from_jwt

logger = logging.getLogger(__name__)

MAX_DEVICE_MODEL_LEN = 200
# `device_type` es un CharField(max_length=50) con choices -- sin truncar,
# un valor más largo revienta el INSERT/UPDATE con una excepción de BD no
# manejada (revisión adversarial).
MAX_DEVICE_TYPE_LEN = 50

# Cuántos `DeviceSession` NUEVOS puede crear un mismo subscriber_code por
# hora. Sin esto, una sola conexión autenticada (JWT válido) podía mandar
# `register_device` sin límite y crear una fila nueva cada vez -- a
# diferencia del pareo UDID (que sí tiene límite por udid), este camino no
# tenía ningún tope (revisión adversarial). No limita refrescar un
# `device_token` ya existente (reconexiones legítimas), solo la creación.
DEVICE_REGISTER_MAX_PER_HOUR = getattr(settings, "DEVICE_REGISTER_MAX_PER_HOUR", 20)


def _get_header(scope, key: str) -> str:
    headers = dict(scope.get("headers", []))
    return headers.get(key.encode().lower(), b"").decode(errors="ignore")


def _register_or_refresh_device(
    subscriber_code: str,
    existing_token: str,
    device_type: str | None,
    device_model: str | None,
    client_ip: str,
    user_agent: str,
):
    """
    Corre en un hilo síncrono (vía sync_to_async) porque toca el ORM.

    Devuelve (session, is_new, error_code):
      - (session, False, None) si se refrescó un `device_token` existente.
      - (session, True, None) si se creó uno nuevo.
      - (None, False, "device_token_invalid") si `existing_token` no es
        reutilizable -- pertenece a otro subscriber_code (nunca se
        reasigna un token entre cuentas) o ya está revocado (el
        dispositivo debe tratarlo igual que un `device_revoked` en vivo:
        borrar su copia local y volver a loguearse).
      - (None, False, "rate_limited") si este subscriber_code ya alcanzó
        el máximo de dispositivos NUEVOS por hora (ver
        DEVICE_REGISTER_MAX_PER_HOUR).

    El camino de refresco usa `select_for_update()` dentro de una
    transacción -- sin esto, una revocación que comitea justo entre el
    `.get()` y el `.save()` de este mismo dispositivo dejaría la conexión
    reportándose "registrada" con éxito aunque ya estuviera revocada
    (condición de carrera encontrada en la revisión adversarial).
    """
    if existing_token:
        with transaction.atomic():
            try:
                session = DeviceSession.objects.select_for_update().get(device_token=existing_token)
            except DeviceSession.DoesNotExist:
                session = None

            if session is not None:
                if session.subscriber_code != subscriber_code or session.status != "active":
                    return None, False, "device_token_invalid"
                session.device_type = (device_type or session.device_type or "")[:MAX_DEVICE_TYPE_LEN] or None
                session.device_model = device_model or session.device_model
                session.client_ip = client_ip
                session.user_agent = user_agent
                session.save(
                    update_fields=["device_type", "device_model", "client_ip", "user_agent", "last_seen_at"]
                )
                return session, False, None

    # A partir de acá se crea un `DeviceSession` NUEVO -- limitado por
    # subscriber_code (no bloquea refrescar uno ya existente, solo la
    # creación, para no penalizar reconexiones legítimas del mismo
    # dispositivo).
    is_allowed, _remaining, _retry_after = check_token_bucket_lua(
        f"device_register:{subscriber_code}",
        capacity=DEVICE_REGISTER_MAX_PER_HOUR,
        window_seconds=3600,
    )
    if not is_allowed:
        return None, False, "rate_limited"

    session = DeviceSession.objects.create(
        subscriber_code=subscriber_code,
        device_type=(device_type or "")[:MAX_DEVICE_TYPE_LEN] or None,
        device_model=device_model,
        client_ip=client_ip,
        user_agent=user_agent,
    )
    return session, True, None


class DeviceSessionWS(AsyncWebsocketConsumer):
    """
    Registro de "dispositivos vinculados" -- cualquier cliente que termine
    de loguearse (manual, social, o TV pareada por QR) abre esta conexión
    para identificarse (tipo/modelo de dispositivo) y quedar listado y
    revocable desde el dashboard del suscriptor.
    """

    MAX_CONNECTIONS_PER_TOKEN = getattr(settings, "DEVICE_WS_MAX_PER_TOKEN", 10)
    MAX_GLOBAL_CONNECTIONS = getattr(settings, "DEVICE_WS_MAX_GLOBAL", 1000)
    PING_INTERVAL = getattr(settings, "DEVICE_WS_PING_INTERVAL", 30)

    async def connect(self):
        self.done = False
        self.subscriber_code = None
        self.device_token = None
        self.group_name = None
        self.device_fingerprint = None
        self.ping_task = None

        query_string = self.scope.get("query_string", b"").decode(errors="ignore")
        raw_token = (parse_qs(query_string).get("token") or [""])[0]

        user = await sync_to_async(resolve_user_from_jwt)(raw_token)
        if not user or not getattr(user, "is_active", False):
            await self.close(code=4001)
            return

        self.subscriber_code = await sync_to_async(resolve_subscriber_code_for_user)(user)
        if not self.subscriber_code:
            await self.close(code=4004)
            return

        self.device_fingerprint = await sync_to_async(generate_device_fingerprint)(self.scope)
        is_allowed, reason, retry_after = await sync_to_async(check_websocket_limits)(
            udid=None,
            device_fingerprint=self.device_fingerprint,
            max_per_token=self.MAX_CONNECTIONS_PER_TOKEN,
            max_global=self.MAX_GLOBAL_CONNECTIONS,
        )
        if not is_allowed:
            await self.close(code=4001, reason=f"{reason}. Retry after {retry_after}s")
            return

        await self.accept()
        self.ping_task = asyncio.create_task(self._ping_loop())

    async def receive(self, text_data=None, bytes_data=None):
        if self.done:
            return

        try:
            data = json.loads(text_data or "{}")
        except Exception:
            return await self._send_err("bad_json", "El cuerpo debe ser JSON", close=True)

        msg_type = data.get("type")
        if msg_type == "ping":
            return await self._send_json({"type": "pong"})
        if msg_type == "pong":
            return

        if msg_type != "register_device":
            return await self._send_err("bad_type", "Usa type=register_device", close=True)

        device_type = str(data.get("device_type") or "").strip() or None
        device_model = str(data.get("device_model") or "").strip()[:MAX_DEVICE_MODEL_LEN] or None
        existing_token = str(data.get("device_token") or "").strip()

        client_ip = (self.scope.get("client") or [""])[0] or ""
        user_agent = _get_header(self.scope, "user-agent")

        try:
            session, is_new, error_code = await sync_to_async(_register_or_refresh_device)(
                self.subscriber_code, existing_token, device_type, device_model, client_ip, user_agent,
            )
        except Exception:
            logger.exception("Error registrando/refrescando DeviceSession")
            return await self._send_err("internal_error", "Error interno registrando el dispositivo", close=True)

        if error_code == "rate_limited":
            return await self._send_err(
                "rate_limited", "Demasiados dispositivos nuevos registrados, intenta más tarde", close=True
            )

        if session is None:
            # `existing_token` no es de esta cuenta o ya fue revocado -- el
            # cliente debe tratarlo igual que una revocación en vivo (borrar
            # su copia local, volver a loguearse / re-registrarse sin token).
            return await self._send_err("device_token_invalid", "device_token no reconocido", close=True)

        # Si esta misma conexión ya estaba unida a otro grupo (p.ej. el
        # cliente mandó un segundo `register_device` con un token distinto
        # al de la primera vez), sale del anterior antes de unirse al nuevo
        # -- evita quedar suscrito a dos grupos device_{token} a la vez.
        if self.group_name and self.group_name != f"device_{session.device_token}":
            try:
                await self.channel_layer.group_discard(self.group_name, self.channel_name)
            except Exception:
                pass

        self.device_token = session.device_token
        self.group_name = f"device_{self.device_token}"
        await self.channel_layer.group_add(self.group_name, self.channel_name)

        await self._send_json({
            "type": "device_registered",
            "device_token": self.device_token,
            "is_new": is_new,
        })

    async def device_revoked(self, event):
        """Handler para eventos de grupo (`device.revoked`, ver device_session_service.py)."""
        if self.done:
            return
        await self._send_json({"type": "device_revoked", "reason": event.get("reason")})
        self.done = True
        await self.close()

    async def disconnect(self, code):
        await self._cleanup()

    async def _ping_loop(self):
        try:
            while not self.done:
                await asyncio.sleep(self.PING_INTERVAL)
                if not self.done:
                    try:
                        await self._send_json({"type": "ping"})
                    except Exception:
                        break
        except asyncio.CancelledError:
            pass

    async def _send_err(self, code: str, detail: str, close: bool = False):
        await self._send_json({"type": "error", "code": code, "detail": detail})
        if close:
            await self.close(code=1011)

    async def _send_json(self, obj: dict):
        try:
            await self.send(text_data=json.dumps(obj, cls=DjangoJSONEncoder))
        except Exception:
            await self.close(code=1011)

    async def _cleanup(self):
        self.done = True
        if getattr(self, "device_fingerprint", None):
            await sync_to_async(decrement_websocket_limits)(
                udid=None, device_fingerprint=self.device_fingerprint
            )
        if getattr(self, "group_name", None):
            try:
                await self.channel_layer.group_discard(self.group_name, self.channel_name)
            except Exception:
                pass
        if getattr(self, "ping_task", None) and not self.ping_task.done():
            self.ping_task.cancel()
