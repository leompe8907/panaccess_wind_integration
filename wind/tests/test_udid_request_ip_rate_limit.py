"""
Tests para la segunda capa de rate limit (por IP) de `RequestUDIDManualView`.

Contexto: el rate limit existente para `GET /wind/request-udid-manual/` es
por `device_fingerprint` (1 cada 5 minutos), pero ese fingerprint se deriva
server-side de headers (User-Agent, Accept-Language, etc.) que un cliente
insistente puede rotar en cada request para obtener un fingerprint "nuevo"
cada vez -- ver `check_udid_request_ip_rate_limit` en
`wind/utils/websocket_utils.py` para el detalle completo. Este archivo cubre
tanto la función de rate limit en sí como su integración en la vista.
"""
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APITestCase
from rest_framework import status

from wind.utils.websocket_utils import check_udid_request_ip_rate_limit

# `log_audit_async` intenta un RPUSH a Redis en cada request (ver
# `wind/utils/log_buffer.py`) -- sin Redis disponible en el entorno de
# tests, cada intento fallido agrega varios segundos de latencia real por
# request (timeout de conexión), y estos tests hacen muchos requests
# seguidos a propósito. Se mockea el cliente Redis (no la lógica de rate
# limit bajo prueba) para que la suite corra en tiempo razonable.
_no_redis = patch("wind.utils.log_buffer._get_redis_client", return_value=None)


class CheckUdidRequestIpRateLimitTestCase(TestCase):
    def setUp(self):
        # LocMemCache no se limpia solo entre tests del mismo proceso --
        # sin esto, el orden de ejecución podía contaminar los contadores
        # de una IP entre tests (mismo bug ya visto en
        # test_udid_account_association.py).
        cache.clear()

    def test_no_ip_is_not_allowed(self):
        allowed, remaining, retry_after = check_udid_request_ip_rate_limit(None)
        self.assertFalse(allowed)
        self.assertEqual(remaining, 0)

    def test_allows_up_to_max_requests(self):
        ip = "203.0.113.10"
        for i in range(10):
            allowed, remaining, retry_after = check_udid_request_ip_rate_limit(
                ip, max_requests=10, window_minutes=5
            )
            self.assertTrue(allowed, f"request {i + 1} debería haber sido permitida")
            self.assertEqual(remaining, 10 - (i + 1))

    def test_blocks_after_max_requests(self):
        ip = "203.0.113.11"
        for _ in range(10):
            check_udid_request_ip_rate_limit(ip, max_requests=10, window_minutes=5)

        allowed, remaining, retry_after = check_udid_request_ip_rate_limit(
            ip, max_requests=10, window_minutes=5
        )
        self.assertFalse(allowed)
        self.assertEqual(remaining, 0)
        self.assertEqual(retry_after, 5 * 60)

    def test_different_ips_are_isolated(self):
        ip_a = "203.0.113.20"
        ip_b = "203.0.113.21"
        for _ in range(10):
            check_udid_request_ip_rate_limit(ip_a, max_requests=10, window_minutes=5)

        # ip_a ya agotó su cupo, pero ip_b no debe verse afectada.
        allowed, remaining, retry_after = check_udid_request_ip_rate_limit(
            ip_b, max_requests=10, window_minutes=5
        )
        self.assertTrue(allowed)
        self.assertEqual(remaining, 9)


class RequestUDIDManualViewIpRateLimitTestCase(APITestCase):
    """
    Confirma que la vista real corta por IP incluso cuando el
    device_fingerprint cambia en cada request (simulando un cliente que
    rota headers para esquivar ese límite) -- el escenario exacto que esta
    segunda capa está pensada para cubrir.
    """

    def setUp(self):
        cache.clear()

    def _request_with_rotating_fingerprint(self, i, ip="198.51.100.5"):
        return self.client.get(
            "/wind/request-udid-manual/",
            REMOTE_ADDR=ip,
            HTTP_USER_AGENT=f"RotatingClient/{i}",
        )

    @_no_redis
    def test_ip_limit_blocks_even_with_rotating_fingerprint(self, _mock_redis):
        # Cada request usa un User-Agent distinto -> fingerprint distinto
        # cada vez, así que el límite por fingerprint (1/5min) nunca se
        # dispara solo -- el límite por IP (10/5min) es el que tiene que
        # cortar acá.
        responses = [self._request_with_rotating_fingerprint(i) for i in range(10)]
        for i, resp in enumerate(responses):
            self.assertEqual(
                resp.status_code, status.HTTP_201_CREATED,
                f"request {i + 1} debería haber pasado (body: {resp.data})"
            )

        blocked = self._request_with_rotating_fingerprint(10)
        self.assertEqual(blocked.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertEqual(blocked.data["error_code"], "IP_RATE_LIMIT_EXCEEDED")

    @_no_redis
    def test_fingerprint_limit_still_applies_within_ip_quota(self, _mock_redis):
        # Mismo User-Agent (mismo fingerprint) dos veces seguidas, sin
        # agotar el cupo de IP -- el límite por fingerprint (1/5min) debe
        # cortar en el segundo intento, como ya hacía antes de este cambio.
        first = self.client.get(
            "/wind/request-udid-manual/",
            REMOTE_ADDR="198.51.100.6",
            HTTP_USER_AGENT="SameClient/1",
        )
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)

        second = self.client.get(
            "/wind/request-udid-manual/",
            REMOTE_ADDR="198.51.100.6",
            HTTP_USER_AGENT="SameClient/1",
        )
        self.assertEqual(second.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertEqual(second.data["error_code"], "DEVICE_FP_RATE_LIMIT_EXCEEDED")
