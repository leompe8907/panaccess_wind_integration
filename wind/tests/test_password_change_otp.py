"""
Tests de "cambiar contraseña con código OTP" (2026-09-14) -- ver
docs/CAMBIO_CONTRASENA_OTP_2026-09-14.md.

Cubre el servicio (wind/services/password_change_otp.py) y los dos
endpoints nuevos (wind/api/profile/views.py), que coexisten con el flujo
de `oldPass` sin reemplazarlo.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from wind.models import PasswordChangeOtp, SubscriberEmailRegistry, SubscriberLoginInfo
from wind.services.password_change_otp import (
    check_password_change_otp,
    consume_password_change_otp,
    generate_otp_code,
    mask_email,
    request_password_change_otp,
)

User = get_user_model()


class MaskEmailTestCase(TestCase):
    def test_masks_local_part_keeping_domain(self):
        self.assertEqual(mask_email("lilian@gmail.com"), "lil***@gmail.com")
        self.assertEqual(mask_email("ab@gmail.com"), "a***@gmail.com")
        self.assertEqual(mask_email(""), "")
        self.assertEqual(mask_email("not-an-email"), "")


class GenerateOtpCodeTestCase(TestCase):
    def test_always_six_digits(self):
        for _ in range(20):
            code = generate_otp_code()
            self.assertEqual(len(code), 6)
            self.assertTrue(code.isdigit())


class PasswordChangeOtpServiceTestCase(TestCase):
    def setUp(self):
        self.subscriber_code = "WNDOTP001"
        self.email = "otp.user@example.com"
        SubscriberLoginInfo.objects.create(
            subscriberCode=self.subscriber_code,
            login1=88001,
            login2="otpuser",
        )
        self.login_info = SubscriberLoginInfo.objects.get(subscriberCode=self.subscriber_code)
        self.login_info.set_password("OldPass123!")
        self.login_info.save()

    @patch("wind.services.password_change_otp_email.enqueue_password_change_otp_email")
    def test_request_generates_and_enqueues_code(self, mock_enqueue):
        result = request_password_change_otp(subscriber_code=self.subscriber_code, email=self.email)

        self.assertTrue(result["success"], result)
        self.assertEqual(result["masked_email"], mask_email(self.email))
        mock_enqueue.assert_called_once()
        kwargs = mock_enqueue.call_args.kwargs
        self.assertEqual(kwargs["email"], self.email)
        self.assertEqual(kwargs["subscriber_code"], self.subscriber_code)
        self.assertEqual(len(kwargs["code"]), 6)

        self.assertEqual(
            PasswordChangeOtp.objects.filter(subscriber_code=self.subscriber_code, consumed_at__isnull=True).count(),
            1,
        )

    def test_request_without_email_fails(self):
        result = request_password_change_otp(subscriber_code=self.subscriber_code, email="")
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "no_email")

    @patch("wind.services.password_change_otp_email.enqueue_password_change_otp_email")
    def test_request_cooldown_blocks_immediate_resend(self, mock_enqueue):
        request_password_change_otp(subscriber_code=self.subscriber_code, email=self.email)
        result = request_password_change_otp(subscriber_code=self.subscriber_code, email=self.email)

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "otp_cooldown")
        mock_enqueue.assert_called_once()  # el segundo pedido no llegó a encolar nada

    @patch("wind.services.password_change_otp_email.enqueue_password_change_otp_email")
    def test_new_request_invalidates_previous_code(self, mock_enqueue):
        request_password_change_otp(subscriber_code=self.subscriber_code, email=self.email)
        first_code = mock_enqueue.call_args.kwargs["code"]

        # Salta el cooldown manipulando directamente created_at del registro previo.
        PasswordChangeOtp.objects.filter(subscriber_code=self.subscriber_code).update(
            created_at=timezone.now() - timezone.timedelta(minutes=5)
        )

        request_password_change_otp(subscriber_code=self.subscriber_code, email=self.email)

        check_old = check_password_change_otp(subscriber_code=self.subscriber_code, code=first_code)
        self.assertFalse(check_old["success"])
        self.assertEqual(check_old["code"], "otp_missing_or_expired")

    @patch("wind.services.password_change_otp_email.enqueue_password_change_otp_email")
    def test_check_correct_code_succeeds_without_consuming(self, mock_enqueue):
        request_password_change_otp(subscriber_code=self.subscriber_code, email=self.email)
        code = mock_enqueue.call_args.kwargs["code"]

        result = check_password_change_otp(subscriber_code=self.subscriber_code, code=code)
        self.assertTrue(result["success"], result)
        record = result["record"]
        self.assertIsNone(record.consumed_at)

        # Sigue siendo válido -- no se consume solo por chequearlo.
        result_again = check_password_change_otp(subscriber_code=self.subscriber_code, code=code)
        self.assertTrue(result_again["success"], result_again)

    @patch("wind.services.password_change_otp_email.enqueue_password_change_otp_email")
    def test_check_wrong_code_increments_attempts(self, mock_enqueue):
        request_password_change_otp(subscriber_code=self.subscriber_code, email=self.email)

        result = check_password_change_otp(subscriber_code=self.subscriber_code, code="000000")
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "otp_incorrect")

        record = PasswordChangeOtp.objects.get(subscriber_code=self.subscriber_code)
        self.assertEqual(record.attempts, 1)

    @patch("wind.services.password_change_otp_email.enqueue_password_change_otp_email")
    def test_check_locks_after_max_attempts(self, mock_enqueue):
        request_password_change_otp(subscriber_code=self.subscriber_code, email=self.email)

        for _ in range(5):
            check_password_change_otp(subscriber_code=self.subscriber_code, code="000000")

        result = check_password_change_otp(subscriber_code=self.subscriber_code, code="000000")
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "otp_locked")

    def test_check_without_any_request_fails(self):
        result = check_password_change_otp(subscriber_code="NOBODY", code="123456")
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "otp_missing_or_expired")

    @patch("wind.services.password_change_otp_email.enqueue_password_change_otp_email")
    def test_expired_code_rejected(self, mock_enqueue):
        request_password_change_otp(subscriber_code=self.subscriber_code, email=self.email)
        code = mock_enqueue.call_args.kwargs["code"]

        PasswordChangeOtp.objects.filter(subscriber_code=self.subscriber_code).update(
            expires_at=timezone.now() - timezone.timedelta(minutes=1)
        )

        result = check_password_change_otp(subscriber_code=self.subscriber_code, code=code)
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "otp_missing_or_expired")

    @patch("wind.services.password_change_otp_email.enqueue_password_change_otp_email")
    def test_consume_marks_record_used(self, mock_enqueue):
        request_password_change_otp(subscriber_code=self.subscriber_code, email=self.email)
        code = mock_enqueue.call_args.kwargs["code"]

        check = check_password_change_otp(subscriber_code=self.subscriber_code, code=code)
        consume_password_change_otp(check["record"])

        check_after = check_password_change_otp(subscriber_code=self.subscriber_code, code=code)
        self.assertFalse(check_after["success"])
        self.assertEqual(check_after["code"], "otp_missing_or_expired")


class PasswordChangeOtpEndpointsTestCase(APITestCase):
    REQUEST_URL = "/api/v1/profile/password/otp/request-code/"
    CONFIRM_URL = "/api/v1/profile/password/otp/confirm/"

    def setUp(self):
        self.subscriber_code = "WNDOTPAPI1"
        self.email = "otp.api@example.com"
        SubscriberEmailRegistry.objects.create(email=self.email, subscriber_code=self.subscriber_code)
        SubscriberLoginInfo.objects.create(
            subscriberCode=self.subscriber_code,
            login1=88002,
            login2="otpapiuser",
        )
        self.login_info = SubscriberLoginInfo.objects.get(subscriberCode=self.subscriber_code)
        self.login_info.set_password("OldPass123!")
        self.login_info.save()

        self.user = User.objects.create_user(
            username=self.email, email=self.email, password="OldPass123!"
        )
        self.client.force_authenticate(user=self.user)

    @patch("wind.services.password_change_otp_email.enqueue_password_change_otp_email")
    def test_request_code_success(self, mock_enqueue):
        response = self.client.post(self.REQUEST_URL, {"code": self.subscriber_code}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["success"])
        self.assertIn("masked_email", response.data)
        mock_enqueue.assert_called_once()

    def test_request_code_rejects_other_subscriber(self):
        response = self.client.post(self.REQUEST_URL, {"code": "SOMEONE_ELSE"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @patch("appConfig.FeatureConfig.CHANGE_PASSWORD_OTP_ENABLED", False)
    def test_request_code_disabled_by_flag(self):
        response = self.client.post(self.REQUEST_URL, {"code": self.subscriber_code}, format="json")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @patch("wind.api.profile.views.reset_password_in_panaccess")
    @patch("wind.services.password_change_otp_email.enqueue_password_change_otp_email")
    def test_confirm_success_changes_password_and_revokes_devices(self, mock_enqueue, mock_reset):
        self.client.post(self.REQUEST_URL, {"code": self.subscriber_code}, format="json")
        code = mock_enqueue.call_args.kwargs["code"]

        with patch("wind.api.profile.views.sync_password_locally") as mock_sync:
            response = self.client.post(
                self.CONFIRM_URL,
                {"code": self.subscriber_code, "otpCode": code, "newPass": "BrandNewPass99!"},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(response.data["success"])
        mock_reset.assert_called_once_with(self.subscriber_code, "BrandNewPass99!")
        mock_sync.assert_called_once_with(self.subscriber_code, self.email, "BrandNewPass99!")

        record = PasswordChangeOtp.objects.get(subscriber_code=self.subscriber_code)
        self.assertIsNotNone(record.consumed_at)

    @patch("wind.api.profile.views.reset_password_in_panaccess")
    @patch("wind.services.password_change_otp_email.enqueue_password_change_otp_email")
    def test_confirm_wrong_code_rejected(self, mock_enqueue, mock_reset):
        self.client.post(self.REQUEST_URL, {"code": self.subscriber_code}, format="json")

        response = self.client.post(
            self.CONFIRM_URL,
            {"code": self.subscriber_code, "otpCode": "000000", "newPass": "BrandNewPass99!"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(response.data["success"])
        self.assertEqual(response.data["code"], "otp_incorrect")
        mock_reset.assert_not_called()

    def test_confirm_without_prior_request_rejected(self):
        response = self.client.post(
            self.CONFIRM_URL,
            {"code": self.subscriber_code, "otpCode": "123456", "newPass": "BrandNewPass99!"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["code"], "otp_missing_or_expired")

    @patch("wind.api.profile.views.reset_password_in_panaccess")
    @patch("wind.services.password_change_otp_email.enqueue_password_change_otp_email")
    def test_confirm_panaccess_rejection_keeps_code_usable(self, mock_enqueue, mock_reset):
        """
        Si PanAccess rechaza la nueva contraseña, el código no se consume --
        mismo criterio que confirm_password_reset/mark_reset_token_used
        (ver docstring de profile_password_otp_confirm_view).
        """
        from wind.exceptions import PanAccessAPIError

        mock_reset.side_effect = PanAccessAPIError("Password rejected", status_code=200, error_code=None)

        self.client.post(self.REQUEST_URL, {"code": self.subscriber_code}, format="json")
        code = mock_enqueue.call_args.kwargs["code"]

        response = self.client.post(
            self.CONFIRM_URL,
            {"code": self.subscriber_code, "otpCode": code, "newPass": "Rejected99!"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["code"], "password_rejected_by_panaccess")

        record = PasswordChangeOtp.objects.get(subscriber_code=self.subscriber_code)
        self.assertIsNone(record.consumed_at, "El código debe seguir usable si PanAccess rechazó la contraseña")

    @patch("appConfig.FeatureConfig.CHANGE_PASSWORD_OTP_ENABLED", False)
    def test_confirm_disabled_by_flag(self):
        response = self.client.post(
            self.CONFIRM_URL,
            {"code": self.subscriber_code, "otpCode": "123456", "newPass": "BrandNewPass99!"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
