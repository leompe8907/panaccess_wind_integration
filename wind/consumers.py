import json
import asyncio
import time
import logging

from channels.generic.websocket import AsyncWebsocketConsumer
from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder

import hmac

from wind.services.udid_auth_service import authenticate_with_udid_service, FATAL_CODES
from wind.utils.websocket_utils import (
    generate_device_fingerprint,
    check_websocket_limits,
    decrement_websocket_limits,
)

logger = logging.getLogger(__name__)

def _get_header(scope, key: str) -> str:
    headers = dict(scope.get("headers", []))
    return headers.get(key.encode().lower(), b"").decode(errors="ignore")


class AuthWaitWS(AsyncWebsocketConsumer):
    """
    Protocolo de emparejamiento para Smart TVs.
    """

    TIMEOUT_AUTOMATIC = getattr(settings, "UDID_WAIT_TIMEOUT_AUTOMATIC", 300)
    TIMEOUT_MANUAL = getattr(settings, "UDID_WAIT_TIMEOUT_MANUAL", 300)
    TIMEOUT_SECONDS = getattr(settings, "UDID_WAIT_TIMEOUT", TIMEOUT_AUTOMATIC)
    ENABLE_POLLING = getattr(settings, "UDID_ENABLE_POLLING", False)
    POLL_INTERVAL = getattr(settings, "UDID_POLL_INTERVAL", 2)
    PING_INTERVAL = getattr(settings, "UDID_WS_PING_INTERVAL", 30)
    INACTIVITY_TIMEOUT = getattr(settings, "UDID_WS_INACTIVITY_TIMEOUT", 180)
    MAX_CONNECTIONS_PER_TOKEN = getattr(settings, "UDID_WS_MAX_PER_TOKEN", 3)
    MAX_GLOBAL_CONNECTIONS = getattr(settings, "UDID_WS_MAX_GLOBAL", 1000)

    async def connect(self):
        self.udid = None
        self.temp_token = None
        self.app_type = None
        self.app_version = None
        self.group_name = None
        self.done = False
        self.device_fingerprint = None
        self.last_activity = time.time()

        self.timeout_task = None
        self.poll_task = None
        self.ping_task = None
        self.inactivity_task = None

        # Rate limiting por device fingerprint. Antes había dos sistemas de
        # conteo de conexiones corriendo en paralelo para lo mismo
        # (`check_websocket_limits`, directo contra Redis, y
        # `check_websocket_rate_limit`/`increment_websocket_connection`,
        # basado en el cache de Django) -- podían desincronizarse entre sí
        # y duplicaban configuración (dos límites/ventanas distintos para
        # la misma conexión). Se consolida en uno solo: el conteo directo
        # en Redis (ver auditoría).
        self.device_fingerprint = await sync_to_async(generate_device_fingerprint)(self.scope)

        is_allowed, reason, retry_after = await sync_to_async(check_websocket_limits)(
            udid=None,
            device_fingerprint=self.device_fingerprint,
            max_per_token=self.MAX_CONNECTIONS_PER_TOKEN,
            max_global=self.MAX_GLOBAL_CONNECTIONS
        )

        if not is_allowed:
            await self.close(code=4001, reason=f"{reason}. Retry after {retry_after}s")
            return

        await self.accept()
        
        self.ping_task = asyncio.create_task(self._ping_loop())
        self.inactivity_task = asyncio.create_task(self._inactivity_check())

    async def receive(self, text_data=None, bytes_data=None):
        if self.done:
            return

        try:
            data = json.loads(text_data or "{}")
        except Exception:
            return await self._send_err("bad_json", "El cuerpo debe ser JSON", close=True)

        self.last_activity = time.time()
        
        if data.get("type") == "ping":
            return await self._send_json({"type": "pong"})
        
        if data.get("type") == "pong":
            return 

        if data.get("type") != "auth_with_udid":
            return await self._send_err("bad_type", "Usa type=auth_with_udid", close=True)

        self.udid = (data.get("udid") or "").strip()
        self.temp_token = (data.get("temp_token") or "").strip()
        self.app_type = (data.get("app_type") or "web").strip()
        self.app_version = (data.get("app_version") or "1.0").strip()
        if not self.udid:
            return await self._send_err("missing_udid", "UDID es requerido", close=True)

        if not self.temp_token:
            return await self._send_err("missing_temp_token", "temp_token es requerido", close=True)

        if self.udid:
            is_allowed_new, reason_new, retry_after_new = await sync_to_async(check_websocket_limits)(
                udid=self.udid,
                device_fingerprint=self.device_fingerprint,
                max_per_token=self.MAX_CONNECTIONS_PER_TOKEN,
                max_global=self.MAX_GLOBAL_CONNECTIONS
            )

            if not is_allowed_new:
                await self._send_err(
                    "rate_limit_exceeded",
                    f"{reason_new}. Retry after {retry_after_new}s",
                    close=True
                )
                return

        client_ip = (self.scope.get("client") or [""])[0] or ""
        user_agent = _get_header(self.scope, "user-agent")

        res = await sync_to_async(authenticate_with_udid_service)(
            udid=self.udid,
            temp_token=self.temp_token,
            app_type=self.app_type,
            app_version=self.app_version,
            client_ip=client_ip,
            user_agent=user_agent,
        )

        if res.get("ok"):
            await self._send_result(res)
            return await self.close()

        if res.get("code") in FATAL_CODES:
            await self._send_result(res, status="error")
            return await self.close()

        from wind.models import UDIDAuthRequest
        try:
            udid_request = await sync_to_async(UDIDAuthRequest.objects.get)(udid=self.udid)
            timeout_seconds = self.TIMEOUT_MANUAL if udid_request.method == 'manual' else self.TIMEOUT_AUTOMATIC
        except Exception:
            timeout_seconds = self.TIMEOUT_SECONDS

        self.group_name = f"udid_{self.udid}"
        try:
            max_retries = 3
            base_delay = 0.5
            for attempt in range(max_retries):
                try:
                    await self.channel_layer.group_add(self.group_name, self.channel_name)
                    break
                except Exception as e:
                    if attempt < max_retries - 1:
                        delay = base_delay * (2 ** attempt)
                        logger.warning(
                            f"Error suscribiendo WebSocket al grupo {self.group_name} "
                            f"(intento {attempt + 1}/{max_retries}): {e}. Reintentando en {delay}s..."
                        )
                        await asyncio.sleep(delay)
                        continue
                    else:
                        raise
        except Exception as e:
            logger.error(f"Error suscribiendo WebSocket al grupo {self.group_name}: {e}", exc_info=True)
            await self._send_err("channel_layer_unavailable", f"Error de conexión: {str(e)}", close=True)
            return

        await self._send_json({
            "type": "pending",
            "status": res.get("status") or "not_validated",
            "detail": res.get("error") or "Esperando validación/asociación de UDID…",
            "timeout": timeout_seconds,
        })

        self.timeout_task = asyncio.create_task(self._timeout_with_seconds(timeout_seconds))

        if self.ENABLE_POLLING:
            self.poll_task = asyncio.create_task(self._poll_every(self.POLL_INTERVAL))

    async def udid_validated(self, event):
        """Handler para eventos de grupo (udid.validated)"""
        if self.done or not self.udid or event.get("udid") != self.udid:
            return

        client_ip = (self.scope.get("client") or [""])[0] or ""
        user_agent = _get_header(self.scope, "user-agent")

        res = await sync_to_async(authenticate_with_udid_service)(
            udid=self.udid,
            temp_token=self.temp_token,
            app_type=self.app_type,
            app_version=self.app_version,
            client_ip=client_ip,
            user_agent=user_agent,
        )

        await self._send_result(res, status=("ok" if res.get("ok") else "error"))
        await self._finish()

    async def disconnect(self, code):
        await self._cleanup()

    async def _send_result(self, res: dict, status: str | None = None):
        self.done = True
        # `res` puede traer un `details` con el texto crudo de una excepción
        # interna (ver authenticate_with_udid_service, casos
        # encryption_failed/internal_error) -- no reenviarlo tal cual a un
        # cliente que todavía ni se autenticó (mismo criterio ya aplicado a
        # las respuestas HTTP equivalentes, ver auditoría).
        safe_res = {k: v for k, v in res.items() if k != "details"}
        payload = {
            "type": "auth_with_udid:result",
            "status": status or ("ok" if res.get("ok") else "error"),
            "result": safe_res,
        }
        await self._send_json(payload)

    async def _send_err(self, code: str, detail: str, close: bool = False):
        await self._send_json({"type": "error", "code": code, "detail": detail})
        if close:
            await self.close(code=1011)

    async def _send_json(self, obj: dict):
        try:
            await self.send(text_data=json.dumps(obj, cls=DjangoJSONEncoder))
        except Exception as e:
            try:
                await self.send(text_data=json.dumps({
                    "type": "error",
                    "code": "serialization_error",
                    "detail": str(e),
                }, cls=DjangoJSONEncoder))
            finally:
                await self.close(code=1011)

    async def _timeout_with_seconds(self, timeout_seconds: int):
        await asyncio.sleep(timeout_seconds)
        if not self.done:
            await self._send_json({"type": "timeout", "detail": f"No se recibió validación/asociación a tiempo (timeout: {timeout_seconds}s)."})
            await self._finish()

    async def _poll_every(self, seconds: int):
        try:
            while not self.done:
                await asyncio.sleep(seconds)

                client_ip = (self.scope.get("client") or [""])[0] or ""
                user_agent = _get_header(self.scope, "user-agent")

                res = await sync_to_async(authenticate_with_udid_service)(
                    udid=self.udid,
                    temp_token=self.temp_token,
                    app_type=self.app_type,
                    app_version=self.app_version,
                    client_ip=client_ip,
                    user_agent=user_agent,
                )

                if res.get("ok"):
                    await self._send_result(res, status="ok")
                    return await self._finish()

                if res.get("code") in FATAL_CODES:
                    await self._send_result(res, status="error")
                    return await self._finish()
        except asyncio.CancelledError:
            pass

    async def _finish(self):
        await self._cleanup()
        try:
            await self.close()
        except Exception:
            pass

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

    async def _inactivity_check(self):
        try:
            while not self.done:
                await asyncio.sleep(10)
                if not self.done:
                    inactivity_time = time.time() - self.last_activity
                    if inactivity_time > self.INACTIVITY_TIMEOUT:
                        await self._send_json({
                            "type": "error",
                            "code": "inactivity_timeout",
                            "detail": f"Connection closed due to inactivity ({self.INACTIVITY_TIMEOUT}s)"
                        })
                        await self._finish()
                        break
        except asyncio.CancelledError:
            pass

    async def _cleanup(self):
        self.done = True

        if self.device_fingerprint:
            await sync_to_async(decrement_websocket_limits)(
                udid=self.udid,
                device_fingerprint=self.device_fingerprint
            )

        if getattr(self, "group_name", None):
            try:
                await self.channel_layer.group_discard(self.group_name, self.channel_name)
            except Exception:
                pass

        for tname in ("timeout_task", "poll_task", "ping_task", "inactivity_task"):
            task = getattr(self, tname, None)
            if task and not task.done():
                task.cancel()
