"""
Rotación de un solo uso de los secretos que quedaron expuestos en el
historial de git: SECRET_KEY, ENCRYPTION_KEY, DB_PASSWORD (ver
docs/AUDITORIA_CONSOLIDADA_2026-08-24.md, hallazgo Urgente #2, y
docs/ROTACION_SECRETOS_COMPROMETIDOS_2026-08-25.md).

No es una tarea periódica -- se corre una vez para resolver la
vulnerabilidad puntual (los tres valores actuales son idénticos a los que
quedaron committeados en claro hasta el commit bc6b9ff).

Flujo en dos pasos. Nunca ejecuta el ALTER USER de Postgres por sí mismo
-- eso requiere permisos de superusuario que este proceso no tiene ni
debería tener, así que ese paso queda a mano, supervisado.

  1) python manage.py rotate_secrets --generate
     Genera los 3 valores nuevos, los deja en un archivo de staging local
     (permisos 600, ignorado por git) y los muestra en pantalla junto con
     el SQL exacto para correr en Postgres.

  2) (el operador corre ese SQL en psql, a mano, y confirma que funcionó)

  3) python manage.py rotate_secrets --apply --db-password-already-changed
     Hace backup del .env actual (.env.bak.<timestamp>), escribe los 3
     valores nuevos en el .env real, borra el staging, e imprime el
     checklist de qué reiniciar.

  python manage.py rotate_secrets --status
     Muestra si hay una rotación generada pendiente de aplicar.

Ejemplo completo:
  python manage.py rotate_secrets --generate
  # ... correr el ALTER USER que imprime ...
  python manage.py rotate_secrets --apply --db-password-already-changed
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import stat
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.core.management.utils import get_random_secret_key

from appConfig import DatabaseConfig

STAGING_FILENAME = ".secrets_rotation_pending.json"
ENV_FILENAME = ".env"
ROTATED_KEYS = ("SECRET_KEY", "ENCRYPTION_KEY", "DB_PASSWORD")


def _project_root() -> Path:
    return Path(settings.BASE_DIR)


def _staging_path() -> Path:
    return _project_root() / STAGING_FILENAME


def _env_path() -> Path:
    return _project_root() / ENV_FILENAME


def _generate_db_password(length: int = 32) -> str:
    # Solo alfanumérico: nada de comillas/%/$ que compliquen el escape en
    # el SQL o en el parseo de .env (el password actual, "4b%N#9zX$2wL",
    # ya mostró que esos caracteres dan dolores de cabeza).
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _restrict_to_owner(path: Path) -> None:
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 600
    except OSError:
        pass  # best-effort (p. ej. filesystems que no soportan chmod)


class Command(BaseCommand):
    help = (
        "Rotación de un solo uso de SECRET_KEY/ENCRYPTION_KEY/DB_PASSWORD "
        "tras confirmarse que quedaron expuestos en el historial de git."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--generate", action="store_true",
            help="Genera los 3 valores nuevos y los deja en staging (no toca .env).",
        )
        parser.add_argument(
            "--apply", action="store_true",
            help="Aplica el staging al .env real (requiere --generate previo).",
        )
        parser.add_argument(
            "--status", action="store_true",
            help="Muestra si hay una rotación generada pendiente de aplicar.",
        )
        parser.add_argument(
            "--db-password-already-changed", action="store_true",
            help="Confirma que ya corriste el ALTER USER en Postgres -- obligatorio para --apply.",
        )

    def handle(self, *args, **options):
        chosen = [f for f in ("generate", "apply", "status") if options.get(f)]
        if len(chosen) != 1:
            raise CommandError("Elegí exactamente una de --generate / --apply / --status. Ver --help.")

        if options["status"]:
            return self._status()
        if options["generate"]:
            return self._generate()
        return self._apply(options)

    # ------------------------------------------------------------------

    def _status(self):
        path = _staging_path()
        if not path.exists():
            self.stdout.write("No hay ninguna rotación generada pendiente.")
            return
        data = json.loads(path.read_text())
        self.stdout.write(f"Rotación pendiente generada el {data['generated_at']}.")
        self.stdout.write(
            "Falta: correr el ALTER USER en Postgres (si todavía no se hizo) "
            "y después `rotate_secrets --apply --db-password-already-changed`."
        )

    def _generate(self):
        path = _staging_path()
        if path.exists():
            raise CommandError(
                f"Ya existe una rotación generada sin aplicar en {path}. "
                "Corré --apply para completarla, o borrá el archivo a mano si quieres descartarla y generar otra."
            )

        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "secret_key": get_random_secret_key(),
            "encryption_key": Fernet.generate_key().decode("ascii"),
            "db_password": _generate_db_password(),
        }
        path.write_text(json.dumps(payload, indent=2))
        _restrict_to_owner(path)

        db_user = DatabaseConfig.USER or "<DB_USER>"
        sql = f"ALTER USER \"{db_user}\" WITH PASSWORD '{payload['db_password']}';"

        self.stdout.write(self.style.WARNING(
            f"Valores nuevos generados y guardados en {path} (permisos 600). "
            "Todavía NO se escribió nada en .env."
        ))
        self.stdout.write("")
        self.stdout.write(f"SECRET_KEY nuevo:      {payload['secret_key']}")
        self.stdout.write(f"ENCRYPTION_KEY nuevo:  {payload['encryption_key']}")
        self.stdout.write(f"DB_PASSWORD nuevo:     {payload['db_password']}")
        self.stdout.write("")
        self.stdout.write(self.style.WARNING("Paso siguiente -- corré esto en psql, contra la base real:"))
        self.stdout.write(f"  {sql}")
        self.stdout.write("")
        self.stdout.write(
            "Cuando el ALTER USER haya funcionado, corré:\n"
            "  python manage.py rotate_secrets --apply --db-password-already-changed"
        )

    def _apply(self, options):
        path = _staging_path()
        if not path.exists():
            raise CommandError("No hay ninguna rotación generada. Corré --generate primero.")

        if not options["db_password_already_changed"]:
            raise CommandError(
                "Falta --db-password-already-changed. Confirmá primero que ya corriste el "
                "ALTER USER en Postgres con el password que mostró --generate -- si el .env se "
                "actualiza sin que Postgres tenga el password nuevo, la app se queda sin poder "
                "conectarse a la base de datos."
            )

        data = json.loads(path.read_text())
        env_path = _env_path()
        if not env_path.exists():
            raise CommandError(f"No encontré {env_path} -- ¿estás corriendo esto en el server correcto?")

        backup_path = env_path.with_name(
            f".env.bak.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        )
        shutil.copy2(env_path, backup_path)
        _restrict_to_owner(backup_path)

        replacements = {
            "SECRET_KEY": data["secret_key"],
            "ENCRYPTION_KEY": data["encryption_key"],
            "DB_PASSWORD": data["db_password"],
        }
        applied = set()
        new_lines = []
        for line in env_path.read_text().splitlines(keepends=True):
            stripped = line.strip()
            matched_key = next(
                (k for k in replacements if stripped.startswith(f"{k}=") or stripped.startswith(f"{k} =")),
                None,
            )
            if matched_key:
                new_lines.append(f"{matched_key}={replacements[matched_key]}\n")
                applied.add(matched_key)
            else:
                new_lines.append(line)

        missing = [k for k in ROTATED_KEYS if k not in applied]
        for key in missing:
            new_lines.append(f"{key}={replacements[key]}\n")

        env_path.write_text("".join(new_lines))
        path.unlink()

        self.stdout.write(self.style.SUCCESS(f"Listo. Backup del .env anterior en {backup_path}."))
        if missing:
            self.stdout.write(self.style.WARNING(
                f"Nota: {', '.join(missing)} no existía como línea en el .env anterior -- se agregó al final."
            ))
        self.stdout.write("")
        self.stdout.write(self.style.WARNING("Pendiente -- hacer ahora, en este orden:"))
        self.stdout.write("  1) Reiniciar Daphne (las 8 instancias -- deploy/manage_daphne.sh).")
        self.stdout.write("  2) Reiniciar los workers de Celery.")
        self.stdout.write(
            "  3) Avisar al equipo: SECRET_KEY nuevo invalida todo JWT/cookie ya emitido -- "
            "todos los usuarios (web y apps) van a tener que volver a loguearse."
        )
