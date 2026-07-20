# Generado con Django 5.2.14 (verificado con makemigrations + migrate contra
# SQLite en un entorno aislado, mismo procedimiento que
# 0004_db_performance_indexes -- este repo no tiene Django con acceso a
# Postgres en el entorno de verificación).
#
# Acompaña el estado de aprovisionamiento parcial (auditoría: "create_subscriber.py
# -- fallos parciales no abortan el registro"): provisioning_status/
# provisioning_pending_steps/provisioning_retry_count en ListOfSubscriber,
# usados por create_subscriber.py, finish_subscriber_provisioning_task y la
# nueva tarea periódica retry_partial_provisioning_task (ver wind/tasks.py,
# wind/services/subscriber_provisioning.py). Todos los campos son aditivos
# con default -- no requieren backfill ni tocan filas existentes.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('wind', '0004_db_performance_indexes'),
    ]

    operations = [
        migrations.AddField(
            model_name='listofsubscriber',
            name='provisioning_pending_steps',
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='listofsubscriber',
            name='provisioning_retry_count',
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='listofsubscriber',
            name='provisioning_status',
            field=models.CharField(choices=[('complete', 'Complete'), ('partial', 'Partial')], db_index=True, default='complete', max_length=20),
        ),
    ]
