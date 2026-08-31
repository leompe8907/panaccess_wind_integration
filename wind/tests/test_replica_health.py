"""
Verificación del circuit breaker de salud de réplica y del
`connect_timeout` de base de datos (Bajo #30 de la auditoría).
"""
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.test import TestCase

from appConfig import DatabaseConfig
from wind.db_router import (
    PrimaryReplicaRouter,
    is_replica_healthy,
    mark_replica_healthy,
    mark_replica_unhealthy,
    use_primary_for_reads,
)
from wind.tasks import check_replica_health_task


class DatabaseConfigConnectTimeoutTestCase(TestCase):
    def test_default_database_includes_connect_timeout(self):
        db = DatabaseConfig.django_default_database()

        self.assertIn("OPTIONS", db)
        self.assertEqual(db["OPTIONS"]["connect_timeout"], DatabaseConfig.CONNECT_TIMEOUT_SECONDS)

    def test_replica_database_inherits_connect_timeout(self):
        with patch.object(DatabaseConfig, "REPLICA_HOST", "replica.internal"):
            db = DatabaseConfig.django_replica_database()

        self.assertIsNotNone(db)
        self.assertEqual(db["HOST"], "replica.internal")
        self.assertEqual(db["OPTIONS"]["connect_timeout"], DatabaseConfig.CONNECT_TIMEOUT_SECONDS)


class ReplicaHealthCircuitBreakerTestCase(TestCase):
    def tearDown(self):
        mark_replica_healthy()

    def test_healthy_by_default(self):
        self.assertTrue(is_replica_healthy())

    def test_mark_unhealthy_then_healthy_again(self):
        mark_replica_unhealthy(ttl_seconds=60)
        self.assertFalse(is_replica_healthy())

        mark_replica_healthy()
        self.assertTrue(is_replica_healthy())

    def test_unhealthy_mark_expires_on_its_own(self):
        mark_replica_unhealthy(ttl_seconds=60)
        self.assertFalse(is_replica_healthy())
        # Simula el paso del tiempo borrando la key directamente -- no hace
        # falta esperar el TTL real para confirmar que no queda "pegada".
        cache.delete("db_router:replica_unhealthy")
        self.assertTrue(is_replica_healthy())

    def test_router_falls_back_to_default_when_replica_unhealthy(self):
        router = PrimaryReplicaRouter()
        mark_replica_unhealthy(ttl_seconds=60)

        self.assertEqual(router.db_for_read(None), "default")

    def test_router_uses_replica_when_healthy(self):
        router = PrimaryReplicaRouter()

        self.assertEqual(router.db_for_read(None), "replica")

    def test_force_primary_wins_even_if_replica_healthy(self):
        router = PrimaryReplicaRouter()

        with use_primary_for_reads():
            self.assertEqual(router.db_for_read(None), "default")


class CheckReplicaHealthTaskTestCase(TestCase):
    def tearDown(self):
        mark_replica_healthy()

    def test_marks_unhealthy_when_query_fails(self):
        mark_replica_healthy()
        fake_connections = {"replica": MagicMock()}
        fake_connections["replica"].cursor.side_effect = Exception("connection refused")

        with patch("appConfig.CeleryConfig.DB_REPLICA_HEALTHCHECK_ENABLED", True), \
             patch("django.db.connections", fake_connections):
            result = check_replica_health_task.run()

        self.assertFalse(result["healthy"])
        self.assertFalse(is_replica_healthy())

    def test_marks_healthy_when_query_succeeds(self):
        mark_replica_unhealthy(ttl_seconds=60)
        fake_cursor = MagicMock()
        fake_connections = {"replica": MagicMock()}
        fake_connections["replica"].cursor.return_value.__enter__.return_value = fake_cursor

        with patch("appConfig.CeleryConfig.DB_REPLICA_HEALTHCHECK_ENABLED", True), \
             patch("django.db.connections", fake_connections):
            result = check_replica_health_task.run()

        self.assertTrue(result["healthy"])
        self.assertTrue(is_replica_healthy())

    def test_noop_when_healthcheck_disabled(self):
        with patch("appConfig.CeleryConfig.DB_REPLICA_HEALTHCHECK_ENABLED", False):
            result = check_replica_health_task.run()

        self.assertTrue(result.get("skipped"))
