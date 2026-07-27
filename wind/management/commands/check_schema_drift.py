"""
Compara el esquema real de la BD contra lo que los modelos de una app esperan
(columnas de cada campo, indices declarados en Meta.indexes), para detectar
drift entre entornos.

Motivacion: docs/restaurar_tablas.py existe porque columnas e indices de
wind_subscriberemailregistry y wind_subscriberdocumentregistry desaparecieron
de Postgres sin que ninguna migracion lo reflejara -- este comando permite
detectar ese tipo de drift proactivamente en cualquier entorno, en vez de
descubrirlo por un error en produccion.

Ejemplos:
  python manage.py check_schema_drift
  python manage.py check_schema_drift --database replica
  python manage.py check_schema_drift --app wind
"""
from __future__ import annotations

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db import connections


class Command(BaseCommand):
    help = "Detecta columnas o indices que el modelo espera pero no existen en la BD."

    def add_arguments(self, parser):
        parser.add_argument(
            "--database", default="default",
            help="Alias de BD a inspeccionar (default: 'default').",
        )
        parser.add_argument(
            "--app", default="wind",
            help="App label a revisar (default: 'wind').",
        )

    def handle(self, *args, **options):
        alias = options["database"]
        app_label = options["app"]

        try:
            connection = connections[alias]
        except Exception as exc:
            raise CommandError(f"Alias de BD desconocido '{alias}': {exc}")

        try:
            app_config = apps.get_app_config(app_label)
        except LookupError as exc:
            raise CommandError(str(exc))

        problems = []
        with connection.cursor() as cursor:
            existing_tables = set(connection.introspection.table_names(cursor))

            for model in app_config.get_models():
                table = model._meta.db_table
                if table not in existing_tables:
                    problems.append(f"[{table}] la tabla no existe en la BD '{alias}'.")
                    continue

                description = connection.introspection.get_table_description(cursor, table)
                actual_columns = {col.name for col in description}
                expected_columns = {field.column for field in model._meta.local_fields}
                for col in sorted(expected_columns - actual_columns):
                    problems.append(
                        f"[{table}] falta la columna '{col}' (existe en el modelo, no en la BD)."
                    )

                try:
                    constraints = connection.introspection.get_constraints(cursor, table)
                except NotImplementedError:
                    constraints = {}
                existing_index_names = set(constraints.keys())
                for index in model._meta.indexes:
                    if index.name and index.name not in existing_index_names:
                        problems.append(
                            f"[{table}] falta el indice '{index.name}' declarado en Meta.indexes."
                        )

        if not problems:
            self.stdout.write(self.style.SUCCESS(
                f"Sin drift detectado entre los modelos de '{app_label}' y la BD '{alias}'."
            ))
            return

        self.stdout.write(self.style.WARNING(
            f"Se detectaron {len(problems)} diferencia(s) entre los modelos de "
            f"'{app_label}' y la BD '{alias}':"
        ))
        for problem in problems:
            self.stdout.write(f"  - {problem}")
