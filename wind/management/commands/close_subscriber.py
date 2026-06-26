"""
Cierra la cuenta de un suscriptor (desaprovisiona PanAccess + tombstone local).

Ejemplos:
  python manage.py close_subscriber --code 1120743001 --dry-run
  python manage.py close_subscriber --code 1120743001 --reason "Solicitud usuario"
  python manage.py close_subscriber --code 1120743001 --local-only
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from wind.services.subscriber_closure import close_subscriber_account


class Command(BaseCommand):
    help = "Cierra cuenta de abonado (productos → smartcards en PA, tombstone local)."

    def add_arguments(self, parser):
        parser.add_argument("--code", required=True, help="Código del suscriptor en PanAccess.")
        parser.add_argument("--reason", default="", help="Motivo del cierre.")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Simula el cierre sin modificar PanAccess ni la BD.",
        )
        parser.add_argument(
            "--local-only",
            action="store_true",
            help="Solo cierre local (omitir llamadas PanAccess).",
        )

    def handle(self, *args, **options):
        code = (options["code"] or "").strip()
        if not code:
            raise CommandError("--code es obligatorio")

        result = close_subscriber_account(
            code,
            reason=options.get("reason") or "",
            dry_run=bool(options.get("dry_run")),
            skip_panaccess=bool(options.get("local_only")),
        )

        self.stdout.write(json.dumps(result, indent=2, default=str))

        if not result.get("success"):
            raise CommandError(result.get("message") or "No se pudo cerrar la cuenta")
