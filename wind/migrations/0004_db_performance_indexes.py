# Generado con Django 5.2.14 (verificado con makemigrations + migrate contra
# SQLite en un entorno aislado, ya que este repo no tiene Django instalado con
# acceso a Postgres). Acompaña las mejoras de rendimiento de BD:
#
# - Elimina índices duplicados: 'code' y 'sn' ya tenían unique=True (que crea
#   su propio índice único) además de db_index=True y una entrada en
#   Meta.indexes para el mismo campo -- se mantenían dos/tres índices btree
#   idénticos por columna, duplicando el costo de cada escritura sin ningún
#   beneficio de lectura.
# - Elimina el índice sobre 'smartcards' (JSONField): nunca se filtra por
#   igualdad completa de ese campo en todo el código, así que era puro costo
#   de escritura sin beneficio.
# - Agrega un índice funcional sobre Upper(emails) en ListOfSubscriber: el
#   login, registro, login social y resolución de perfil filtran con
#   emails__iexact, que en PostgreSQL compila a UPPER(emails) = UPPER(...) y
#   NO puede usar el índice plano existente sobre 'emails' -- es el camino
#   más caliente del sistema y antes de este índice probablemente hacía
#   sequential scan en cada login.

import django.db.models.functions.text
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('wind', '0003_closure_retry_password_reset_security_profile'),
    ]

    operations = [
        migrations.RemoveIndex(
            model_name='listofsmartcards',
            name='wind_listof_subscri_aa9df0_idx',
        ),
        migrations.RemoveIndex(
            model_name='listofsmartcards',
            name='wind_listof_sn_71b4a9_idx',
        ),
        migrations.RemoveIndex(
            model_name='listofsubscriber',
            name='wind_listof_code_414b90_idx',
        ),
        migrations.RemoveIndex(
            model_name='listofsubscriber',
            name='wind_listof_emails_81bc55_idx',
        ),
        migrations.RemoveIndex(
            model_name='listofsubscriber',
            name='wind_listof_smartca_c64543_idx',
        ),
        migrations.AlterField(
            model_name='listofsmartcards',
            name='sn',
            field=models.CharField(blank=True, max_length=100, null=True, unique=True),
        ),
        migrations.AlterField(
            model_name='listofsubscriber',
            name='code',
            field=models.CharField(blank=True, max_length=100, null=True, unique=True),
        ),
        migrations.AlterField(
            model_name='listofsubscriber',
            name='smartcards',
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddIndex(
            model_name='listofsubscriber',
            index=models.Index(django.db.models.functions.text.Upper('emails'), name='wind_lof_sub_emails_upper'),
        ),
    ]
