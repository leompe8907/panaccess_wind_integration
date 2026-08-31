"""
Cobertura de tests para `DeviceSession` ("dispositivos vinculados") y su
tarea de expiración por inactividad (Bajo #28 de la auditoría). Hueco de
cobertura señalado en Bajo #26 -- no existía ningún test para este modelo
ni para `expire_idle_device_sessions_task` antes de este archivo.
"""
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from wind.models import DeviceSession
from wind.tasks import expire_idle_device_sessions_task


def _set_last_seen(device_session, when):
    """
    `DeviceSession.last_seen_at` es `auto_now=True` -- un `.save()` normal
    siempre lo pisa con la hora actual. Para simular inactividad hay que
    esquivar `save()` y actualizar la columna directo vía queryset.
    """
    DeviceSession.objects.filter(pk=device_session.pk).update(last_seen_at=when)


class DeviceSessionModelTestCase(TestCase):
    def test_save_generates_device_token_when_missing(self):
        session = DeviceSession.objects.create(subscriber_code="CODE1")
        self.assertTrue(session.device_token)
        self.assertEqual(len(session.device_token), 43)  # secrets.token_urlsafe(32)

    def test_revoke_sets_status_reason_and_timestamp(self):
        session = DeviceSession.objects.create(subscriber_code="CODE1", status="active")

        session.revoke(reason="revoked_by_subscriber")

        session.refresh_from_db()
        self.assertEqual(session.status, "revoked")
        self.assertEqual(session.revoked_reason, "revoked_by_subscriber")
        self.assertIsNotNone(session.revoked_at)


class ExpireIdleDeviceSessionsTaskTestCase(TestCase):
    def setUp(self):
        self.enabled_patcher = patch(
            "appConfig.CeleryConfig.DEVICE_SESSION_IDLE_EXPIRY_ENABLED", True
        )
        self.days_patcher = patch(
            "appConfig.CeleryConfig.DEVICE_SESSION_IDLE_EXPIRY_DAYS", 183
        )
        self.enabled_patcher.start()
        self.days_patcher.start()
        self.addCleanup(self.enabled_patcher.stop)
        self.addCleanup(self.days_patcher.stop)

    def test_revokes_only_stale_active_sessions(self):
        stale = DeviceSession.objects.create(subscriber_code="STALE1", status="active")
        _set_last_seen(stale, timezone.now() - timezone.timedelta(days=200))

        fresh = DeviceSession.objects.create(subscriber_code="FRESH1", status="active")
        # `auto_now` ya deja `last_seen_at` en "ahora" al crearlo -- sin tocar nada, ya está "reciente".

        already_revoked = DeviceSession.objects.create(subscriber_code="OLDREV1", status="revoked")
        _set_last_seen(already_revoked, timezone.now() - timezone.timedelta(days=300))

        result = expire_idle_device_sessions_task.run()

        self.assertTrue(result["success"])
        self.assertEqual(result["revoked"], 1)

        stale.refresh_from_db()
        fresh.refresh_from_db()
        already_revoked.refresh_from_db()

        self.assertEqual(stale.status, "revoked")
        self.assertEqual(stale.revoked_reason, "idle_timeout")
        self.assertEqual(fresh.status, "active")
        # No se debe recontar ni sobreescribir una revocación ya existente con otro motivo.
        self.assertEqual(already_revoked.revoked_reason, None)

    def test_session_exactly_at_threshold_is_not_touched(self):
        boundary = DeviceSession.objects.create(subscriber_code="BOUND1", status="active")
        # Un poco *menos* de 183 días de inactividad -- no debe expirar todavía.
        _set_last_seen(boundary, timezone.now() - timezone.timedelta(days=182, hours=23))

        result = expire_idle_device_sessions_task.run()

        boundary.refresh_from_db()
        self.assertEqual(result["revoked"], 0)
        self.assertEqual(boundary.status, "active")

    def test_noop_when_feature_disabled(self):
        stale = DeviceSession.objects.create(subscriber_code="STALE2", status="active")
        _set_last_seen(stale, timezone.now() - timezone.timedelta(days=400))

        with patch("appConfig.CeleryConfig.DEVICE_SESSION_IDLE_EXPIRY_ENABLED", False):
            result = expire_idle_device_sessions_task.run()

        self.assertTrue(result.get("skipped"))
        stale.refresh_from_db()
        self.assertEqual(stale.status, "active")
