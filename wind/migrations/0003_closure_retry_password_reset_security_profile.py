# Generated manually (siguiendo el estilo de 0002_*) para acompañar los
# cambios de la auditoría del 2026-07-15: reintento de cierres parciales,
# token de reset de contraseña persistido en BD, y metadatos de seguridad
# por usuario para invalidar JWT tras un cambio de contraseña.

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('wind', '0002_subscriberclosurelog_listofsubscriber_closed_at_and_more'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='listofsubscriber',
            name='closure_retry_count',
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.CreateModel(
            name='PasswordResetTokenUse',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('token_hash', models.CharField(db_index=True, max_length=64, unique=True)),
                ('used_at', models.DateTimeField(auto_now_add=True)),
            ],
        ),
        migrations.CreateModel(
            name='UserSecurityProfile',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('password_changed_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('user', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='security_profile', to=settings.AUTH_USER_MODEL)),
            ],
        ),
    ]
