# Generated manually (2026-09-01) matching applogs/models.py -- ver nota en
# docs/LOGS_DIAGNOSTICO_2026-09-01.md: se escribió a mano porque el acceso
# de shell a este repo estaba interrumpido en el momento de implementar esta
# app; verificar con `manage.py makemigrations --check` apenas se pueda
# correr para confirmar que no quedó ningún desfasaje contra los modelos.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="LogIssue",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("fingerprint", models.CharField(db_index=True, max_length=64, unique=True)),
                (
                    "platform",
                    models.CharField(
                        choices=[
                            ("web", "Web"),
                            ("tv_tizen", "TV Tizen"),
                            ("tv_webos", "TV webOS"),
                            ("ios", "iOS"),
                            ("android", "Android"),
                            ("backend", "Backend"),
                        ],
                        max_length=20,
                    ),
                ),
                (
                    "level",
                    models.CharField(
                        choices=[("error", "Error"), ("warning", "Warning"), ("info", "Info")],
                        default="error",
                        max_length=10,
                    ),
                ),
                ("message", models.CharField(max_length=500)),
                (
                    "status",
                    models.CharField(
                        choices=[("open", "Abierto"), ("resolved", "Resuelto"), ("ignored", "Ignorado")],
                        default="open",
                        max_length=10,
                    ),
                ),
                ("occurrence_count", models.PositiveIntegerField(default=0)),
                ("first_seen_at", models.DateTimeField(auto_now_add=True)),
                ("last_seen_at", models.DateTimeField(auto_now_add=True)),
                ("last_alerted_at", models.DateTimeField(blank=True, null=True)),
            ],
        ),
        migrations.CreateModel(
            name="LogEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("subscriber_code", models.CharField(blank=True, db_index=True, max_length=100)),
                ("device_type", models.CharField(blank=True, max_length=50)),
                ("app_version", models.CharField(blank=True, max_length=50)),
                ("stack", models.TextField(blank=True)),
                ("breadcrumbs", models.JSONField(blank=True, null=True)),
                ("extra", models.JSONField(blank=True, null=True)),
                ("client_ip", models.GenericIPAddressField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "issue",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="events",
                        to="applogs.logissue",
                    ),
                ),
            ],
        ),
        migrations.AddIndex(
            model_name="logissue",
            index=models.Index(
                fields=["platform", "status", "-last_seen_at"], name="applogs_issue_plat_status_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="logissue",
            index=models.Index(fields=["-last_seen_at"], name="applogs_issue_last_seen_idx"),
        ),
        migrations.AddIndex(
            model_name="logevent",
            index=models.Index(fields=["issue", "-created_at"], name="applogs_evt_issue_created_idx"),
        ),
        migrations.AddIndex(
            model_name="logevent",
            index=models.Index(fields=["subscriber_code"], name="applogs_event_subscriber_idx"),
        ),
    ]
