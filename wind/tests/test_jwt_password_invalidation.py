"""
Cobertura de tests para la invalidación de JWT tras cambio de contraseña
(`wind/services/jwt_invalidation.py`). Hueco de cobertura señalado en
Bajo #26 -- no existía ningún test para `PasswordAwareJWTAuthentication`
ni para `mark_password_changed` antes de este archivo, pese a ser el
mecanismo que cierra la ventana de "un access token robado sigue
funcionando después de cambiar la contraseña".
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework_simplejwt.exceptions import InvalidToken
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from wind.models import UserSecurityProfile
from wind.services.jwt_invalidation import PasswordAwareJWTAuthentication, mark_password_changed

User = get_user_model()


def _token_with_iat(user, when):
    token = AccessToken.for_user(user)
    token["iat"] = int(when.timestamp())
    return token


class PasswordAwareJWTAuthenticationTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="jwtuser", email="jwtuser@example.com", password="Sup3rSecure!"
        )
        self.auth = PasswordAwareJWTAuthentication()

    def test_allows_token_when_user_has_no_security_profile(self):
        # Nunca cambió la contraseña por estos flujos -- no hay fila
        # `UserSecurityProfile` -- no debe aplicarse ninguna restricción.
        token = _token_with_iat(self.user, timezone.now())

        resolved_user = self.auth.get_user(token)

        self.assertEqual(resolved_user.pk, self.user.pk)

    def test_rejects_token_issued_before_password_change(self):
        changed_at = timezone.now()
        UserSecurityProfile.objects.create(user=self.user, password_changed_at=changed_at)
        token = _token_with_iat(self.user, changed_at - timedelta(seconds=30))

        with self.assertRaises(InvalidToken):
            self.auth.get_user(token)

    def test_allows_token_issued_after_password_change(self):
        changed_at = timezone.now()
        UserSecurityProfile.objects.create(user=self.user, password_changed_at=changed_at)
        token = _token_with_iat(self.user, changed_at + timedelta(seconds=30))

        resolved_user = self.auth.get_user(token)

        self.assertEqual(resolved_user.pk, self.user.pk)

    def test_allows_token_when_iat_missing(self):
        # Caso borde defensivo: si por alguna razón el token no trae "iat",
        # no se debe rechazar a ciegas.
        UserSecurityProfile.objects.create(user=self.user, password_changed_at=timezone.now())
        token = AccessToken.for_user(self.user)
        del token.payload["iat"]

        resolved_user = self.auth.get_user(token)

        self.assertEqual(resolved_user.pk, self.user.pk)


class MarkPasswordChangedTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="jwtuser2", email="jwtuser2@example.com", password="Sup3rSecure!"
        )

    def test_creates_or_updates_security_profile_timestamp(self):
        before = timezone.now()

        mark_password_changed(self.user)

        profile = UserSecurityProfile.objects.get(user=self.user)
        self.assertGreaterEqual(profile.password_changed_at, before)

    def test_blacklists_outstanding_refresh_tokens(self):
        refresh = RefreshToken.for_user(self.user)
        outstanding = OutstandingToken.objects.get(jti=refresh["jti"])
        self.assertFalse(BlacklistedToken.objects.filter(token=outstanding).exists())

        mark_password_changed(self.user)

        self.assertTrue(BlacklistedToken.objects.filter(token=outstanding).exists())

    def test_previously_issued_access_token_is_rejected_after_password_change(self):
        """
        Extremo a extremo (sin blacklist, que solo cubre refresh tokens):
        un access token emitido ANTES de `mark_password_changed` debe dejar
        de servir para `PasswordAwareJWTAuthentication`, aunque todavía no
        haya expirado por su cuenta.
        """
        old_token = AccessToken.for_user(self.user)

        mark_password_changed(self.user)

        with self.assertRaises(InvalidToken):
            PasswordAwareJWTAuthentication().get_user(old_token)
