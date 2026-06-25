"""
Prueba del correo de bienvenida sin pasar por el registro.

Ejemplos:
  # Solo guardar HTML local (sin SMTP)
  python manage.py send_welcome_email_test --preview --preview-file /tmp/welcome.html

  # Enviar correo de prueba con datos ficticios
  python manage.py send_welcome_email_test --to tu@email.com

  # Variante cuenta social
  python manage.py send_welcome_email_test --to tu@email.com --social

  # Usar credenciales reales de un suscriptor existente en PanAccess
  python manage.py send_welcome_email_test --to tu@email.com --subscriber-code AUTO12345
"""
from __future__ import annotations

from pathlib import Path

from django.core.mail import send_mail
from django.core.management.base import BaseCommand, CommandError

from appConfig import EmailConfig
from wind.services.welcome_email import (
    build_welcome_email_context,
    render_welcome_email_bodies,
)


class Command(BaseCommand):
    help = "Previsualiza o envía el correo de bienvenida sin registrar un usuario."

    def add_arguments(self, parser):
        parser.add_argument(
            "--to",
            dest="recipient",
            help="Correo destino. Obligatorio salvo con --preview.",
        )
        parser.add_argument(
            "--preview",
            action="store_true",
            help="Genera el HTML/texto sin enviar por SMTP.",
        )
        parser.add_argument(
            "--preview-file",
            dest="preview_file",
            help="Ruta donde guardar el HTML (implica --preview).",
        )
        parser.add_argument(
            "--social",
            action="store_true",
            help="Simula registro con Google/Facebook.",
        )
        parser.add_argument(
            "--first-name",
            default="Juan",
            help="Nombre de prueba (default: Juan).",
        )
        parser.add_argument(
            "--last-name",
            default="Pérez",
            help="Apellido de prueba (default: Pérez).",
        )
        parser.add_argument(
            "--email",
            dest="sample_email",
            help="Email ficticio del suscriptor en el cuerpo (default: --to o juan.perez@ejemplo.com).",
        )
        parser.add_argument(
            "--subscriber-code",
            help="Código PanAccess real para cargar credenciales (ignorado con --social).",
        )
        parser.add_argument(
            "--password",
            help="Contraseña ficticia si no usas --subscriber-code (default: Xk9#mP2wQz).",
        )

    def handle(self, *args, **options):
        preview = options["preview"] or bool(options.get("preview_file"))
        recipient = (options.get("recipient") or "").strip()
        sample_email = (options.get("sample_email") or recipient or "juan.perez@ejemplo.com").strip()

        if not preview and not recipient:
            raise CommandError("Indica --to para enviar, o usa --preview / --preview-file.")

        if options["social"]:
            context = build_welcome_email_context(
                first_name=options["first_name"],
                last_name=options["last_name"],
                email=sample_email,
                subscriber_code="PREVIEW",
                is_social_account=True,
            )
        elif options.get("subscriber_code"):
            context = build_welcome_email_context(
                first_name=options["first_name"],
                last_name=options["last_name"],
                email=sample_email,
                subscriber_code=options["subscriber_code"].strip(),
                is_social_account=False,
            )
        else:
            context = {
                "full_name": f"{options['first_name']} {options['last_name']}".strip(),
                "username": sample_email,
                "password_display": options.get("password") or "Xk9#mP2wQz",
                "is_social_account": False,
                "support_email": EmailConfig.SUPPORT_ADDRESS,
                "support_phone": EmailConfig.SUPPORT_PHONE,
                "terms_url": EmailConfig.TERMS_URL,
                "google_play_url": EmailConfig.GOOGLE_PLAY_URL,
                "app_store_url": EmailConfig.APP_STORE_URL,
            }

        text_body, html_body = render_welcome_email_bodies(context)

        if preview:
            preview_path = options.get("preview_file")
            if preview_path:
                path = Path(preview_path)
                path.write_text(html_body, encoding="utf-8")
                self.stdout.write(self.style.SUCCESS(f"HTML guardado en {path.resolve()}"))
            else:
                self.stdout.write("--- TEXTO PLANO ---\n")
                self.stdout.write(text_body)
                self.stdout.write("\n--- HTML (primeras 80 líneas) ---\n")
                lines = html_body.splitlines()
                self.stdout.write("\n".join(lines[:80]))
                if len(lines) > 80:
                    self.stdout.write(f"\n... ({len(lines) - 80} líneas más; usa --preview-file)")
            return

        subject = f"[PRUEBA] {EmailConfig.WELCOME_SUBJECT}"
        send_mail(
            subject=subject,
            message=text_body,
            from_email=EmailConfig.DEFAULT_FROM,
            recipient_list=[recipient],
            fail_silently=False,
            html_message=html_body,
        )
        self.stdout.write(
            self.style.SUCCESS(f"Correo de prueba enviado a {recipient} (asunto: {subject})")
        )
