from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from appConfig import EmailConfig
from wind.services.welcome_email import (
    build_welcome_email_context,
    enqueue_welcome_credentials_email,
    render_welcome_email_bodies,
)


class WelcomeEmailContextTestCase(SimpleTestCase):
    def test_display_name_from_first_and_last(self):
        context = build_welcome_email_context(
            first_name="Juan",
            last_name="Pérez",
            email="juan@example.com",
            subscriber_code="AUTO100",
            is_social_account=True,
        )
        self.assertEqual(context["full_name"], "Juan Pérez")
        self.assertEqual(context["username"], "juan@example.com")
        self.assertEqual(context["password_display"], EmailConfig.SOCIAL_PASSWORD_MESSAGE)

    @patch("wind.services.welcome_email.CallGetSubscriberLoginInfo")
    def test_credentials_from_panaccess(self, mock_login_info):
        mock_login_info.return_value = {
            "login2": "juan@example.com",
            "password": "Secr3t!",
        }
        context = build_welcome_email_context(
            first_name="Juan",
            last_name="Pérez",
            email="juan@example.com",
            subscriber_code="AUTO100",
            is_social_account=False,
        )
        self.assertEqual(context["username"], "juan@example.com")
        self.assertEqual(context["password_display"], "Secr3t!")

    @patch("wind.services.welcome_email.CallGetSubscriberLoginInfo")
    def test_username_is_always_email_not_login2(self, mock_login_info):
        mock_login_info.return_value = {
            "login2": "wtl_1@AUTO100",
            "password": "Secr3t!",
        }
        context = build_welcome_email_context(
            first_name="Juan",
            last_name="Pérez",
            email="juan@example.com",
            subscriber_code="AUTO100",
            is_social_account=False,
        )
        self.assertEqual(context["username"], "juan@example.com")
        self.assertNotEqual(context["username"], "wtl_1@AUTO100")

    @patch("wind.services.welcome_email.CallGetSubscriberLoginInfo")
    def test_credentials_fallback_when_panaccess_fails(self, mock_login_info):
        mock_login_info.side_effect = RuntimeError("timeout")
        context = build_welcome_email_context(
            first_name="Juan",
            last_name="Pérez",
            email="juan@example.com",
            subscriber_code="AUTO100",
            is_social_account=False,
        )
        self.assertEqual(context["username"], "juan@example.com")
        self.assertIn("No pudimos cargar", context["password_display"])


class WelcomeEmailRenderTestCase(SimpleTestCase):
    def test_render_includes_credentials_and_store_links(self):
        context = build_welcome_email_context(
            first_name="Ana",
            last_name="Gómez",
            email="ana@example.com",
            subscriber_code="AUTO200",
            is_social_account=True,
        )
        text_body, html_body = render_welcome_email_bodies(context)
        self.assertIn("Ana Gómez", text_body)
        self.assertIn("ana@example.com", text_body)
        self.assertIn(EmailConfig.SOCIAL_PASSWORD_MESSAGE, text_body)
        self.assertIn("Ana Gómez", html_body)
        self.assertIn("ana@example.com", html_body)
        self.assertIn("#33cccc", html_body)

    @patch("wind.tasks.send_welcome_credentials_email_task")
    def test_enqueue_dispatches_celery_task(self, mock_task):
        mock_task.delay = MagicMock()
        enqueue_welcome_credentials_email(
            first_name="Luis",
            last_name="Díaz",
            email="luis@example.com",
            subscriber_code="AUTO300",
            is_social_account=True,
        )
        mock_task.delay.assert_called_once()
        args = mock_task.delay.call_args[0]
        self.assertEqual(args[0], "luis@example.com")
        self.assertEqual(args[1], EmailConfig.WELCOME_SUBJECT)
        self.assertIn("Luis Díaz", args[2])
        self.assertIn("Luis Díaz", args[3])
