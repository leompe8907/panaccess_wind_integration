"""
Cobertura de tests para `applogs` (logs de diagnóstico para desarrolladores)
-- ver docs/LOGS_DIAGNOSTICO_2026-09-01.md. No es telemetría de negocio ni
auditoría de seguridad (ver `applogs/apps.py`).
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import AccessToken

from applogs.models import LogEvent, LogIssue
from applogs.services import compute_fingerprint, record_log_event
from applogs.tasks import purge_old_log_events_task
from wind.models import SubscriberEmailRegistry

User = get_user_model()

LOGS_URL = "/api/v1/logs/"
TEST_API_KEY = "test-ingest-key-123"


def _payload(**overrides):
    data = {
        "platform": LogIssue.PLATFORM_WEB,
        "level": LogIssue.LEVEL_ERROR,
        "message": "TypeError: cannot read properties of undefined",
        "stack": "TypeError: cannot read properties of undefined\n  at PlayerHud.jsx:42",
        "breadcrumbs": [{"category": "nav", "message": "abrió BouquetPage"}],
        "appVersion": "1.2.3",
        "deviceType": "web",
    }
    data.update(overrides)
    return data


@override_settings()
class LogIngestViewTestCase(APITestCase):
    def setUp(self):
        patcher = patch("applogs.views.AppLogsConfig.INGEST_API_KEY", TEST_API_KEY)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_ingest_creates_issue_and_event(self):
        response = self.client.post(
            LOGS_URL, _payload(), format="json", HTTP_X_APP_LOG_KEY=TEST_API_KEY
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(LogIssue.objects.count(), 1)
        self.assertEqual(LogEvent.objects.count(), 1)
        issue = LogIssue.objects.get()
        self.assertEqual(issue.occurrence_count, 1)
        self.assertEqual(issue.platform, LogIssue.PLATFORM_WEB)
        event = LogEvent.objects.get()
        self.assertEqual(event.app_version, "1.2.3")
        self.assertEqual(event.breadcrumbs, [{"category": "nav", "message": "abrió BouquetPage"}])

    def test_ingest_rejects_missing_api_key(self):
        response = self.client.post(LOGS_URL, _payload(), format="json")

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(LogIssue.objects.count(), 0)

    def test_ingest_rejects_wrong_api_key(self):
        response = self.client.post(
            LOGS_URL, _payload(), format="json", HTTP_X_APP_LOG_KEY="algo-incorrecto"
        )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_ingest_groups_repeated_errors_into_same_issue(self):
        for _ in range(3):
            self.client.post(LOGS_URL, _payload(), format="json", HTTP_X_APP_LOG_KEY=TEST_API_KEY)

        self.assertEqual(LogIssue.objects.count(), 1)
        self.assertEqual(LogEvent.objects.count(), 3)
        issue = LogIssue.objects.get()
        self.assertEqual(issue.occurrence_count, 3)

    def test_ingest_different_platform_creates_separate_issue(self):
        self.client.post(
            LOGS_URL, _payload(platform=LogIssue.PLATFORM_WEB), format="json", HTTP_X_APP_LOG_KEY=TEST_API_KEY
        )
        self.client.post(
            LOGS_URL, _payload(platform=LogIssue.PLATFORM_IOS), format="json", HTTP_X_APP_LOG_KEY=TEST_API_KEY
        )

        self.assertEqual(LogIssue.objects.count(), 2)

    def test_ingest_validation_rejects_missing_message(self):
        payload = _payload()
        del payload["message"]

        response = self.client.post(LOGS_URL, payload, format="json", HTTP_X_APP_LOG_KEY=TEST_API_KEY)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("message", response.data["errors"])

    def test_ingest_validation_rejects_invalid_platform(self):
        response = self.client.post(
            LOGS_URL, _payload(platform="not-a-real-platform"), format="json", HTTP_X_APP_LOG_KEY=TEST_API_KEY
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("platform", response.data["errors"])

    def test_ingest_validation_rejects_too_many_breadcrumbs(self):
        payload = _payload(breadcrumbs=[{"i": i} for i in range(101)])

        response = self.client.post(LOGS_URL, payload, format="json", HTTP_X_APP_LOG_KEY=TEST_API_KEY)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("breadcrumbs", response.data["errors"])

    def test_ingest_works_without_jwt_anonymous(self):
        """Un crash antes del login debe poder reportarse igual (ver vista)."""
        response = self.client.post(LOGS_URL, _payload(), format="json", HTTP_X_APP_LOG_KEY=TEST_API_KEY)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        event = LogEvent.objects.get()
        self.assertEqual(event.subscriber_code, "")

    def test_ingest_resolves_subscriber_code_from_valid_jwt(self):
        user = User.objects.create_user(username="loguser", email="loguser@example.com", password="Sup3rSecure!")
        SubscriberEmailRegistry.objects.create(email=user.email, subscriber_code="WNDLOG1")
        token = AccessToken.for_user(user)

        response = self.client.post(
            LOGS_URL,
            _payload(),
            format="json",
            HTTP_X_APP_LOG_KEY=TEST_API_KEY,
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        event = LogEvent.objects.get()
        self.assertEqual(event.subscriber_code, "WNDLOG1")

    def test_ingest_ignores_invalid_jwt_instead_of_rejecting(self):
        """Un token vencido/inválido no debe tumbar el reporte -- sigue anónimo."""
        response = self.client.post(
            LOGS_URL,
            _payload(),
            format="json",
            HTTP_X_APP_LOG_KEY=TEST_API_KEY,
            HTTP_AUTHORIZATION="Bearer esto-no-es-un-jwt-valido",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        event = LogEvent.objects.get()
        self.assertEqual(event.subscriber_code, "")


class RecordLogEventServiceTestCase(TestCase):
    def test_compute_fingerprint_is_stable_for_same_input(self):
        f1 = compute_fingerprint(platform="web", level="error", message="X", stack="Y\nZ")
        f2 = compute_fingerprint(platform="web", level="error", message="X", stack="Y\nZ")
        self.assertEqual(f1, f2)

    def test_compute_fingerprint_differs_by_platform(self):
        f1 = compute_fingerprint(platform="web", level="error", message="X")
        f2 = compute_fingerprint(platform="ios", level="error", message="X")
        self.assertNotEqual(f1, f2)

    def test_record_log_event_backend_platform(self):
        event = record_log_event(platform=LogIssue.PLATFORM_BACKEND, level=LogIssue.LEVEL_ERROR, message="boom")

        self.assertEqual(event.issue.platform, LogIssue.PLATFORM_BACKEND)
        self.assertEqual(event.issue.occurrence_count, 1)

    @patch("applogs.services.AppLogsConfig.ALERTS_ENABLED", True)
    @patch("applogs.services.AppLogsConfig.alert_recipients", return_value=["dev@example.com"])
    def test_new_issue_triggers_alert_email(self, _mock_recipients):
        record_log_event(platform=LogIssue.PLATFORM_WEB, message="algo se rompió")

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("nuevo", mail.outbox[0].subject)
        issue = LogIssue.objects.get()
        self.assertIsNotNone(issue.last_alerted_at)

    @patch("applogs.services.AppLogsConfig.ALERTS_ENABLED", True)
    @patch("applogs.services.AppLogsConfig.alert_recipients", return_value=["dev@example.com"])
    def test_repeat_occurrence_does_not_realert_within_cooldown(self, _mock_recipients):
        record_log_event(platform=LogIssue.PLATFORM_WEB, message="algo se rompió")
        mail.outbox.clear()

        record_log_event(platform=LogIssue.PLATFORM_WEB, message="algo se rompió")

        self.assertEqual(len(mail.outbox), 0)  # dentro del cooldown, y no es múltiplo de ALERT_SPIKE_EVERY

    @patch("applogs.services.AppLogsConfig.ALERTS_ENABLED", False)
    def test_no_alert_when_alerts_disabled(self):
        record_log_event(platform=LogIssue.PLATFORM_WEB, message="algo se rompió")

        self.assertEqual(len(mail.outbox), 0)

    def test_no_alert_when_no_recipients_configured(self):
        with patch("applogs.services.AppLogsConfig.ALERTS_ENABLED", True), patch(
            "applogs.services.AppLogsConfig.alert_recipients", return_value=[]
        ):
            record_log_event(platform=LogIssue.PLATFORM_WEB, message="algo se rompió")

        self.assertEqual(len(mail.outbox), 0)


class PurgeOldLogEventsTaskTestCase(TestCase):
    def test_purge_deletes_only_events_older_than_retention(self):
        issue = LogIssue.objects.create(fingerprint="f1", platform=LogIssue.PLATFORM_WEB, message="x")
        old_event = LogEvent.objects.create(issue=issue)
        recent_event = LogEvent.objects.create(issue=issue)
        LogEvent.objects.filter(pk=old_event.pk).update(created_at=timezone.now() - timezone.timedelta(days=200))

        with patch("applogs.tasks.AppLogsConfig.RETENTION_ENABLED", True), patch(
            "applogs.tasks.AppLogsConfig.RETENTION_DAYS", 90
        ):
            result = purge_old_log_events_task()

        self.assertEqual(result["deleted"], 1)
        self.assertFalse(LogEvent.objects.filter(pk=old_event.pk).exists())
        self.assertTrue(LogEvent.objects.filter(pk=recent_event.pk).exists())
        # El issue agregado nunca se borra en esta tarea.
        self.assertTrue(LogIssue.objects.filter(pk=issue.pk).exists())

    def test_purge_skips_when_retention_disabled(self):
        with patch("applogs.tasks.AppLogsConfig.RETENTION_ENABLED", False):
            result = purge_old_log_events_task()

        self.assertTrue(result["skipped"])
