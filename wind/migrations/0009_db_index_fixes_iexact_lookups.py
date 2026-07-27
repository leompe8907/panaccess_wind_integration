# Generado con Django 5.2.14 (misma metodología que 0004: makemigrations +
# migrate verificado contra SQLite en un entorno aislado, ya que este repo no
# tiene Django instalado con acceso a Postgres). Continúa el trabajo de 0004
# de optimización de índices:
#
# - SubscriberEmailRegistry.email tenía unique=True + db_index=True + una
#   entrada en Meta.indexes para el mismo campo -- TRES índices btree
#   idénticos sobre 'email' (mismo bug que 'code'/'sn' en ListOfSubscriber/
#   ListOfSmartcards). document/trial_used/account_closed_at tenían
#   db_index=True + Meta.Index para el mismo campo cada uno -- índice
#   duplicado. Esta tabla se reescribe en cada registro/compra/cierre de
#   cuenta, así que cada índice de más dobla el costo de esas escrituras sin
#   ningún beneficio de lectura.
# - SubscriberDocumentRegistry.document tenía el mismo triple (unique= True +
#   db_index=True + Meta.Index); trial_used/account_closed_at el mismo par
#   duplicado.
# - Agrega índices funcionales sobre Upper(...) que faltaban en los otros
#   caminos calientes de autenticación además de emails (ya cubierto en
#   0004):
#     * SubscriberEmailRegistry.email -- resuelto por email__iexact en
#       subscriber_catalog.py (casi cada request autenticado),
#       password_reset.py, subscriber_auth.py, subscriber_trial.py,
#       social_login_provisioning.py.
#     * ListOfSubscriber.code -- resuelto por code__iexact en
#       subscriber_auth.py.
#     * SubscriberLoginInfo.login2 -- resuelto por login2__iexact en
#       subscriber_auth.py.
#   Sin estos, cada UPPER(campo) = UPPER(...) forzaba sequential scan pese a
#   existir un índice plano sobre la columna.
# - auth_user.email (modelo propio de django.contrib.auth, no editable via
#   Meta.indexes) también se resuelve por email__iexact en
#   subscriber_auth.py/password_reset.py y por Lower("email") en
#   subscriber_closure.py -- se agrega el mismo índice funcional por SQL
#   crudo. CREATE INDEX CONCURRENTLY en Postgres (no bloquea la tabla
#   mientras se construye) requiere que la migración no corra dentro de una
#   transacción (atomic = False); en SQLite (dev sin Postgres) no existe
#   CONCURRENTLY, así que se usa CREATE INDEX simple.

import django.db.models.functions.text
from django.conf import settings
from django.db import migrations, models

AUTH_USER_EMAIL_UPPER_INDEX = "auth_user_email_upper_idx"


def create_auth_user_email_upper_index(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        if schema_editor.connection.vendor == "postgresql":
            cursor.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS "
                f"{AUTH_USER_EMAIL_UPPER_INDEX} ON auth_user (UPPER(email))"
            )
        else:
            cursor.execute(
                f"CREATE INDEX IF NOT EXISTS "
                f"{AUTH_USER_EMAIL_UPPER_INDEX} ON auth_user (UPPER(email))"
            )


def drop_auth_user_email_upper_index(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        if schema_editor.connection.vendor == "postgresql":
            cursor.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {AUTH_USER_EMAIL_UPPER_INDEX}")
        else:
            cursor.execute(f"DROP INDEX IF EXISTS {AUTH_USER_EMAIL_UPPER_INDEX}")


class Migration(migrations.Migration):

    # Requerido por CREATE/DROP INDEX CONCURRENTLY (Postgres no permite ese
    # comando dentro de un bloque de transacción).
    atomic = False

    dependencies = [
        ('wind', '0008_devicesession'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveIndex(
            model_name='subscriberdocumentregistry',
            name='wind_subscr_documen_ad7d90_idx',
        ),
        migrations.RemoveIndex(
            model_name='subscriberemailregistry',
            name='wind_subscr_email_6cc5ab_idx',
        ),
        migrations.AlterField(
            model_name='subscriberdocumentregistry',
            name='account_closed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='subscriberdocumentregistry',
            name='document',
            field=models.CharField(max_length=50, unique=True),
        ),
        migrations.AlterField(
            model_name='subscriberdocumentregistry',
            name='trial_used',
            field=models.BooleanField(default=False),
        ),
        migrations.AlterField(
            model_name='subscriberemailregistry',
            name='account_closed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='subscriberemailregistry',
            name='document',
            field=models.CharField(blank=True, max_length=50, null=True),
        ),
        migrations.AlterField(
            model_name='subscriberemailregistry',
            name='email',
            field=models.EmailField(max_length=254, unique=True),
        ),
        migrations.AlterField(
            model_name='subscriberemailregistry',
            name='trial_used',
            field=models.BooleanField(default=False),
        ),
        migrations.AddIndex(
            model_name='listofsubscriber',
            index=models.Index(django.db.models.functions.text.Upper('code'), name='wind_lof_sub_code_upper'),
        ),
        migrations.AddIndex(
            model_name='subscriberemailregistry',
            index=models.Index(django.db.models.functions.text.Upper('email'), name='wind_email_reg_upper'),
        ),
        migrations.AddIndex(
            model_name='subscriberlogininfo',
            index=models.Index(django.db.models.functions.text.Upper('login2'), name='wind_sli_login2_upper'),
        ),
        migrations.RunPython(
            create_auth_user_email_upper_index,
            drop_auth_user_email_upper_index,
        ),
    ]
