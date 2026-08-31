"""
Comando para enviar correos de prueba en lote a los suscriptores registrados y reportar cuáles fueron exitosos y cuáles fallaron.

Ejemplos:
  # Ejecutar en modo simulación (Dry Run) para ver a quiénes se les enviaría
  python manage.py send_bulk_test_emails

  # Enviar correos de prueba reales a todos los suscriptores activos
  python manage.py send_bulk_test_emails --send

  # Enviar correos de prueba usando la plantilla real de bienvenida (con credenciales)
  python manage.py send_bulk_test_emails --send --welcome

  # Filtrar por un email específico
  python manage.py send_bulk_test_emails --send --only usuario@ejemplo.com

  # Limitar a los primeros 10 y filtrar por estado
  python manage.py send_bulk_test_emails --send --limit 10 --status active
"""
from __future__ import annotations

from django.core.mail import send_mail
from django.core.management.base import BaseCommand

from appConfig import EmailConfig
from wind.models import ListOfSubscriber
from wind.services.welcome_email import (
    build_welcome_email_context,
    render_welcome_email_bodies,
)


class Command(BaseCommand):
    help = "Envía correos de prueba a los suscriptores de la base de datos y reporta los resultados de entrega."

    def add_arguments(self, parser):
        parser.add_argument(
            "--send",
            action="store_true",
            help="Si se especifica, realiza el envío real de correos. De lo contrario, corre en modo simulación (dry-run).",
        )
        parser.add_argument(
            "--welcome",
            action="store_true",
            help="Envía la plantilla de bienvenida real (con credenciales) en lugar de un correo de prueba genérico.",
        )
        parser.add_argument(
            "--status",
            default="active",
            choices=["active", "closed", "pending_closure", "all"],
            help="Filtrar suscriptores por estado (default: active).",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Limita la cantidad de correos a procesar.",
        )
        parser.add_argument(
            "--only",
            dest="only_emails",
            help="Envía únicamente a estos correos (separados por comas) si existen en la base de datos.",
        )

    def handle(self, *args, **options):
        send = options["send"]
        welcome = options["welcome"]
        status_filter = options["status"]
        limit = options["limit"]
        only_emails = options["only_emails"]

        # 1. Obtener suscriptores con email
        subscribers = ListOfSubscriber.objects.exclude(emails__isnull=True).exclude(emails="")

        # Filtrar por estado
        if status_filter != "all":
            subscribers = subscribers.filter(status=status_filter)

        # Filtrar por lista específica si se provee
        if only_emails:
            target_emails = [e.strip().lower() for e in only_emails.split(",") if e.strip()]
            subscribers = subscribers.filter(emails__in=target_emails)

        subscribers = subscribers.order_by("created")

        if limit is not None:
            subscribers = subscribers[:limit]

        total_count = subscribers.count()

        if total_count == 0:
            self.stdout.write(self.style.WARNING("No se encontraron suscriptores que coincidan con los filtros."))
            return

        self.stdout.write("=" * 60)
        if send:
            self.stdout.write(self.style.WARNING(f"INICIANDO ENVÍO REAL A {total_count} SUSCRIPTORES"))
        else:
            self.stdout.write(self.style.SUCCESS(f"MODO SIMULACIÓN (DRY RUN): Se procesarán {total_count} suscriptores"))
        self.stdout.write("=" * 60)

        successes = []
        failures = []

        for index, sub in enumerate(subscribers, start=1):
            email = sub.emails.strip()
            first_name = sub.firstName or ""
            last_name = sub.lastName or ""
            sub_code = sub.code or "N/A"
            full_name = f"{first_name} {last_name}".strip() or "Suscriptor"

            self.stdout.write(f"[{index}/{total_count}] Procesando {email} (Código: {sub_code}, Nombre: {full_name})...")

            # Construir asunto y cuerpos
            if welcome:
                # Determinar si es cuenta social por el prefijo del código (FB$ o GG$)
                is_social = False
                if sub.code:
                    is_social = sub.code.startswith("FB$") or sub.code.startswith("GG$")

                try:
                    context = build_welcome_email_context(
                        first_name=first_name,
                        last_name=last_name,
                        email=email,
                        subscriber_code=sub_code,
                        is_social_account=is_social,
                    )
                    text_body, html_body = render_welcome_email_bodies(context)
                    subject = EmailConfig.WELCOME_SUBJECT
                except Exception as e:
                    self.stdout.write(self.style.ERROR(f"  -> Error al construir contexto de bienvenida: {e}"))
                    failures.append((email, sub_code, f"Error construcción: {e}"))
                    continue
            else:
                # Correo de prueba genérico
                subject = f"[PRUEBA DE CONEXIÓN] Mensaje de verificación de correo WindTV"
                text_body = (
                    f"Hola {full_name},\n\n"
                    f"Este es un correo de prueba automatizado enviado desde el sistema de WindTV para "
                    f"verificar la recepción de correos en su dirección de correo electrónico ({email}).\n\n"
                    f"No es necesario que responda a este mensaje.\n\n"
                    f"Atentamente,\n"
                    f"El equipo de WindTV"
                )
                html_body = (
                    f"<div style='font-family: Arial, sans-serif; padding: 20px; line-height: 1.6;'>"
                    f"<h2>Hola {full_name},</h2>"
                    f"<p>Este es un correo de prueba automatizado enviado desde el sistema de WindTV para "
                    f"verificar la recepción de correos en su dirección de correo electrónico (<strong>{email}</strong>).</p>"
                    f"<p>No es necesario que responda a este mensaje.</p>"
                    f"<hr style='border: none; border-top: 1px solid #eee; margin: 20px 0;'>"
                    f"<p style='font-size: 0.9em; color: #666;'>Atentamente,<br><strong>El equipo de WindTV</strong></p>"
                    f"</div>"
                )

            if not send:
                # Simulación exitosa
                successes.append((email, sub_code, "Simulado correctamente"))
                self.stdout.write(self.style.SUCCESS("  -> [SIMULADO OK]"))
                continue

            # Envío real
            try:
                send_mail(
                    subject=subject,
                    message=text_body,
                    from_email=EmailConfig.DEFAULT_FROM,
                    recipient_list=[email],
                    fail_silently=False,
                    html_message=html_body,
                )
                successes.append((email, sub_code, "Enviado con éxito"))
                self.stdout.write(self.style.SUCCESS("  -> [ENVIADO OK]"))
            except Exception as e:
                failures.append((email, sub_code, str(e)))
                self.stdout.write(self.style.ERROR(f"  -> [FALLIDO]: {e}"))

        # 4. Mostrar reporte final
        self.stdout.write("\n" + "=" * 60)
        self.stdout.write(self.style.SUCCESS("REPORTE DE EJECUCIÓN"))
        self.stdout.write("=" * 60)
        self.stdout.write(f"Total procesados: {total_count}")
        self.stdout.write(self.style.SUCCESS(f"Exitosos:         {len(successes)}"))
        if failures:
            self.stdout.write(self.style.ERROR(f"Fallidos:         {len(failures)}"))
        else:
            self.stdout.write(self.style.SUCCESS("Fallidos:         0"))
        self.stdout.write("=" * 60 + "\n")

        if successes:
            self.stdout.write(self.style.SUCCESS("DETALLE DE ENVÍOS EXITOSOS:"))
            for email, code, status_msg in successes:
                self.stdout.write(f" - {email} ({code}): {status_msg}")

        if failures:
            self.stdout.write("\n" + self.style.ERROR("DETALLE DE ENVÍOS FALLIDOS:"))
            for email, code, error_msg in failures:
                self.stdout.write(self.style.ERROR(f" - {email} ({code}): {error_msg}"))

        self.stdout.write("\n" + "=" * 60)
