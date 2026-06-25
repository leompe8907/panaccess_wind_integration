import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from wind.models import SubscriberEmailRegistry, SubscriberLoginInfo
from wind.services.password_reset import (
    GENERIC_FORGOT_MESSAGE,
    build_reset_token,
    confirm_password_reset,
    request_password_reset,
)

User = get_user_model()


class PasswordResetServiceTestCase(APITestCase):
    def setUp(self):
        self.email = "reset.user@example.com"
        self.subscriber_code = "WND0099"
        SubscriberEmailRegistry.objects.create(
            email=self.email,
            subscriber_code=self.subscriber_code,
        )
        SubscriberLoginInfo.objects.create(
            subscriberCode=self.subscriber_code,
            login1=99001,
            login2="resetuser",
        )
        self.login_info = SubscriberLoginInfo.objects.get(subscriberCode=self.subscriber_code)
        self.login_info.set_password("OldPass123!")
        self.login_info.save()

    @patch("wind.tasks.send_password_reset_email_task")
    def test_request_password_reset_registered_email(self, mock_task):
        result = request_password_reset(self.email, "https://example.com/wind/reset-password/")
        self.assertTrue(result["success"])
        self.assertEqual(result["message"], GENERIC_FORGOT_MESSAGE)
        mock_task.delay.assert_called_once()
        args = mock_task.delay.call_args[0]
        self.assertEqual(args[0], self.email)
        self.assertIn("t=", args[1])

    @patch("wind.tasks.send_password_reset_email_task")
    def test_request_password_reset_unregistered_email(self, mock_task):
        result = request_password_reset("unknown@example.com", "https://example.com/wind/reset-password/")
        self.assertTrue(result["success"])
        self.assertEqual(result["message"], GENERIC_FORGOT_MESSAGE)
        mock_task.delay.assert_not_called()

    @patch("wind.services.password_reset.reset_password_in_panaccess")
    @patch("wind.services.password_reset.mark_reset_token_used")
    def test_confirm_password_reset_success(self, mock_mark_used, mock_panaccess_reset):
        token = build_reset_token(self.subscriber_code, self.email)
        new_pass = "NewSecurePass99!"

        result = confirm_password_reset(token, new_pass)

        self.assertTrue(result["success"])
        mock_panaccess_reset.assert_called_once_with(self.subscriber_code, new_pass)
        mock_mark_used.assert_called_once_with(token)

        self.login_info.refresh_from_db()
        self.assertTrue(self.login_info.check_password(new_pass))

    def test_confirm_password_reset_invalid_token(self):
        result = confirm_password_reset("invalid-token", "NewSecurePass99!")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_type"], "InvalidToken")


class PasswordResetAPITestCase(APITestCase):
    def setUp(self):
        self.forgot_url = reverse("password_forgot")
        self.confirm_url = reverse("password_reset_confirm")
        self.email = "api.reset@example.com"
        self.subscriber_code = "WND0100"
        SubscriberEmailRegistry.objects.create(
            email=self.email,
            subscriber_code=self.subscriber_code,
        )

    @patch("wind.tasks.send_password_reset_email_task")
    def test_forgot_api_generic_response(self, mock_task):
        response = self.client.post(
            self.forgot_url,
            data=json.dumps({"email": self.email}),
            content_type="application/json",
            HTTP_HOST="testserver",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["success"])
        self.assertIn("registrado", response.data["message"].lower())
        mock_task.delay.assert_called_once()

    @patch("wind.tasks.send_password_reset_email_task")
    def test_forgot_api_unknown_email_same_response(self, mock_task):
        response = self.client.post(
            self.forgot_url,
            data=json.dumps({"email": "nobody@example.com"}),
            content_type="application/json",
            HTTP_HOST="testserver",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["success"])
        mock_task.delay.assert_not_called()

    @patch("wind.api.password_reset.views.confirm_password_reset")
    def test_reset_confirm_api_success(self, mock_confirm):
        mock_confirm.return_value = {
            "success": True,
            "message": "Contraseña actualizada correctamente. Ya puedes iniciar sesión.",
        }
        response = self.client.post(
            self.confirm_url,
            data=json.dumps(
                {
                    "token": "signed-token",
                    "newPass": "NewSecure99!",
                    "confirmPass": "NewSecure99!",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["success"])

    def test_reset_confirm_password_mismatch(self):
        response = self.client.post(
            self.confirm_url,
            data=json.dumps(
                {
                    "token": "signed-token",
                    "newPass": "NewSecure99!",
                    "confirmPass": "Different99!",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("confirmPass", response.data["errors"])
