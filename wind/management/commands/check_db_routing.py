"""
Verifica si el enrutamiento primaria/replica (wind.db_router.PrimaryReplicaRouter)
esta realmente activo en el entorno actual: DATABASES['replica'] solo se agrega
si DB_REPLICA_HOST esta seteado (ver panaccess_wind_integration/settings.py),
asi que en cualquier entorno donde esa variable falte, TODO el trafico -- lectura
y escritura -- cae sobre la BD primaria sin que nada lo señale.

Ejemplo:
  python manage.py check_db_routing
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connections
from django.db.utils import OperationalError


class Command(BaseCommand):
    help = "Diagnostica si el enrutamiento de lecturas a la BD replica esta activo."

    def handle(self, *args, **options):
        routers = list(getattr(settings, "DATABASE_ROUTERS", []))
        has_router = "wind.db_router.PrimaryReplicaRouter" in routers
        self.stdout.write(
            f"DATABASE_ROUTERS: {routers or '(vacio)'} "
            f"-> PrimaryReplicaRouter {'activo' if has_router else 'NO activo'}"
        )

        databases = settings.DATABASES
        has_replica_alias = "replica" in databases
        self.stdout.write(f"Alias 'replica' en DATABASES: {'si' if has_replica_alias else 'NO'}")

        if not has_router or not has_replica_alias:
            self.stdout.write(self.style.WARNING(
                "El enrutamiento de lecturas a replica NO esta activo en este entorno: "
                "todo el trafico de lectura y escritura va a 'default'. Si se esperaba "
                "descargar lecturas a una replica de solo lectura, revisar que "
                "DB_REPLICA_HOST (y DB_REPLICA_PORT) esten seteados en este entorno."
            ))
            return

        for alias in ("default", "replica"):
            conn_max_age = databases.get(alias, {}).get("CONN_MAX_AGE")
            self.stdout.write(f"[{alias}] CONN_MAX_AGE = {conn_max_age}")
            try:
                with connections[alias].cursor() as cursor:
                    cursor.execute("SELECT 1")
                self.stdout.write(self.style.SUCCESS(f"[{alias}] conexion OK"))
            except OperationalError as exc:
                self.stdout.write(self.style.ERROR(f"[{alias}] no se pudo conectar: {exc}"))
                return

        self.stdout.write(self.style.SUCCESS(
            "Enrutamiento primaria/replica activo: las lecturas normales van a "
            "'replica', salvo las envueltas en wind.db_router.use_primary_for_reads()."
        ))
