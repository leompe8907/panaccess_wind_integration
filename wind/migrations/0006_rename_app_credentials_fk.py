# Escrita a mano siguiendo el mismo procedimiento que 0003/0005 (este
# entorno de verificación no tiene acceso a Postgres para correr
# `makemigrations` de verdad) -- `RenameField` es una operación simple y
# determinística, sin ambigüedad posible en su salida.
#
# Corrige el hallazgo de auditoría: `EncryptedCredentialsLog.app_credentials_id`
# ya incluía el sufijo `_id` en el nombre del campo Python, pero Django
# vuelve a agregar `_id` automáticamente a la columna real de toda
# ForeignKey -- el resultado era una columna Postgres llamada
# `app_credentials_id_id` (duplicada), en vez de la esperada
# `app_credentials_id`. Renombrado el campo a `app_credentials` (sin el
# sufijo manual) para que la columna quede como se esperaría.
#
# **Pendiente antes de desplegar:** correr `python manage.py migrate`
# contra un entorno real con acceso a Postgres para confirmar que el
# `ALTER TABLE ... RENAME COLUMN` aplica limpio.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('wind', '0005_partial_provisioning_state'),
    ]

    operations = [
        migrations.RenameField(
            model_name='encryptedcredentialslog',
            old_name='app_credentials_id',
            new_name='app_credentials',
        ),
    ]
