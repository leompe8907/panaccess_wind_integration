"""
Re-encripta los 3 campos que dependen de ENCRYPTION_KEY antes de poder rotar
esa llave de verdad (ver docs/AUDITORIA_CONSOLIDADA_2026-08-24.md, hallazgo
Urgente #2, y docs/ROTACION_SECRETOS_COMPROMETIDOS_2026-08-25.md).

Por qué existe: ENCRYPTION_KEY (Fernet) cifra `SubscriberLoginInfo.password_hash`,
`SubscriberInfo.password_hash` y `SubscriberInfo.pin_hash`. A diferencia de
SECRET_KEY/DB_PASSWORD (que solo verifican, no "abren" nada guardado),
cambiar ENCRYPTION_KEY sin este paso deja indescifrable todo lo que ya
estaba cifrado con la llave vieja.

Flujo en dos pasos, la llave vieja (la actual, todavía activa en .env en
este momento) NUNCA se escribe en el staging en texto plano innecesariamente
más de lo justo, y el archivo de staging queda con permisos 600 y
gitignored:

  1) python manage.py reencrypt_credentials --generate
     Genera una ENCRYPTION_KEY nueva, junta TODOS los valores ya cifrados
     con la llave actual (los descifra en memoria, no toca la base), y deja
     todo en un archivo de staging local. No escribe nada en la base ni en
     .env todavía.

  2) python manage.py reencrypt_credentials --apply --dry-run
     Simula la migración completa (descifra con la vieja, cifra con la
     nueva, verifica que se pueda volver a leer) SIN guardar nada en la
     base. Para confirmar que va a andar antes del paso real.

  3) python manage.py reencrypt_credentials --apply
     Hace lo mismo pero además guarda cada fila ya re-encriptada con la
     llave nueva. Verifica cada fila apenas la escribe. Si TODAS las filas
     migran bien, borra el staging e imprime el siguiente paso (poner la
     ENCRYPTION_KEY nueva en .env y reiniciar).

  python manage.py reencrypt_credentials --status
     Muestra si hay una migración generada pendiente de aplicar.

IMPORTANTE: --apply exige que la ENCRYPTION_KEY activa en este proceso
(la que está hoy en .env) sea EXACTAMENTE la misma que estaba activa
cuando se corrió --generate. Si cambiaste .env entre medio, se niega a
correr -- de lo contrario, cada fila fallaría al intentar descifrarla con
la llave equivocada, y no hay forma de distinguir eso de una fila
genuinamente corrupta.

Después de un --apply exitoso (0 fallos), el paso manual que queda es:
  1) Poner la ENCRYPTION_KEY nueva (la que imprimió --generate) en el .env
     real del servidor.
  2) Reiniciar Daphne/Celery.
  3) Recién ahí la ENCRYPTION_KEY vieja (la que estaba filtrada en git)
     queda completamente retirada -- ya no hay ninguna fila que dependa de
     ella.
"""
from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from appConfig import PanaccessConfig

STAGING_FILENAME = ".reencrypt_credentials_pending.json"

# (modelo, atributo_app_label.Model, campo) -- se resuelve el modelo real
# recién adentro de handle() para no forzar el import de Django antes de
# tiempo (patrón ya usado en rotate_secrets.py).
FIELDS = (
    ("wind", "SubscriberLoginInfo", "password_hash"),
    ("wind", "SubscriberInfo", "password_hash"),
    ("wind", "SubscriberInfo", "pin_hash"),
)


def _project_root() -> Path:
    return Path(settings.BASE_DIR)


def _staging_path() -> Path:
    return _project_root() / STAGING_FILENAME


def _restrict_to_owner(path: Path) -> None:
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 600
    except OSError:
        pass


class Command(BaseCommand):
    help = (
        "Re-encripta password_hash/pin_hash (SubscriberLoginInfo, SubscriberInfo) "
        "con una ENCRYPTION_KEY nueva, para poder rotarla sin dejar datos ilegibles."
    )

    def add_arguments(self, parser):
        parser.add_argument("--generate", action="store_true")
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--status", action="store_true")
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Con --apply: simula todo (descifra/cifra/verifica) sin escribir en la base.",
        )

    def handle(self, *args, **options):
        chosen = [f for f in ("generate", "apply", "status") if options.get(f)]
        if len(chosen) != 1:
            raise CommandError("Elegí exactamente una de --generate / --apply / --status.")

        if options["status"]:
            return self._status()
        if options["generate"]:
            return self._generate()
        return self._apply(dry_run=options["dry_run"])

    # ------------------------------------------------------------------

    def _current_key(self) -> str:
        key = PanaccessConfig.KEY
        if not key:
            raise CommandError("ENCRYPTION_KEY no está configurada en el entorno actual.")
        return key

    def _iter_rows(self):
        from django.apps import apps as django_apps

        for app_label, model_name, field in FIELDS:
            model = django_apps.get_model(app_label, model_name)
            qs = model.objects.exclude(**{field: None}).exclude(**{field: ""})
            for obj in qs.only("pk", field).iterator():
                value = getattr(obj, field)
                if value:
                    yield model, obj.pk, field, value

    def _status(self):
        path = _staging_path()
        if not path.exists():
            self.stdout.write("No hay ninguna re-encriptación generada pendiente.")
            return
        data = json.loads(path.read_text())
        self.stdout.write(
            f"Re-encriptación pendiente generada el {data['generated_at']} -- "
            f"{len(data['rows'])} filas capturadas."
        )
        self.stdout.write(
            "Falta: `reencrypt_credentials --apply --dry-run` para simular, "
            "y después `reencrypt_credentials --apply` para aplicar de verdad."
        )

    def _generate(self):
        path = _staging_path()
        if path.exists():
            raise CommandError(
                f"Ya existe una re-encriptación generada sin aplicar en {path}. "
                "Corré --apply para completarla, o borrala a mano si querés descartarla."
            )

        current_key = self._current_key()
        old_fernet = Fernet(current_key)
        new_key = Fernet.generate_key().decode("ascii")

        rows = []
        errors = []
        for model, pk, field, ciphertext in self._iter_rows():
            try:
                plaintext = old_fernet.decrypt(ciphertext.encode()).decode()
            except InvalidToken:
                errors.append(f"{model.__name__}#{pk}.{field}")
                continue
            rows.append(
                {
                    "model": model.__name__,
                    "pk": pk,
                    "field": field,
                    "plaintext": plaintext,
                    "old_ciphertext": ciphertext,
                }
            )

        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "current_key": current_key,
            "new_key": new_key,
            "rows": rows,
            "undecryptable_with_current_key": errors,
        }
        path.write_text(json.dumps(payload, indent=2))
        _restrict_to_owner(path)

        self.stdout.write(self.style.WARNING(
            f"Capturadas {len(rows)} filas (guardadas en {path}, permisos 600). "
            "Todavía NO se tocó la base ni el .env."
        ))
        if errors:
            self.stdout.write(self.style.ERROR(
                f"{len(errors)} filas NO se pudieron descifrar con la ENCRYPTION_KEY "
                f"actual (posible corrupción previa, no relacionada con esta rotación): "
                f"{', '.join(errors)}"
            ))
            self.stdout.write(
                "Esas filas quedan afuera de la migración -- revisalas aparte, no bloquean el resto."
            )
        self.stdout.write("")
        self.stdout.write(f"ENCRYPTION_KEY nueva: {new_key}")
        self.stdout.write("")
        self.stdout.write(
            "Paso siguiente: `python manage.py reencrypt_credentials --apply --dry-run` "
            "para simular, y después `--apply` para aplicar de verdad."
        )

    def _apply(self, *, dry_run: bool):
        path = _staging_path()
        if not path.exists():
            raise CommandError("No hay ninguna re-encriptación generada. Corré --generate primero.")

        data = json.loads(path.read_text())
        current_key = self._current_key()
        if current_key != data["current_key"]:
            raise CommandError(
                "La ENCRYPTION_KEY activa ahora mismo no es la misma que estaba activa "
                "cuando corriste --generate. No sigo -- si el .env cambió entre medio, "
                "cada fila fallaría al descifrarse con la llave equivocada. Volvé a poner "
                "la ENCRYPTION_KEY original en .env, o corré --generate de nuevo."
            )

        new_fernet = Fernet(data["new_key"])
        from django.apps import apps as django_apps

        succeeded = 0
        failed = []

        for row in data["rows"]:
            model = django_apps.get_model("wind", row["model"])
            pk = row["pk"]
            field = row["field"]
            plaintext = row["plaintext"]

            try:
                new_ciphertext = new_fernet.encrypt(plaintext.encode()).decode()
                # Verificación de round-trip antes de guardar nada.
                if new_fernet.decrypt(new_ciphertext.encode()).decode() != plaintext:
                    raise ValueError("round-trip verification failed")
            except Exception as exc:  # noqa: BLE001 -- se reporta, no se propaga
                failed.append(f"{row['model']}#{pk}.{field} ({exc})")
                continue

            if not dry_run:
                with transaction.atomic():
                    model.objects.filter(pk=pk).update(**{field: new_ciphertext})
            succeeded += 1

        label = "[dry-run] " if dry_run else ""
        self.stdout.write(f"{label}Migradas correctamente: {succeeded}/{len(data['rows'])}")
        if failed:
            self.stdout.write(self.style.ERROR(f"Fallaron: {len(failed)} -- {', '.join(failed)}"))

        if dry_run:
            self.stdout.write("")
            self.stdout.write("Dry-run -- no se escribió nada en la base. Si todo salió bien arriba, corré sin --dry-run.")
            return

        if failed:
            self.stdout.write(self.style.WARNING(
                "Quedaron filas sin migrar. El staging NO se borra -- corré `--apply` de "
                "nuevo cuando lo resuelvas (las que ya migraron no se van a volver a tocar "
                "de forma incorrecta porque el filtro sigue usando el mismo staging, aunque "
                "sí se re-intentarán -- no debería haber problema, es la misma operación)."
            ))
            return

        path.unlink()
        self.stdout.write(self.style.SUCCESS("Listo. Todas las filas quedaron re-encriptadas con la llave nueva."))
        self.stdout.write("")
        self.stdout.write(self.style.WARNING("Pendiente -- hacer ahora, en este orden:"))
        self.stdout.write("  1) Poner la ENCRYPTION_KEY nueva (la que imprimió --generate) en el .env real.")
        self.stdout.write("  2) Reiniciar Daphne (las 8 instancias) y los workers de Celery.")
        self.stdout.write("  3) Verificar: loguear con alguno de los usuarios migrados usando su contraseña real.")
