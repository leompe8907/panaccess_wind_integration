from pathlib import Path
import os
import sys
import django

# Obtiene la raíz del proyecto de forma dinámica (carpeta padre de docs)
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.append(str(BASE_DIR))

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'panaccess_wind_integration.settings')
django.setup()

from django.db import connection

sql_commands = [
    # 1. wind_listofsubscriber
    "ALTER TABLE wind_listofsubscriber ADD COLUMN IF NOT EXISTS closed_at timestamp with time zone;",
    "ALTER TABLE wind_listofsubscriber ADD COLUMN IF NOT EXISTS closed_reason text;",
    "ALTER TABLE wind_listofsubscriber ADD COLUMN IF NOT EXISTS status varchar(20) NOT NULL DEFAULT 'active';",
    "CREATE INDEX IF NOT EXISTS wind_listofsubscriber_status_idx ON wind_listofsubscriber(status);",

    # 2. wind_subscriberdocumentregistry
    "ALTER TABLE wind_subscriberdocumentregistry ADD COLUMN IF NOT EXISTS account_closed_at timestamp with time zone;",
    "ALTER TABLE wind_subscriberdocumentregistry ADD COLUMN IF NOT EXISTS closed_subscriber_code varchar(100);",
    "ALTER TABLE wind_subscriberdocumentregistry ADD COLUMN IF NOT EXISTS eligible_for_trial boolean NOT NULL DEFAULT true;",
    "ALTER TABLE wind_subscriberdocumentregistry ADD COLUMN IF NOT EXISTS trial_expires_at timestamp with time zone;",
    "ALTER TABLE wind_subscriberdocumentregistry ADD COLUMN IF NOT EXISTS trial_granted_at timestamp with time zone;",
    "ALTER TABLE wind_subscriberdocumentregistry ADD COLUMN IF NOT EXISTS trial_used boolean NOT NULL DEFAULT false;",
    "CREATE INDEX IF NOT EXISTS wind_subscr_trial_u_fca576_idx ON wind_subscriberdocumentregistry(trial_used);",
    "CREATE INDEX IF NOT EXISTS wind_subscr_account_771ba6_idx ON wind_subscriberdocumentregistry(account_closed_at);",

    # 3. wind_subscriberemailregistry
    "ALTER TABLE wind_subscriberemailregistry ADD COLUMN IF NOT EXISTS account_closed_at timestamp with time zone;",
    "ALTER TABLE wind_subscriberemailregistry ADD COLUMN IF NOT EXISTS closed_subscriber_code varchar(100);",
    "ALTER TABLE wind_subscriberemailregistry ADD COLUMN IF NOT EXISTS eligible_for_trial boolean NOT NULL DEFAULT true;",
    "ALTER TABLE wind_subscriberemailregistry ADD COLUMN IF NOT EXISTS trial_expires_at timestamp with time zone;",
    "ALTER TABLE wind_subscriberemailregistry ADD COLUMN IF NOT EXISTS trial_granted_at timestamp with time zone;",
    "ALTER TABLE wind_subscriberemailregistry ADD COLUMN IF NOT EXISTS trial_used boolean NOT NULL DEFAULT false;",
    "CREATE INDEX IF NOT EXISTS wind_subscr_trial_u_77fb03_idx ON wind_subscriberemailregistry(trial_used);",
    "CREATE INDEX IF NOT EXISTS wind_subscr_account_b8eb99_idx ON wind_subscriberemailregistry(account_closed_at);",
]

print("Iniciando la restauración de columnas eliminadas...")
with connection.cursor() as cursor:
    for cmd in sql_commands:
        try:
            cursor.execute(cmd)
            print(f"Ejecutado con éxito: {cmd}")
        except Exception as e:
            print(f"Error ejecutando '{cmd}': {e}")
print("Restauración finalizada.")
