"""
Cobertura de tests para el flujo de login social (Google/Facebook) --
`wind/adapters.py` (`PanAccessSocialAccountAdapter`) y
`wind/services/social_login_provisioning.py`. Hueco de cobertura
señalado en Bajo #26: no existía ningún test para la verificación de
email por proveedor, la fusión con un usuario local existente, el
bloqueo por `SOCIAL_LOGIN_REQUIRE_EXISTING_SUBSCRIBER`, ni el
auto-registro de suscriptor nuevo.
"""
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.exceptions import ValidationError

from wind.adapters import PanAccessSocialAccountAdapter
from wind.models import ListOfSubscriber, SubscriberEmailRegistry
from wind.services.social_login_provisioning import (
    SocialLoginSubscriberNotFound,
    ensure_subscriber_for_social_email,
)

User = get_user_model()


def _make_sociallogin(email, verified=True, provider="google", via_extra_data=False):
    """
    Doble de prueba mínimo de `allauth.socialaccount.models.SocialLogin` --
    el adaptador solo toca `.user`, `.account.extra_data`,
    `.account.provider` y `.email_addresses`, así que no hace falta
    levantar allauth completo (OAuth real) para probar la lógica propia.
    """
    extra_data = {}
    user = User(email=email, first_name="", last_name="")
    email_addresses = []
    if via_extra_data:
        if verified:
            extra_data["email_verified"] = True
    else:
        email_addresses = [SimpleNamespace(email=email, verified=verified)]

    account = SimpleNamespace(extra_data=extra_data, provider=provider)
    return SimpleNamespace(user=user, account=account, email_addresses=email_addresses)


class PanAccessSocialAccountAdapterTestCase(TestCase):
    def setUp(self):
        self.adapter = PanAccessSocialAccountAdapter()

    @patch("wind.adapters.ensure_subscriber_for_social_email")
    def test_rejects_missing_email(self, mock_ensure):
        sociallogin = _make_sociallogin(email="")

        with self.assertRaises(ValidationError):
            self.adapter.pre_social_login(request=None, sociallogin=sociallogin)

        mock_ensure.assert_not_called()

    @patch("wind.adapters.ensure_subscriber_for_social_email")
    def test_rejects_email_not_verified_by_provider(self, mock_ensure):
        sociallogin = _make_sociallogin(email="user@example.com", verified=False)

        with self.assertRaises(ValidationError):
            self.adapter.pre_social_login(request=None, sociallogin=sociallogin)

        mock_ensure.assert_not_called()

    @patch("wind.adapters.ensure_subscriber_for_social_email", return_value="WND0099")
    def test_accepts_verification_via_extra_data_fallback(self, mock_ensure):
        # Google expone `email_verified` directo en `extra_data`, sin pasar
        # por `email_addresses` -- ver `_is_email_verified_by_provider`.
        sociallogin = _make_sociallogin(email="user@example.com", verified=True, via_extra_data=True)

        self.adapter.pre_social_login(request=None, sociallogin=sociallogin)

        mock_ensure.assert_called_once()

    @patch("wind.adapters.ensure_subscriber_for_social_email", return_value="WND0100")
    def test_passes_real_provider_through_not_a_hardcoded_default(self, mock_ensure):
        # Auditoría: si el provider no se propaga bien, todo suscriptor
        # nuevo cae al prefijo por defecto sin importar si vino de Google o
        # Facebook -- este test fija ese comportamiento.
        sociallogin = _make_sociallogin(email="user@example.com", verified=True, provider="facebook")

        self.adapter.pre_social_login(request=None, sociallogin=sociallogin)

        _, kwargs = mock_ensure.call_args
        self.assertEqual(kwargs["social_provider"], "facebook")

    def test_merges_with_existing_local_user_by_email(self):
        existing = User.objects.create_user(
            username="existing", email="existing@example.com", password="x"
        )
        sociallogin = _make_sociallogin(email="existing@example.com", verified=True)

        with patch("wind.adapters.ensure_subscriber_for_social_email", return_value="WND0101"):
            self.adapter.pre_social_login(request=None, sociallogin=sociallogin)

        self.assertEqual(sociallogin.user.pk, existing.pk)

    @patch(
        "wind.adapters.ensure_subscriber_for_social_email",
        side_effect=SocialLoginSubscriberNotFound("user3@example.com"),
    )
    def test_translates_subscriber_not_found_into_specific_message(self, mock_ensure):
        sociallogin = _make_sociallogin(email="user3@example.com", verified=True)

        with self.assertRaises(ValidationError) as ctx:
            self.adapter.pre_social_login(request=None, sociallogin=sociallogin)

        self.assertIn("SubscriberNotFound", str(ctx.exception.detail))

    @patch("wind.adapters.ensure_subscriber_for_social_email", return_value=None)
    def test_rejects_when_subscriber_code_could_not_be_resolved(self, mock_ensure):
        sociallogin = _make_sociallogin(email="user4@example.com", verified=True)

        with self.assertRaises(ValidationError):
            self.adapter.pre_social_login(request=None, sociallogin=sociallogin)


class EnsureSubscriberForSocialEmailTestCase(TestCase):
    def test_returns_existing_registry_code_without_hitting_panaccess(self):
        SubscriberEmailRegistry.objects.create(email="a@example.com", subscriber_code="WND01")

        with patch(
            "wind.services.social_login_provisioning.create_subscriber_in_panaccess"
        ) as mock_create:
            code = ensure_subscriber_for_social_email("a@example.com")

        self.assertEqual(code, "WND01")
        mock_create.assert_not_called()

    def test_links_registry_from_existing_list_of_subscriber(self):
        ListOfSubscriber.objects.create(
            id="LNK1", code="LNK1", emails="linked@example.com",
            status=ListOfSubscriber.STATUS_ACTIVE,
        )

        code = ensure_subscriber_for_social_email("linked@example.com")

        self.assertEqual(code, "LNK1")
        self.assertTrue(
            SubscriberEmailRegistry.objects.filter(
                email="linked@example.com", subscriber_code="LNK1"
            ).exists()
        )

    @patch("wind.services.social_login_provisioning.FeatureConfig")
    def test_raises_when_require_existing_subscriber_is_enabled(self, mock_feature_config):
        mock_feature_config.SOCIAL_LOGIN_REQUIRE_EXISTING_SUBSCRIBER = True

        with self.assertRaises(SocialLoginSubscriberNotFound):
            ensure_subscriber_for_social_email("nobody@example.com")

    @patch("wind.services.social_login_provisioning.create_subscriber_in_panaccess")
    @patch("wind.services.social_login_provisioning.FeatureConfig")
    def test_auto_creates_subscriber_when_flag_disabled(self, mock_feature_config, mock_create):
        mock_feature_config.SOCIAL_LOGIN_REQUIRE_EXISTING_SUBSCRIBER = False
        mock_create.return_value = {"success": True, "subscriber_code": "NEW1"}

        code = ensure_subscriber_for_social_email("new@example.com", social_provider="google")

        self.assertEqual(code, "NEW1")
        _, kwargs = mock_create.call_args
        self.assertTrue(kwargs["is_social_account"])
        self.assertEqual(kwargs["social_provider"], "google")
