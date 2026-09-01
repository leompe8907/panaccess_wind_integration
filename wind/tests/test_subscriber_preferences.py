"""
Cobertura de tests para el endpoint de preferencias sincronizadas
(control parental + favoritos) -- ver
docs/SINCRONIZACION_PREFERENCIAS_2026-08-31.md.
"""
from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from wind.models import SubscriberEmailRegistry, SubscriberPreferences
from wind.services.subscriber_preferences import get_or_migrate_preferences

User = get_user_model()

PREFERENCES_URL = "/api/v1/preferences/"


class PreferencesViewTestCase(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="prefsuser", email="prefsuser@example.com", password="Sup3rSecure!"
        )
        SubscriberEmailRegistry.objects.create(email=self.user.email, subscriber_code="WNDPREFS1")
        self.client.force_authenticate(user=self.user)

    def test_get_creates_default_row_on_first_access(self):
        response = self.client.get(PREFERENCES_URL)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["success"])
        self.assertEqual(response.data["profileKey"], SubscriberPreferences.DEFAULT_PROFILE_KEY)
        self.assertIsNone(response.data["parental"])
        self.assertEqual(response.data["favorites"], [])
        self.assertTrue(
            SubscriberPreferences.objects.filter(
                subscriber_code="WNDPREFS1", profile_key=SubscriberPreferences.DEFAULT_PROFILE_KEY
            ).exists()
        )

    def test_get_rejects_unlinked_user(self):
        other_user = User.objects.create_user(
            username="nolink", email="nolink@example.com", password="Sup3rSecure!"
        )
        self.client.force_authenticate(user=other_user)

        response = self.client.get(PREFERENCES_URL)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_put_updates_parental_and_favorites(self):
        payload = {
            "parental": {"enabled": True, "pinHash": "abc", "pinSalt": "xyz", "blockedChannelIds": ["10"]},
            "favorites": ["101", "202"],
        }

        response = self.client.put(PREFERENCES_URL, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["favorites"], ["101", "202"])
        self.assertEqual(response.data["parental"]["enabled"], True)

    def test_put_partial_update_does_not_clear_other_field(self):
        self.client.put(
            PREFERENCES_URL,
            {"parental": {"enabled": True}, "favorites": ["1"]},
            format="json",
        )

        response = self.client.put(PREFERENCES_URL, {"favorites": ["1", "2"]}, format="json")

        self.assertEqual(response.data["favorites"], ["1", "2"])
        self.assertEqual(response.data["parental"], {"enabled": True})

    def test_get_reads_back_a_put_by_another_device(self):
        self.client.put(PREFERENCES_URL, {"favorites": ["55"]}, format="json")

        response = self.client.get(PREFERENCES_URL)

        self.assertEqual(response.data["favorites"], ["55"])

    def test_profile_key_defaults_when_blank(self):
        response = self.client.put(PREFERENCES_URL, {"profileKey": "  ", "favorites": ["1"]}, format="json")

        self.assertEqual(response.data["profileKey"], SubscriberPreferences.DEFAULT_PROFILE_KEY)

    def test_different_profile_keys_are_isolated(self):
        self.client.put(PREFERENCES_URL, {"favorites": ["1"]}, format="json")
        self.client.put(
            PREFERENCES_URL, {"profileKey": "kid-profile", "favorites": ["999"]}, format="json"
        )

        default_row = SubscriberPreferences.objects.get(
            subscriber_code="WNDPREFS1", profile_key=SubscriberPreferences.DEFAULT_PROFILE_KEY
        )
        # La migración automática solo copia hacia el perfil real nuevo --
        # no debe pisar hacia atrás la fila "default" original.
        self.assertEqual(default_row.favorite_channel_ids, ["1"])

    def test_validate_parental_rejects_non_object(self):
        response = self.client.put(PREFERENCES_URL, {"parental": "not-an-object"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("parental", response.data["errors"])

    def test_validate_favorites_rejects_too_many(self):
        response = self.client.put(
            PREFERENCES_URL, {"favorites": [str(i) for i in range(501)]}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("favorites", response.data["errors"])


class GetOrMigratePreferencesTestCase(APITestCase):
    """
    Migración automática: la primera vez que se usa un perfil real, hereda
    lo que había bajo "default"; cualquier perfil siguiente arranca vacío.
    """

    def test_first_real_profile_inherits_default(self):
        SubscriberPreferences.objects.create(
            subscriber_code="WNDMIG1",
            profile_key=SubscriberPreferences.DEFAULT_PROFILE_KEY,
            parental={"enabled": True, "pinHash": "h", "pinSalt": "s"},
            favorite_channel_ids=["10", "20"],
        )

        migrated = get_or_migrate_preferences("WNDMIG1", "first-real-profile")

        self.assertEqual(migrated.favorite_channel_ids, ["10", "20"])
        self.assertEqual(migrated.parental["enabled"], True)

    def test_second_real_profile_starts_empty(self):
        SubscriberPreferences.objects.create(
            subscriber_code="WNDMIG2",
            profile_key=SubscriberPreferences.DEFAULT_PROFILE_KEY,
            favorite_channel_ids=["10"],
        )
        get_or_migrate_preferences("WNDMIG2", "first-profile")

        second = get_or_migrate_preferences("WNDMIG2", "second-profile")

        self.assertIsNone(second.favorite_channel_ids)
        self.assertIsNone(second.parental)

    def test_no_migration_when_no_default_row_exists(self):
        # Cuenta nueva que arranca directo con perfiles (nunca usó
        # "default") -- no debe fallar ni inventar datos.
        result = get_or_migrate_preferences("WNDMIG3", "some-profile")

        self.assertIsNone(result.favorite_channel_ids)
        self.assertIsNone(result.parental)

    def test_existing_row_is_returned_without_touching_migration_logic(self):
        SubscriberPreferences.objects.create(
            subscriber_code="WNDMIG4", profile_key="already-there", favorite_channel_ids=["7"]
        )

        result = get_or_migrate_preferences("WNDMIG4", "already-there")

        self.assertEqual(result.favorite_channel_ids, ["7"])
